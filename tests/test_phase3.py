from mf_accounting import phase3

N = phase3.NORMALIZERS


def test_normalizers_keep_short_numbers_and_unify_width():
    assert N["n1 NFKC+casefold+空白整理"]("ＡＢＣ　 Shop") == "abc shop"
    assert N["n3 +空白除去"]("ｶﾌｪ・ABC 7") == "カフェabc7"  # 半角カナ→全角、記号除去、短い数字は残す
    assert N["n4 +6桁以上の数字をマスク"]("振込 123456789 abc12") == "振込#abc12"
    assert N["n5 +4桁以上の数字と日付をマスク"]("abc 4/15 1234 7") == "abc##7"
    assert N["raw"]("  x ") == "x"


def row(i, d, content, label, fy=2024, cx=False):
    return {"transaction_id": i, "tx_date": d, "fiscal_year": fy, "tx_content": content, "primary_account_id": label, "complex_journal": cx}


def test_rolling_stages_and_no_future_leak():
    rows = [
        row("1", "2023-09-01", "ＡＢＣ SHOP", "EXP", fy=2023),
        row("2", "2024-09-01", "ＡＢＣ SHOP", "EXP"),  # raw exact
        row("3", "2024-09-02", "abc shop", "EXP"),  # normalized exact
        row("4", "2024-09-03", "abc shopp", "EXP"),  # fuzzy
        row("5", "2024-09-03", "abc shoppp", "OTHER"),  # 同日の #4 は検索対象外
        row("6", "2024-09-04", "zzzz", "X"),
        row("7", "2025-09-01", "zzzz", "X", fy=2025),  # 未来
    ]
    preds = {p["transaction_id"]: p for p in phase3.rolling_predictions(rows, 2024, "n3 +空白除去")}
    assert preds["2"]["stage"] == "raw_exact" and preds["2"]["correct"]
    assert preds["3"]["stage"] == "norm_exact"
    assert preds["4"]["stage"] == "fuzzy" and preds["4"]["score"] >= 90
    assert "abcshopp" not in preds["5"]["top3_keys"]
    assert preds["6"]["stage"] in ("fuzzy", "none") and preds["6"].get("hist_n", 0) <= 4
    assert "7" not in preds
    s = phase3.stage_summary(phase3.apply_threshold(list(preds.values()), 95))
    assert s["raw_exact"]["n"] == 1 and s["norm_exact"]["n"] == 1
