import csv
from datetime import date

from mf_accounting.endpoints import periods_from_term_settings, split_date_range
from mf_accounting.inspect_json import summarize
from mf_accounting.transform import (
    JOURNAL_LINE_COLUMNS,
    journal_lines,
    TRAINING_PAIR_COLUMNS,
    training_pairs,
    transaction_rows,
    write_csv,
)

JOURNAL = {
    "id": "J1",
    "number": 12,
    "transaction_date": "2025-04-03",
    "term_period": 5,
    "journal_type": "JOURNAL_TYPE_NORMAL",
    "entered_by": "JOURNAL_TYPE_NORMAL",
    "is_realized": True,
    "memo": "メモ",
    "tags": ["a", "b"],
    "transaction_id": "T1",
    "voucher_file_ids": ["v1"],
    "branches": [
        {
            "remark": "振込手数料",
            "debitor": {"account_name": "支払手数料", "value": 110, "tax_value": 10, "tax_name": "課税仕入 10%"},
            "creditor": {"account_name": "普通預金", "sub_account_name": "A銀行", "value": 110},
        },
        {"remark": "", "debitor": {"account_name": "旅費交通費", "value": 500}, "creditor": None},
    ],
}


def test_journal_lines_one_row_per_branch():
    rows = journal_lines([JOURNAL])
    assert len(rows) == 2
    r = rows[0]
    assert r["journal_id"] == "J1" and r["branch_index"] == 1 and r["branch_count"] == 2
    assert r["debit_account_name"] == "支払手数料" and r["credit_account_name"] == "普通預金"
    assert r["credit_sub_account_name"] == "A銀行"
    assert r["tags"] == "a|b" and r["voucher_file_count"] == 1
    assert rows[1]["credit_account_name"] is None


TERMS = [
    {"fiscal_year": 2024, "start_date": "2024-08-01", "end_date": "2025-07-31", "tax_method": "PROPORTIONAL_ALLOCATION", "accounting_method": "TAX_EXCLUDED_INCLUDED"},
    {"fiscal_year": 2025, "start_date": "2025-08-01", "end_date": "2026-07-31", "tax_method": "FREE"},
]
CONNECTED = [{"id": "C1", "name": "A銀行", "connected_sub_accounts": [{"id": "CS1", "name": "普通", "sub_account_id": "SUB_BANK"}]}]


def _journal(jid, tx_id, number, branches):
    return {"id": jid, "number": number, "transaction_date": "2025-04-03", "transaction_id": tx_id, "entered_by": "JOURNAL_TYPE_NORMAL", "branches": branches}


def test_transaction_rows_enriched():
    tx = transaction_rows(
        [{"id": "T1", "date": "2025-04-03", "value": 110, "side": "EXPENSE", "content": "x", "connected_account_id": "C1", "connected_sub_account_id": "CS1"}],
        CONNECTED, TERMS, {"T1": 2},
    )[0]
    assert tx["connected_account_name"] == "A銀行" and tx["connected_sub_account_name"] == "普通"
    assert tx["bank_sub_account_id"] == "SUB_BANK" and tx["linked_journal_count"] == 2
    assert tx["fiscal_year"] == 2024 and tx["term_tax_method"] == "PROPORTIONAL_ALLOCATION"


def test_training_pairs_keeps_multi_branch_structure():
    txs = transaction_rows(
        [
            {"id": "T1", "date": "2025-04-03", "value": 1100, "side": "EXPENSE", "connected_account_id": "C1", "connected_sub_account_id": "CS1"},
            {"id": "T2", "date": "2025-04-04", "value": 500, "side": "EXPENSE", "connected_account_id": "C1", "connected_sub_account_id": "CS1"},
        ],
        CONNECTED, TERMS,
    )
    bank = {"account_name": "普通預金", "sub_account_id": "SUB_BANK"}
    journals = [
        # 税抜: 借方 1000 + 税 100、貸方 口座 1100
        _journal("J1", "T1", 1, [{"remark": "r", "debitor": {"account_name": "消耗品費", "value": 1000, "tax_value": 100, "tax_name": "課仕 10%"}, "creditor": {**bank, "value": 1100}}]),
        # 複数行: 未払金経由
        _journal("J2", "T1", 2, [
            {"debitor": {"account_name": "未払金", "value": 1100}, "creditor": {**bank, "value": 1100}},
            {"debitor": {"account_name": "消耗品費", "value": 1100}, "creditor": {"account_name": "未払金", "value": 1100}},
        ]),
        _journal("J3", None, 3, [{"debitor": {"account_name": "雑費", "value": 1}, "creditor": {"account_name": "現金", "value": 1}}]),
    ]
    rows, stats = training_pairs(txs, journals, TERMS)
    assert stats["transactions"] == 2 and stats["transactions_linked"] == 1 and stats["link_rate"] == 0.5
    assert stats["journals_linked"] == 2 and stats["journals_per_transaction"] == [2] and stats["multi_branch_journals"] == 1
    assert len(rows) == 3  # J1: 1行 + J2: 2行（潰さない）
    j2 = [r for r in rows if r["journal_id"] == "J2"]
    assert [r["branch_index"] for r in j2] == [1, 2] and all(r["branch_count"] == 2 for r in j2)
    assert [r["bank_side"] for r in j2] == ["credit", "none"]
    assert all(r["amount_matches_tx"] is True for r in rows)
    assert rows[0]["fiscal_year"] == 2024 and rows[0]["debit_tax_name"] == "課仕 10%"
    assert set(rows[0]) <= set(TRAINING_PAIR_COLUMNS)


def test_write_csv_bom_and_columns(tmp_path):
    p = write_csv(tmp_path / "x.csv", journal_lines([JOURNAL]), JOURNAL_LINE_COLUMNS)
    raw = p.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    with p.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert list(rows[0].keys()) == JOURNAL_LINE_COLUMNS
    assert rows[0]["is_realized"] == "true" and rows[1]["credit_account_name"] == ""


def test_split_date_range_within_366_days():
    w = split_date_range(date(2022, 1, 1), date(2024, 6, 30))
    assert w[0][0] == date(2022, 1, 1) and w[-1][1] == date(2024, 6, 30)
    assert all((e - s).days <= 366 for s, e in w)
    assert all((b[0] - a[1]).days == 1 for a, b in zip(w, w[1:]))


def test_periods_sorted_deduped_and_clipped():
    terms = [
        {"fiscal_year": 2024, "start_date": "2024-04-01", "end_date": "2025-03-31"},
        {"fiscal_year": 2023, "start_date": "2023-04-01", "end_date": "2024-03-31"},
        {"fiscal_year": 2024, "start_date": "2024-04-01", "end_date": "2025-03-31"},
    ]
    p = periods_from_term_settings(terms, clip_start=date(2023, 10, 1))
    assert [x["fiscal_year"] for x in p] == [2023, 2024]
    assert p[0]["start_date"] == date(2023, 10, 1)


def test_summarize_reports_structure_not_values():
    s = summarize([{"content": "連絡先 03-1234-5678 test@example.com", "date": "2025-01-02", "n": {"x": 1}}], "date")
    assert s["count"] == 1 and s["date_min"] == "2025-01-02"
    pii = s["fields"]["content"]["pii_matches"]
    assert pii["email"] == 1 and pii["phone"] == 1
    assert "n.x" in s["fields"]
    assert "test@example.com" not in str(s)
