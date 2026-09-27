from mf_accounting import routing


def row(i, d, content, label, acct="CS1", fy=2024, value=1000, side="EXPENSE", cx=False):
    return {"transaction_id": i, "tx_date": d, "fiscal_year": fy, "tx_content": content, "primary_account_id": label,
            "connected_sub_account_id": acct, "tx_value": value, "tx_side": side, "complex_journal": cx}


def test_history_features_use_only_past():
    rows = [
        row("a", "2023-09-01", "SHOP", "X", fy=2023),
        row("b", "2024-09-01", "SHOP", "X"),
        row("c", "2024-09-01", "SHOP", "Y", acct="CS2"),  # 同日: 互いに見えない
        row("d", "2024-10-01", "SHOP", "X", acct="CS2"),
        row("e", "2025-09-01", "SHOP", "Z", fy=2025),  # 未来
    ]
    f = routing.history_features(rows, 2024)
    assert f["b"]["hist_n"] == 1 and f["c"]["hist_n"] == 1
    assert f["c"]["account_seen"] is False  # CS2 は過去になし
    assert f["d"]["hist_n"] == 3 and f["d"]["account_seen"] is True and f["d"]["distinct_labels"] == 2
    assert "e" not in f


def item(**kw):
    base = {"high_confidence": False, "account_seen": True, "llm": "X", "conf": 0.95, "nr": False, "rule_valid": True, "agree": True}
    return {**base, **kw}


def test_plan_routing():
    d = routing.plan(0.9)
    assert d(item(high_confidence=True)) == "rule"
    assert d(item(high_confidence=True, account_seen=False)) == "human"
    assert d(item()) == "llm"
    assert d(item(conf=0.85)) == "human"
    assert d(item(nr=True)) == "human"
    assert d(item(agree=False)) == "human"
    assert d(item(account_seen=False)) == "human"
    assert d(item(rule_valid=False, agree=False, account_seen=None)) == "llm"  # 履歴なし（A層）は口座条件の対象外
