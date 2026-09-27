from mf_accounting import phase25
from mf_accounting.phase25 import Obs, Rule


def seq(labels, fys=None, cx=None):
    fys = fys or [2020] * len(labels)
    return [Obs(f"{fy}-09-{i + 1:02d}", fy, l, bool(cx and cx[i])) for i, (l, fy) in enumerate(zip(labels, fys))]


def test_change_detection_aaaaaabbb():
    o = seq(list("AAAAAABBB"))
    c = phase25.detect_change(o, 2)
    assert c["change_detected"] and c["previous_account"] == "A" and c["current_account"] == "B"
    assert c["observations_since_change"] == 3 and c["change_date"] == o[6].date
    assert phase25.mode(o)[0] == "A"
    assert not phase25.detect_change(seq(list("AAAAB")), 2)["change_detected"]  # 1件だけの変化は検知しない
    assert not phase25.detect_change(seq(list("BBB")), 2)["change_detected"]


def test_mode_tie_prefers_recent():
    assert phase25.mode(seq(list("AABB")))[0] == "B"


def test_fy_weighted():
    o = seq(list("AAAB"), fys=[2019, 2019, 2019, 2023])
    w = phase25.FY_WEIGHTS["fy_w(1/.7/.5/.3)"]
    assert phase25.fy_weighted(o, 2024, w) == "B"  # A: 0.9, B: 1.0


def test_rule_conditions():
    o = seq(list("AABBB"), fys=[2021, 2021, 2023, 2023, 2023], cx=[0, 0, 0, 0, 1])
    assert Rule(3, 3, "any", False, False).applies(o, 2024) == "B"
    assert Rule(3, 3, "any", False, True).applies(o, 2024) is None  # 全期間100%ではない
    assert Rule(3, 3, "any", True, False).applies(o, 2024) is None  # 直近に complex
    assert Rule(3, 3, "prev_fy", False, False).applies(o, 2025) is None  # 前年度に使用していない
    assert Rule(3, 10, "any", False, False).applies(o, 2024) is None  # 総件数不足


def test_histories_exclude_target_and_future():
    rows = [{"transaction_id": str(i), "fiscal_year": fy, "tx_date": f"{fy}-09-01", "tx_content": "X", "label_account": "A"} for i, fy in enumerate((2022, 2024, 2025))]
    assert [o.fy for o in phase25.histories(rows, 2024)["X"]] == [2022]


def test_rolling_pairs_use_only_earlier_dates():
    rows = [
        {"transaction_id": "a", "fiscal_year": 2023, "tx_date": "2023-09-01", "tx_content": "X", "label_account": "A"},
        {"transaction_id": "b", "fiscal_year": 2024, "tx_date": "2024-09-01", "tx_content": "X", "label_account": "B"},
        {"transaction_id": "c", "fiscal_year": 2024, "tx_date": "2024-09-01", "tx_content": "X", "label_account": "B"},  # 同日
        {"transaction_id": "d", "fiscal_year": 2024, "tx_date": "2024-10-01", "tx_content": "X", "label_account": "B"},
        {"transaction_id": "e", "fiscal_year": 2025, "tx_date": "2025-09-01", "tx_content": "X", "label_account": "Z"},  # 未来
    ]
    pairs = {r["transaction_id"]: o for r, o in phase25.rolling_pairs(rows, 2024)}
    assert [x.label for x in pairs["b"]] == ["A"] and [x.label for x in pairs["c"]] == ["A"]  # 同日は使わない
    assert [x.label for x in pairs["d"]] == ["A", "B", "B"]
    assert "e" not in pairs
    yb = {r["transaction_id"]: o for r, o in phase25.year_block_pairs(rows, 2024)}
    assert [x.label for x in yb["d"]] == ["A"]
