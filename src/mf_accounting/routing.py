"""Phase 4.5: 誤判定の原因分析とルーティング設計（LLM API は呼ばない）。

特徴量はすべて「その明細の取引日より前」の履歴のみから計算する（未来情報を使わない）。
正解ラベルは分析（誤りの比較）にのみ使い、ルーティング条件には使わない。
"""

from collections import Counter
from datetime import date

from . import phase3
from .phase4 import NORMALIZER, amount_band

LABEL = "primary_account_id"


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(b[:10]) - date.fromisoformat(a[:10])).days


def history_features(rows: list[dict], target_fy: int) -> dict[str, dict]:
    """評価年度の各明細について、同一 content（raw → 正規化）の過去履歴から特徴量を作る。"""
    f_norm = phase3.NORMALIZERS[NORMALIZER]
    ordered = sorted((r for r in rows if r.get("tx_content") and r.get(LABEL) and r["fiscal_year"] is not None and r["fiscal_year"] <= target_fy),
                     key=lambda r: (r["tx_date"], r["transaction_id"]))
    raw_hist: dict[str, list[dict]] = {}
    norm_hist: dict[str, list[dict]] = {}
    out: dict[str, dict] = {}
    i = 0
    while i < len(ordered):
        day = ordered[i]["tx_date"]
        batch = []
        while i < len(ordered) and ordered[i]["tx_date"] == day:
            batch.append(ordered[i])
            i += 1
        for r in batch:
            if r["fiscal_year"] != target_fy:
                continue
            raw, nk = r["tx_content"].strip(), f_norm(r["tx_content"])
            h = raw_hist.get(raw) or norm_hist.get(nk) or []
            out[r["transaction_id"]] = _features(r, h)
        for r in batch:
            raw_hist.setdefault(r["tx_content"].strip(), []).append(r)
            norm_hist.setdefault(f_norm(r["tx_content"]), []).append(r)
    return out


def _tail_same(labels: list[str], k: int) -> bool | None:
    return None if len(labels) < k else len(set(labels[-k:])) == 1


def _features(r: dict, h: list[dict]) -> dict:
    band = amount_band(r.get("tx_value"))
    f = {"hist_n": len(h), "tx_band": band, "account_kind_bank": None}
    if not h:
        return f
    labels = [x[LABEL] for x in h]
    by_fy: dict = {}
    for x in h:
        by_fy.setdefault(x["fiscal_year"], Counter())[x[LABEL]] += 1
    fy_modes = [c.most_common(1)[0][0] for _, c in sorted(by_fy.items())]
    f.update({
        "days_since_last": _days(h[-1]["tx_date"], r["tx_date"]),
        "hist_span_days": _days(h[0]["tx_date"], h[-1]["tx_date"]),
        "distinct_labels": len(set(labels)),
        "last_label": labels[-1],
        "tail2_same": _tail_same(labels, 2),
        "tail3_same": _tail_same(labels, 3),
        "tail5_same": _tail_same(labels, 5),
        "band_seen": band in {amount_band(x.get("tx_value")) for x in h},
        "band_same_as_last": band == amount_band(h[-1].get("tx_value")),
        "side_seen": r.get("tx_side") in {x.get("tx_side") for x in h},
        "account_seen": r.get("connected_sub_account_id") in {x.get("connected_sub_account_id") for x in h},
        "account_same_as_last": r.get("connected_sub_account_id") == h[-1].get("connected_sub_account_id"),
        "past_complex": any(x.get("complex_journal") for x in h),
        "fy_label_change": len(set(fy_modes)) > 1,
        "hist_fy_count": len(by_fy),
    })
    return f


def build_items(rows: list[dict], rules: list[dict], results: dict, accounts_by_id: dict, target_fy: int = 2024) -> list[dict]:
    """パイプライン評価の各明細に、ルール・LLM・履歴特徴量をまとめる。"""
    feats = history_features(rows, target_fy)
    preds = {p["transaction_id"]: p for p in phase3.rolling_predictions(rows, target_fy, NORMALIZER, "ratio")}
    row_by_tx = {r["transaction_id"]: r for r in rows}
    items = []
    for r in rules:
        tid = r["transaction_id"]
        o = results.get(tid) or {}
        p = preds[tid]
        row = row_by_tx[tid]
        bank = accounts_by_id.get(row.get("bank_account_id")) or {}
        items.append({
            **r, **feats.get(tid, {}),
            "layer": "+".join(c[0] for c in r["categories"]) if r["categories"] else "H",
            "stage": p["stage"], "score": p.get("score"), "rule_agreement": p.get("hist_agreement"),
            "account_kind": "bank" if bank.get("category") == "CASH_AND_DEPOSITS" else "card_or_liability" if bank.get("account_group") == "LIABILITY" else "other",
            "llm": o.get("primary_account_id"), "conf": o.get("confidence"), "nr": o.get("needs_review"),
            "insuff": o.get("insufficient_information"), "rcat": o.get("reason_category"),
            "rule_valid": r["rule_pred"] is not None and "A_候補なし" not in r["categories"],
        })
    for it in items:
        it["rule_ok"] = it["rule_valid"] and it["rule_pred"] == it["actual"]
        it["llm_ok"] = it["llm"] is not None and it["llm"] == it["actual"]
        it["agree"] = it["rule_valid"] and it["llm"] is not None and it["rule_pred"] == it["llm"]
    return items


def route(items: list[dict], decide) -> dict:
    """decide(item) -> ("rule" | "llm" | "human")。未来情報を使う条件を decide に入れないこと。"""
    auto = ok = 0
    by = Counter()
    for it in items:
        d = decide(it)
        by[d] += 1
        if d == "rule":
            auto += 1
            ok += it["rule_ok"]
        elif d == "llm":
            auto += 1
            ok += it["llm_ok"]
    n = len(items)
    return {"n": n, "auto": auto, "coverage": auto / n, "accuracy": ok / auto if auto else None, "errors": auto - ok,
            "human": n - auto, "by": dict(by)}


def plan(llm_min_conf: float):
    """候補ルーティング（実行時に判定可能な条件のみ）。

    - 高confidenceルール（exact・3回以上・一致率100%・365日以内）は、今回の口座が過去履歴にある場合のみ自動
    - LLM は confidence >= llm_min_conf かつ needs_review=false、ルール予測と不一致でなく、今回の口座が過去履歴にない場合は除外
    - それ以外は人間確認
    """
    def decide(i: dict) -> str:
        if i["high_confidence"]:
            return "rule" if i.get("account_seen") else "human"
        if i["llm"] is None or i["conf"] < llm_min_conf or i["nr"] is not False:
            return "human"
        if i["rule_valid"] and not i["agree"]:
            return "human"
        if i.get("account_seen") is False:
            return "human"
        return "llm"
    return decide


PLANS = {"Aggressive": 0.80, "Balanced": 0.90, "Conservative": 0.95}
