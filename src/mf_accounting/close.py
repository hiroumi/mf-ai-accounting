"""決算処理（FY2025〜）: 連携明細の対象抽出・ルール判定・Sonnet 対象選定・レビュー CSV 生成。

Phase 4.5 で検証した判定（phase3 / phase4 / routing と同じ正規化・層・閾値）を、正解ラベルのない年度に適用する。
- 履歴は対象年度より前の年度（仕訳が紐付いた明細）のみ。対象年度の仕訳・正解・未来情報は使わない
- MF への書き込みは行わない。LLM 呼び出しは既存の `llm-run` を使う

再現のための条件（口座系統・Amazon 除外・routing）はすべてこのファイルの定数で明示する。
"""

import unicodedata
from collections import Counter

from rapidfuzz import fuzz, process

from . import phase3, phase4

# ---- 口座系統（account lineage） -----------------------------------------------------
# 勘定科目推定上、同じ利用系統として扱う連携口座。
# キー: 系統名、値: MF の連携サービス名（connected_accounts[].name）。その配下の全サブ口座を1系統にまとめる。
# 楽天カード: カード更新（4004 → 9023 → 2676）、再登録（【利用不可】9023）、追加 Visa（4235）を含む（2026-09-29 ユーザー確認）
ACCOUNT_LINEAGES = {"楽天カード": "楽天カード"}

# ---- Amazon の除外 ---------------------------------------------------------------------
AMAZON_CONNECTED_ACCOUNT = "Amazon.co.jp"  # Amazon 連携の明細はユーザーが MF 画面で手動処理する
AMAZON_DUP_KEYWORDS = ("AMAZON", "アマゾン")  # カード明細の摘要（NFKC + 大文字）にこれを含むものを重複確認候補にする
AMAZON_DUP_EXCLUDE = ("AMAZON WEB SERVICES",)  # AWS は Amazon ショッピングではないため通常の AI 対象

# ---- routing（FY2023 で事前登録した Balanced。FY2025 の結果を見て変更しない） ---------------
SONNET_LAYERS = ("A", "B", "C", "C+D", "D")  # E（exact・過去1〜2回）は人間確認
SONNET_MIN_CONF = 0.90  # かつ needs_review=false, insufficient_information=false, 科目あり

RULE_CANDIDATE = "rule_candidate"
SONNET_AUTO = "sonnet_auto_candidate"
HUMAN = "human_review"


def lineage_map(connected: list[dict]) -> dict[str, str]:
    """connected_sub_account_id → 系統キー（系統に属さない口座は自身の ID）。"""
    out = {}
    for a in connected:
        lin = next((k for k, v in ACCOUNT_LINEAGES.items() if v == a.get("name")), None)
        for s in a.get("connected_sub_accounts") or []:
            out[s["id"]] = f"lineage:{lin}" if lin else s["id"]
    return out


def is_amazon_dup(content: str | None, is_card: bool) -> bool:
    c = unicodedata.normalize("NFKC", content or "").upper()
    return is_card and any(k in c for k in AMAZON_DUP_KEYWORDS) and not any(k in c for k in AMAZON_DUP_EXCLUDE)


def static_predictions(history: list[dict], targets: list[dict]) -> list[dict]:
    """phase3.rolling_predictions と同じ raw exact → normalized exact → fuzzy(top1) を、固定の過去履歴に対して行う。

    targets は正解のない明細（tx_content, tx_date 等）。対象同士は履歴に加えない。
    """
    f = phase3.NORMALIZERS[phase4.NORMALIZER]
    raw_h: dict[str, list[dict]] = {}
    norm_h: dict[str, list[dict]] = {}
    for r in sorted(history, key=lambda r: (r["tx_date"], r["transaction_id"])):
        raw_h.setdefault(r["tx_content"].strip(), []).append(r)
        norm_h.setdefault(f(r["tx_content"]), []).append(r)
    keys = [k for k in norm_h if k]
    out = []
    for t in targets:
        raw, nk = (t["tx_content"] or "").strip(), f(t["tx_content"] or "")
        stage, score, rs = "none", None, None
        if raw in raw_h:
            stage, score, rs = "raw_exact", 100.0, raw_h[raw]
        elif nk in norm_h:
            stage, score, rs = "norm_exact", 100.0, norm_h[nk]
        elif keys and nk:
            top = process.extract(nk, keys, scorer=fuzz.ratio, limit=1)
            if top:
                stage, score, rs = "fuzzy", float(top[0][1]), norm_h[top[0][0]]
        p = {"transaction_id": t["transaction_id"], "stage": stage, "score": score, "pred": None, "exact_history": []}
        if rs:
            h = phase3.Hist()
            for x in rs:
                h.add(x)
            p.update({"pred": h.mode(), "hist_n": len(h.labels), "hist_agreement": round(h.agreement(), 4),
                      "days_since_last": phase3._days(h.dates[-1], t["tx_date"])})
            if stage != "fuzzy":
                p["exact_history"] = rs
        out.append(p)
    return out


def classify(targets: list[dict], history: list[dict], lineage: dict[str, str]) -> list[dict]:
    """各明細に層（H/A/B/C/C+D/D/E）・ルール予測・account_seen（系統統合後）・LLM 前の routing を付ける。"""
    preds = {p["transaction_id"]: p for p in static_predictions(history, targets)}
    key = lambda sid: lineage.get(sid, sid)
    out = []
    for t in targets:
        p = preds[t["transaction_id"]]
        c = phase4.classify_target(p)
        layer = "+".join(x[0] for x in c["categories"]) if c["categories"] else "H"
        eh = p.pop("exact_history")
        seen = None if not eh else key(t["connected_sub_account_id"]) in {key(x["connected_sub_account_id"]) for x in eh}
        if c["high_confidence"]:
            routing = RULE_CANDIDATE if seen else HUMAN
        elif layer in SONNET_LAYERS:
            routing = "sonnet"
        else:
            routing = HUMAN
        out.append({**t, **{k: v for k, v in p.items() if k != "transaction_id"}, "layer": layer,
                    "high_confidence": c["high_confidence"], "account_seen": seen, "routing": routing})
    return out


def route_sonnet(output: dict | None) -> str:
    o = output or {}
    ok = (o.get("primary_account_id") and (o.get("confidence") or 0) >= SONNET_MIN_CONF
          and o.get("needs_review") is False and o.get("insufficient_information") is False)
    return SONNET_AUTO if ok else HUMAN


def conf_band(c: float | None) -> str:
    if c is None:
        return "なし"
    for lo, label in ((0.95, "0.95+"), (0.90, "0.90–0.94"), (0.80, "0.80–0.89"), (0.70, "0.70–0.79"), (0.50, "0.50–0.69")):
        if c >= lo:
            return label
    return "<0.50"


def review_rows(items: list[dict], results: dict, names: dict, subnames: dict) -> list[dict]:
    """人間確認用の行。同じ routing・推定科目・正規化摘要がまとまる順に並べ、review_group を振る。"""
    f = phase3.NORMALIZERS[phase4.NORMALIZER]
    rows = []
    for it in items:
        o = (results.get(it["transaction_id"]) or {}).get("output") or {}
        if it["routing"] == "sonnet":
            routing, source, acct = route_sonnet(o), "sonnet", o.get("primary_account_id")
        else:
            routing, acct = it["routing"], it.get("pred")
            source = "rule" if routing == RULE_CANDIDATE else ("rule_reference" if acct else "none")
        rows.append({
            "transaction_id": it["transaction_id"],
            "transaction_date": it["tx_date"],
            "transaction_content": it["tx_content"],
            "amount": it["tx_value"],
            "side": it["tx_side"],
            "connected_account": subnames.get(it["connected_sub_account_id"], ""),
            "proposed_primary_account": names.get(acct, "") if acct else "",
            "proposed_primary_account_id": acct or "",
            "inference_source": source,
            "confidence": o.get("confidence", "") if source == "sonnet" else "",
            "reason_category": o.get("reason_category", "") if source == "sonnet" else "",
            "reason": o.get("reason", "") if source == "sonnet" else "",
            "needs_review": o.get("needs_review", "") if source == "sonnet" else "",
            "layer": it["layer"],
            "history_n": it.get("hist_n", ""),
            "account_seen": "" if it["account_seen"] is None else it["account_seen"],
            "routing": routing,
            "human_decision": "",
            "human_correction_account": "",
            "note": "",
            "_nk": f(it["tx_content"] or ""),
        })
    order = {RULE_CANDIDATE: 0, SONNET_AUTO: 1, HUMAN: 2}
    rows.sort(key=lambda r: (order[r["routing"]], r["proposed_primary_account"], r["_nk"], r["transaction_date"], r["transaction_id"]))
    groups: dict = {}
    for r in rows:
        g = groups.setdefault((r["routing"], r["proposed_primary_account_id"], r.pop("_nk")), len(groups) + 1)
        r["review_group"] = g
    size = Counter(r["review_group"] for r in rows)
    for r in rows:
        r["group_size"] = size[r["review_group"]]
    return rows


REVIEW_AI_COLUMNS = ["review_group", "group_size", "routing", "transaction_date", "transaction_content", "amount", "side",
                     "connected_account", "proposed_primary_account", "inference_source", "confidence", "reason_category", "reason",
                     "needs_review", "layer", "history_n", "account_seen", "human_decision", "human_correction_account", "note",
                     "proposed_primary_account_id", "transaction_id"]
REVIEW_AMAZON_COLUMNS = ["transaction_date", "transaction_content", "amount", "side", "connected_account",
                         "amazon_linked_same_amount_within_7d", "human_decision", "human_correction_account", "note", "transaction_id"]
