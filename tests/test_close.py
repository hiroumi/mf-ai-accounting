from mf_accounting import close


def hrow(i, d, content, label, acct):
    return {"transaction_id": i, "tx_date": d, "fiscal_year": 2024, "tx_content": content, "primary_account_id": label,
            "connected_sub_account_id": acct, "complex_journal": False}


def target(i, d, content, acct):
    return {"transaction_id": i, "tx_date": d, "tx_content": content, "tx_value": 1000, "tx_side": "EXPENSE", "connected_sub_account_id": acct}


CONNECTED = [
    {"name": "楽天カード", "connected_sub_accounts": [{"id": "R_OLD"}, {"id": "R_NEW"}, {"id": "R_VISA"}]},
    {"name": "アメリカン・エキスプレスカード", "connected_sub_accounts": [{"id": "AMEX"}]},
]


def test_lineage_map_merges_rakuten_sub_accounts_only():
    m = close.lineage_map(CONNECTED)
    assert m["R_OLD"] == m["R_NEW"] == m["R_VISA"] == "lineage:楽天カード"
    assert m["AMEX"] == "AMEX"


def test_amazon_dup_keywords_exclude_aws_and_bank():
    assert close.is_amazon_dup("AMAZON.CO.JP", True)
    assert close.is_amazon_dup("アマゾンプライムカイヒ", True)
    assert close.is_amazon_dup("ＡＭＡＺＯＮ．ＣＯ．ＪＰ", True)  # 全角も NFKC で拾う
    assert not close.is_amazon_dup("AMAZON WEB SERVICES", True)
    assert not close.is_amazon_dup("AMAZON.CO.JP", False)  # カード明細のみ


def test_classify_uses_lineage_for_rule_candidate():
    hist = [hrow(f"h{i}", f"2024-0{i}-01", "SHOP", "X", "R_OLD") for i in range(1, 4)]
    items = close.classify([target("t1", "2025-01-01", "SHOP", "R_NEW"), target("t2", "2025-01-01", "SHOP", "AMEX")], hist,
                           close.lineage_map(CONNECTED))
    by = {i["transaction_id"]: i for i in items}
    assert by["t1"]["layer"] == "H" and by["t1"]["account_seen"] is True and by["t1"]["routing"] == close.RULE_CANDIDATE
    assert by["t2"]["account_seen"] is False and by["t2"]["routing"] == close.HUMAN


def test_classify_layers_and_sonnet_routing():
    hist = [hrow("h1", "2024-03-01", "ONCE", "X", "AMEX"), hrow("h2", "2024-03-01", "SPLIT", "X", "AMEX"),
            hrow("h3", "2024-04-01", "SPLIT", "Y", "AMEX")]
    items = close.classify([target("e", "2024-06-01", "ONCE", "AMEX"), target("c", "2024-06-01", "SPLIT", "AMEX"),
                            target("a", "2024-06-01", "ZZZZZZZZZZ", "AMEX")], hist, {})
    by = {i["transaction_id"]: i for i in items}
    assert by["e"]["layer"] == "E" and by["e"]["routing"] == close.HUMAN
    assert by["c"]["layer"] == "C" and by["c"]["routing"] == "sonnet"
    assert by["a"]["layer"] == "A" and by["a"]["routing"] == "sonnet"


def test_route_sonnet_uses_preregistered_balanced_gate():
    ok = {"primary_account_id": "X", "confidence": 0.90, "needs_review": False, "insufficient_information": False}
    assert close.route_sonnet(ok) == close.SONNET_AUTO
    assert close.route_sonnet({**ok, "confidence": 0.89}) == close.HUMAN
    assert close.route_sonnet({**ok, "needs_review": True}) == close.HUMAN
    assert close.route_sonnet({**ok, "insufficient_information": True}) == close.HUMAN
    assert close.route_sonnet({**ok, "primary_account_id": None}) == close.HUMAN


def test_review_rows_group_same_content_and_account():
    items = [{**target(i, d, c, "AMEX"), "layer": "H", "account_seen": True, "routing": close.RULE_CANDIDATE, "pred": "X", "hist_n": 3}
             for i, d, c in (("1", "2025-02-01", "SHOP"), ("2", "2025-01-01", "OTHER"), ("3", "2025-01-01", "SHOP"))]
    rows = close.review_rows(items, {}, {"X": "消耗品費"}, {})
    assert [r["transaction_id"] for r in rows] == ["2", "3", "1"]
    assert rows[1]["review_group"] == rows[2]["review_group"] != rows[0]["review_group"]
    assert rows[1]["group_size"] == 2 and rows[1]["inference_source"] == "rule"
