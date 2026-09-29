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


def test_merchant_key_strips_card_noise_and_merges_truncated_names():
    assert close.merchant_key("マスター国内利用 MZZ ペイペイ ブックオフ") == close.merchant_key("ペイペイ ブックオフ")
    assert close.merchant_key("サイゼリヤ/NFC") == close.merchant_key("サイゼリヤ")
    m = close.merchant_keys(["ペイペイ ララポートカシワ", "ペイペイ ララポートカ(ヘンサイヘンコウ", "ペイペイ ラ(ヘンサイヘンコウ",
                             "ペイペイ ラーメン"])
    assert m["ペイペイ ララポートカ(ヘンサイヘンコウ"] == m["ペイペイ ララポートカシワ"]
    assert m["ペイペイ ラ(ヘンサイヘンコウ"] != m["ペイペイ ラーメン"]  # 候補が複数なら寄せない


def rrow(i, content, acct, conf, routing=close.HUMAN, amount=1000, d="2025-09-01"):
    return {"transaction_id": i, "transaction_date": d, "transaction_content": content, "amount": amount, "side": "EXPENSE",
            "connected_account": "楽天カード / X", "proposed_primary_account": acct, "proposed_primary_account_id": acct,
            "inference_source": "sonnet", "confidence": conf, "reason": f"r{i}", "reason_category": "", "needs_review": True,
            "layer": "C", "routing": routing, "review_group": 1}


def test_review_groups_merge_by_merchant_and_expand_to_every_transaction():
    rows = [rrow("1", "ペイペイ ブックオフ", "X", 0.6, amount=500), rrow("2", "マスター国内利用 MZZ ペイペイ ブックオフ", "X", 0.8, amount=900),
            rrow("3", "ペイペイ ブックオフ", "Y", 0.5), rrow("4", "SHOP", "X", 0.9, routing=close.RULE_CANDIDATE)]
    groups, members = close.review_groups(rows, {"2": (5, "exact: X×5")})
    assert [g["routing"] for g in groups] == [close.HUMAN, close.HUMAN, close.RULE_CANDIDATE]  # 人間確認が先頭
    g = groups[0]
    assert g["group_size"] == 2 and g["proposed_account"] == "X" and g["amount_range"] == "500–900"
    assert g["confidence_range"] == "0.60–0.80" and g["reason"] == "r2" and g["past_accounts"] == "exact: X×5"
    assert g["same_merchant_other_groups"] == 1  # 同じ加盟店で推定科目が違うグループがある
    assert sorted(m["transaction_id"] for m in members) == ["1", "2", "3", "4"]
    assert {m["review_group"] for m in members if m["transaction_id"] in ("1", "2")} == {g["review_group"]}


def test_expand_decisions_group_row_override_and_split():
    groups = [{"review_group": "G1", "human_decision": "approve", "human_correction_account": "", "proposed_primary_account_id": "EXP"},
              {"review_group": "G2", "human_decision": "correct", "human_correction_account": "会議費", "proposed_primary_account_id": "EXP"},
              {"review_group": "G3", "human_decision": "split", "human_correction_account": "", "proposed_primary_account_id": "EXP"},
              {"review_group": "G4", "human_decision": "hold", "human_correction_account": "", "proposed_primary_account_id": "EXP"},
              {"review_group": "G5", "human_decision": "", "human_correction_account": "", "proposed_primary_account_id": "EXP"},
              {"review_group": "G6", "human_decision": "correct", "human_correction_account": "存在しない科目", "proposed_primary_account_id": "EXP"}]
    m = lambda i, g, rd="", rc="": {"transaction_id": i, "review_group": g, "row_decision_override": rd, "row_correction_account": rc}
    members = [m("a", "G1"), m("b", "G1", "correct", "会議費"), m("c", "G2"), m("d", "G3"), m("e", "G3", "approve"),
               m("f", "G4"), m("g", "G5"), m("h", "G6"), m("i", "G1", "maybe")]
    out = {r["transaction_id"]: r for r in close.expand_decisions(groups, members, {"会議費": "MTG", "消耗品費": "EXP"})}
    assert (out["a"]["final_status"], out["a"]["final_account_id"]) == (close.CONFIRMED, "EXP")
    assert (out["b"]["final_account_id"], out["b"]["decision_source"]) == ("MTG", "row")  # 行の判断が優先
    assert out["c"]["final_account_id"] == "MTG"
    assert out["d"]["final_status"] == close.UNDECIDED and "split" in out["d"]["problem"]
    assert out["e"]["final_status"] == close.CONFIRMED
    assert out["f"]["final_status"] == close.HELD and out["g"]["final_status"] == close.UNDECIDED
    assert out["h"]["final_status"] == close.INVALID and out["i"]["final_status"] == close.INVALID


def test_funding_source_is_explicit_not_inferred_from_card_type():
    assert close.funding_source("楽天カード", "楽天カード(Mastercard) XXXX - 2676") == close.FOUNDER_PERSONAL_CARD
    assert close.funding_source("楽天カード", "【利用不可】楽天カード(MasterCard) XXXX - 4004") == close.FOUNDER_PERSONAL_CARD
    assert close.funding_source("楽天カード", "楽天カード(Visa) XXXX - XXXX - XXXX - 4235") == close.FOUNDER_PERSONAL_CARD
    assert close.funding_source("楽天カード", "楽天カード(Visa) XXXX - XXXX - XXXX - 9999") == close.UNCONFIRMED  # 新しい Visa は推測しない
    assert close.funding_source("アメリカン・エキスプレスカード", "デルタ スカイマイル アメリカン・エキスプレス・カード 55006") == close.FOUNDER_PERSONAL_CARD
    assert close.funding_source("新しいカード会社", "何かのカード") == close.UNCONFIRMED
    assert close.funding_source("【法人】三井住友銀行", "三田通支店") == close.BANK


def test_personal_card_journal_nets_payable_and_books_loan():
    card = ("AP", "RAKUTEN")
    use = close.journal_branches("EXPENSE", 10000, "SUPPLIES", card, close.FOUNDER_PERSONAL_CARD, "LOAN")
    assert use == [{"debitor": {"account_id": "SUPPLIES", "sub_account_id": None, "value": 10000}, "creditor": {"account_id": "AP", "sub_account_id": "RAKUTEN", "value": 10000}},
                   {"debitor": {"account_id": "AP", "sub_account_id": "RAKUTEN", "value": 10000}, "creditor": {"account_id": "LOAN", "sub_account_id": None, "value": 10000}}]
    refund = close.journal_branches("INCOME", 3000, "SUPPLIES", card, close.FOUNDER_PERSONAL_CARD, "LOAN")
    assert refund[1] == {"debitor": {"account_id": "LOAN", "sub_account_id": None, "value": 3000}, "creditor": {"account_id": "AP", "sub_account_id": "RAKUTEN", "value": 3000}}
    corp = close.journal_branches("EXPENSE", 5000, "SUPPLIES", ("AP", "CORP"), close.CORPORATE_CARD, "LOAN")
    assert len(corp) == 1  # 法人カードは振替なし
    js = [{"funding": close.FOUNDER_PERSONAL_CARD, "side": "EXPENSE", "value": 10000, "branches": use},
          {"funding": close.FOUNDER_PERSONAL_CARD, "side": "INCOME", "value": 3000, "branches": refund},
          {"funding": close.CORPORATE_CARD, "side": "EXPENSE", "value": 5000, "branches": corp}]
    r = close.reconcile(js, "AP", "LOAN")
    p = r[close.FOUNDER_PERSONAL_CARD]
    assert (p["loan_increase"], p["loan_decrease"], p["loan_net"]) == (10000, 3000, 7000)
    assert p["payable_net"] == 0 and p["payable_net_zero_every_transaction"] and p["loan_net_equals_usage_minus_refund"]
    assert r[close.CORPORATE_CARD]["usage_total"] == 5000 and r[close.CORPORATE_CARD]["loan_increase"] == 0


def test_final_decision_uses_assistant_columns_only():
    ids = {"会議費": "MTG"}
    g = lambda rec, routing=close.HUMAN, corr="", proposed="EXP": {"assistant_recommendation": rec, "assistant_correction_account": corr,
                                                                  "routing": routing, "proposed_primary_account_id": proposed,
                                                                  "human_decision": "exclude"}  # human_decision は無視
    assert close.final_decision(g("approve"), ids) == (close.CONFIRMED, "EXP", "")
    assert close.final_decision(g("correct", corr="会議費"), ids) == (close.CONFIRMED, "MTG", "")
    assert close.final_decision(g("correct", corr="補助金収入"), ids)[0] == close.INVALID  # マスターにない科目
    assert close.final_decision(g("exclude"), ids)[0] == close.EXCLUDED
    assert close.final_decision(g("existing_auto_candidate", routing=close.RULE_CANDIDATE), ids) == (close.CONFIRMED, "EXP", "")
    assert close.final_decision(g("existing_auto_candidate"), ids)[0] == close.INVALID  # human_review には使えない
    assert close.final_decision(g("ask_hiro"), ids)[0] == close.UNDECIDED
    assert close.final_decision(g("approve", proposed=""), ids)[0] == close.INVALID


def test_validate_final_checks():
    card = ("AP", "C1")
    ok = {"transaction_id": "t1", "date": "2025-09-01", "funding": close.FOUNDER_PERSONAL_CARD,
          "branches": close.journal_branches("EXPENSE", 1000, "EXP", card, close.FOUNDER_PERSONAL_CARD, "LOAN")}
    out_of_fy = {**ok, "transaction_id": "t2", "date": "2026-08-01"}
    dup = {**ok}
    txs = [{"transaction_id": "t1", "status": close.CONFIRMED, "journal_status": "generated", "recommendation": "approve", "problem": ""},
           {"transaction_id": "t2", "status": close.CONFIRMED, "journal_status": "generated", "recommendation": "approve", "problem": ""},
           {"transaction_id": "t3", "status": close.EXCLUDED, "journal_status": "excluded", "recommendation": "exclude", "problem": ""},
           {"transaction_id": "t4", "status": close.UNDECIDED, "journal_status": "undecided", "recommendation": "ask_hiro", "problem": ""}]
    res = {c["check"]: c for c in close.validate_final(txs, [ok, out_of_fy, dup], "2025-08-01", "2026-07-31", "AP")}
    failed = {k for k, c in res.items() if c["result"] == "fail"}
    assert failed == {"未確定科目がない（仕訳対象はすべて仕訳化）", "ask_hiro が0件", "FY期間外の取引がない", "同一取引の重複仕訳がない"}
    excl = {**ok, "transaction_id": "t3"}
    res = {c["check"]: c for c in close.validate_final(txs[:1] + txs[2:3], [ok, excl], "2025-08-01", "2026-07-31", "AP")}
    assert res["exclude・統合済み明細が仕訳に含まれていない"]["result"] == "fail"


def test_load_overrides_validates_and_resolves_sub_accounts():
    subs = {"みずほ銀行": "S_MIZUHO"}
    rows = [{"transaction_id": "a", "action": "exclude", "reason": "r"},
            {"transaction_id": "b", "action": "set_counter_sub_account", "counter_sub_account": "みずほ銀行"},
            {"transaction_id": "c", "action": "merged_into", "target_transaction_id": "b"}]
    o = close.load_overrides(rows + [{"transaction_id": "d", "action": "confirm_refund", "reason": "r"}], subs, {"a", "b", "c", "d"})
    assert o["b"]["sub_account_id"] == "S_MIZUHO" and o["c"]["target"] == "b" and o["d"]["action"] == "confirm_refund"
    import pytest
    for bad in ([{"transaction_id": "x", "action": "exclude"}],
                [{"transaction_id": "a", "action": "guess"}],
                [{"transaction_id": "b", "action": "set_counter_sub_account", "counter_sub_account": "城南信用金庫"}],
                [{"transaction_id": "c", "action": "merged_into", "target_transaction_id": "a"}, {"transaction_id": "a", "action": "exclude"}]):
        with pytest.raises(ValueError):
            close.load_overrides(bad, subs, {"a", "b", "c"})


def test_bank_transfer_journal_uses_counter_sub_account():
    b = close.journal_branches("EXPENSE", 100000, "DEP", ("DEP", "SMBC"), close.BANK, "LOAN", primary_sub="YUCHO")
    assert b == [{"debitor": {"account_id": "DEP", "sub_account_id": "YUCHO", "value": 100000},
                  "creditor": {"account_id": "DEP", "sub_account_id": "SMBC", "value": 100000}}]


def test_load_overrides_set_account():
    o = close.load_overrides([{"transaction_id": "a", "action": "set_account", "account": "長期借入金"}], {}, {"a"}, {"長期借入金": "LOAN"})
    assert o["a"]["account_id"] == "LOAN"
    import pytest
    with pytest.raises(ValueError):
        close.load_overrides([{"transaction_id": "a", "action": "set_account", "account": "補助金収入"}], {}, {"a"}, {"長期借入金": "LOAN"})
