from datetime import date

import pytest

from mf_accounting.quality import quality_report
from mf_accounting.storage import latest_run_dir, save_json
from mf_accounting.validate import DataValidationError, validate_journals, validate_transactions

S, E = date(2024, 8, 1), date(2025, 7, 31)


def J(i, d="2024-09-01", tx=None, branches=None, entered_by="JOURNAL_TYPE_NORMAL"):
    return {"id": i, "transaction_date": d, "transaction_id": tx, "entered_by": entered_by,
            "branches": branches or [{"debitor": {"value": 1}, "creditor": {"value": 1}}]}


def T(i, d="2024-09-01", status="registered", content="x", value=1100, sub="CS1"):
    return {"id": i, "date": d, "value": value, "side": "EXPENSE", "journalizing_status": status, "content": content, "connected_sub_account_id": sub}


def test_validate_journals_ok():
    validate_journals([J("a"), J("b")], {"total_count": 2}, S, E)


@pytest.mark.parametrize(
    "items,meta",
    [
        ([J("a")], {"total_count": 2}),  # 件数不一致
        ([J("a"), J("a")], {"total_count": 2}),  # ID重複
        ([J("a", d="2023-01-01")], {"total_count": 1}),  # 期間外
        ([J("a", branches=[{"debitor": None, "creditor": None}])], {"total_count": 1}),
        ([J("a", branches=[{"debitor": {"value": "1"}}])], {"total_count": 1}),
        ([J("a")], {}),
    ],
)
def test_validate_journals_rejects(items, meta):
    with pytest.raises(DataValidationError):
        validate_journals(items, meta, S, E)


def test_validate_transactions_rejects_unknown_status():
    validate_transactions([T("t")], {"total_count": 1}, S, E)
    with pytest.raises(DataValidationError):
        validate_transactions([T("t", status="weird")], {"total_count": 1}, S, E)


def test_validation_error_hides_values():
    with pytest.raises(DataValidationError) as e:
        validate_transactions([{**T("t"), "value": "秘密の金額"}], {"total_count": 1}, S, E)
    assert "秘密" not in str(e.value)


def test_latest_run_skips_failed(tmp_path):
    base = tmp_path / "raw" / "0000-0001" / "journals"
    save_json(base / "20260101-000000" / "manifest.json", {"status": "complete"})
    save_json(base / "20260102-000000" / "manifest.json", {"status": "failed"})
    (base / "20260103-000000").mkdir()  # manifest なし（中断）
    assert latest_run_dir(tmp_path, "0000-0001", "journals").name == "20260101-000000"


def test_quality_report_counts():
    terms = [{"fiscal_year": 2024, "start_date": "2024-08-01", "end_date": "2025-07-31"}]
    connected = [{"id": "C1", "connected_sub_accounts": [{"id": "CS1", "sub_account_id": "BANK"}]}]
    bank = {"sub_account_id": "BANK", "value": 1100}
    journals = [
        J("o", entered_by="JOURNAL_TYPE_OPENING"),
        J("j1", tx="t1", branches=[{"remark": "r", "debitor": {"value": 1000, "tax_value": 100}, "creditor": bank}]),
        J("j2", tx="t2", branches=[{"debitor": {"value": 500, "trade_partner_name": "p"}, "creditor": {**bank, "value": 500}},
                                   {"debitor": {"value": 1}, "creditor": {"value": 1}}]),
        J("j3", tx="t2", branches=[{"debitor": {"value": 600}, "creditor": {**bank, "value": 600}}]),
    ]
    txs = [T("t1", content="A"), T("t2", content="A"), T("t3", status="none", content="B"), T("t4", status="excluded", content="")]
    r = quality_report(journals, txs, terms, connected, [2024])["by_fiscal_year"][2024]
    assert r["journals_total"] == 4 and r["journals_normal"] == 3
    assert r["transactions"] == 4 and r["transactions_journalized"] == 2
    assert r["transactions_linked"] == 2 and r["transactions_linked_rate"] == 0.5
    assert r["transactions_multi_journal"] == 1 and r["journals_multi_branch"] == 1
    assert r["transactions_amount_match"] == 2  # t1: 1100 / t2: 複数仕訳の純額合計 500+600=1100
    assert r["content_empty"] == 1 and r["content_repeated_rows"] == 2 and r["content_repeated_distinct"] == 1 and r["content_unique_rows"] == 1
    assert r["journals_with_remark"] == 1 and r["journals_with_trade_partner"] == 1
