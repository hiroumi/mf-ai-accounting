from mf_accounting import phase2

TERMS = [{"fiscal_year": fy, "start_date": f"{fy}-08-01", "end_date": f"{fy + 1}-07-31"} for fy in range(2020, 2025)]
CONNECTED = [{"id": "S1", "connected_sub_accounts": [{"id": "CS1", "account_id": "NEW_BANK", "sub_account_id": "NEW_SUB"}]}]


def side(acc, value, sub=None, tax=0, tax_id=None, inv=None, name=None):
    return {"account_id": acc, "sub_account_id": sub, "value": value, "tax_value": tax, "tax_id": tax_id, "invoice_kind": inv, "account_name": name or acc}


def tx(i, date, value, content="SHOP", side_="EXPENSE"):
    return {"id": i, "date": date, "value": value, "side": side_, "content": content, "connected_account_id": "S1", "connected_sub_account_id": "CS1"}


def jr(i, t, date, branches):
    return {"id": i, "transaction_id": t, "transaction_date": date, "number": 1, "branches": branches}


def test_bank_side_inferred_per_fiscal_year_even_if_master_changed():
    # FY2020 は旧口座科目 OLD_BANK、現マスターは NEW_BANK
    txs = [tx(f"t{i}", "2020-09-0%d" % (i + 1), 1100) for i in range(3)]
    js = [jr(f"j{i}", f"t{i}", "2020-09-0%d" % (i + 1), [{"debitor": side("EXP", 1000, tax=100), "creditor": side("OLD_BANK", 1100)}]) for i in range(3)]
    rows, bank_map = phase2.build_labels(js, txs, TERMS, CONNECTED)
    assert bank_map.by_sub[(2020, "CS1")]["key"] == ("OLD_BANK", None)
    assert all(r["bank_method"] == "fy_sub_account_map" and r["bank_account_id"] == "OLD_BANK" for r in rows)
    assert all(r["bank_matches_current_master"] is False and r["bank_amount_matches_tx"] for r in rows)
    assert all(r["label_account"] == "EXP" and r["complex_journal"] is False for r in rows)


def test_intermediate_account_nets_out():
    t = tx("t", "2024-09-01", 1100)
    j = jr("j", "t", "2024-09-01", [
        {"debitor": side("AP", 1100), "creditor": side("BANK", 1100)},
        {"debitor": side("EXP", 1000, tax=100, tax_id="T10", inv="Q"), "creditor": side("AP", 1100)},
    ])
    lab = phase2.counter_label(t, j, ("BANK", None))
    assert lab["label_account"] == "EXP" and lab["complex_journal"] is False
    assert lab["primary_tax_id"] == "T10" and lab["primary_invoice_kind"] == "Q"


def test_fee_deducted_income_is_complex_with_negative():
    t = tx("t", "2024-09-01", 9890, side_="INCOME")
    j = jr("j", "t", "2024-09-01", [
        {"debitor": side("BANK", 9890), "creditor": side("SALES", 10000)},
        {"debitor": side("FEE", 110), "creditor": None},
    ])
    assert phase2.bank_candidates(j, t) == [("BANK", None)]
    lab = phase2.counter_label(t, j, ("BANK", None))
    assert lab["complex_journal"] is True and lab["has_negative_counter"] is True
    assert lab["label_account"] == "-FEE|SALES" and lab["primary_account_id"] == "SALES"


def test_backtest_uses_only_past_years():
    rows = [
        {"transaction_id": "a", "fiscal_year": 2022, "tx_date": "2022-09-01", "tx_content": "X", "label_account": "A"},
        {"transaction_id": "b", "fiscal_year": 2023, "tx_date": "2023-09-01", "tx_content": "X", "label_account": "B"},
        {"transaction_id": "c", "fiscal_year": 2024, "tx_date": "2024-09-01", "tx_content": "X", "label_account": "B"},
        {"transaction_id": "d", "fiscal_year": 2024, "tx_date": "2024-09-02", "tx_content": "NEW", "label_account": "C"},
        {"transaction_id": "e", "fiscal_year": 2025, "tx_date": "2025-09-01", "tx_content": "NEW", "label_account": "C"},  # 未来
    ]
    b = phase2.backtest(rows, 2024)
    assert b["train_fys"] == [2022, 2023] and b["test"] == 2 and b["covered"] == 1
    assert b["latest_correct"] == 1  # 直近 = B
    assert b["confidence_unanimous"][">=1"]["n"] == 0  # A/B 混在なので満場一致ではない
    b23 = phase2.backtest(rows, 2023)
    assert b23["covered"] == 1 and b23["mode_correct"] == 0 and b23["confidence_unanimous"][">=1"] == {"n": 1, "share_of_test": 1.0, "accuracy": 0.0}


def test_content_stats():
    rows = [
        {"transaction_id": str(i), "fiscal_year": fy, "tx_date": d, "tx_content": "X", "label_account": l}
        for i, (fy, d, l) in enumerate([(2022, "2022-09-01", "A"), (2022, "2022-10-01", "A"), (2023, "2023-09-01", "B")])
    ]
    s = phase2.content_stats(rows)[0]
    assert s["count"] == 3 and s["distinct_labels"] == 2 and s["top_label"] == "A" and s["latest_label"] == "B"
    assert s["labels_by_fy"] == {2022: {"A": 2}, 2023: {"B": 1}}
