"""決算処理（FY2025〜）: 連携明細の対象抽出・ルール判定・Sonnet 対象選定・レビュー CSV 生成。

Phase 4.5 で検証した判定（phase3 / phase4 / routing と同じ正規化・層・閾値）を、正解ラベルのない年度に適用する。
- 履歴は対象年度より前の年度（仕訳が紐付いた明細）のみ。対象年度の仕訳・正解・未来情報は使わない
- MF への書き込みは行わない。LLM 呼び出しは既存の `llm-run` を使う

再現のための条件（口座系統・Amazon 除外・routing）はすべてこのファイルの定数で明示する。
"""

import re
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


# ---- グループ単位レビュー（review_groups.csv） -------------------------------------------
# カード会社が付ける表記ゆれ（返済方法変更の注記・国内利用の接頭辞・NFC）を除いた「加盟店キー」でまとめる。
_MERCHANT_NOISE = [re.compile(p) for p in (r"^マスター国内利用\s*MZZ\s*", r"\s*\(ヘンサイヘンコウ\s*$", r"/NFC(?=\(|$)")]
_TRUNCATED = re.compile(r"\(ヘンサイヘンコウ\s*$")  # この注記が付くと加盟店名の末尾が切れる
DECISIONS = ("approve", "correct", "split", "hold")  # split: members の行単位で判断する
GROUP_ORDER = (HUMAN, SONNET_AUTO, RULE_CANDIDATE)  # 人間確認を先頭に


def merchant_key(content: str) -> str:
    c = unicodedata.normalize("NFKC", content or "")
    for p in _MERCHANT_NOISE:
        c = p.sub("", c)
    return phase3.NORMALIZERS[phase4.NORMALIZER](c)


def merchant_keys(contents: list[str]) -> dict[str, str]:
    """content → 加盟店キー。返済変更の注記で末尾が切れた名前は、同じ接頭辞の完全な名前に寄せる（一意に決まる場合のみ）。"""
    base = {c: merchant_key(c) for c in contents}
    full = {k for c, k in base.items() if not _TRUNCATED.search(unicodedata.normalize("NFKC", c))}
    out = {}
    for c, k in base.items():
        if _TRUNCATED.search(unicodedata.normalize("NFKC", c)) and k not in full and len(k) >= 4:
            longer = sorted(f for f in full if f.startswith(k))
            k = longer[0] if len(longer) == 1 else k
        out[c] = k
    return out


def past_summary(cands: list[dict]) -> tuple[int, str]:
    """過去候補（phase4.candidates_before）→（exact の過去件数, 表示用の過去科目）。"""
    fmt = lambda c: " / ".join(f"{a['name']}×{a['count']}" for a in c["accounts"])
    exact = next((c for c in cands if c["similarity"] == 100), None)
    if exact:
        return exact["occurrences"], f"exact: {fmt(exact)}"
    if cands:
        c = cands[0]
        return 0, f"類似{c['similarity']}「{c['content']}」: {fmt(c)}"
    return 0, ""


def _range(vals: list, fmt=str) -> str:
    vals = [v for v in vals if v not in ("", None)]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    return fmt(lo) if lo == hi else f"{fmt(lo)}–{fmt(hi)}"


def review_groups(rows: list[dict], past: dict[str, tuple[int, str]]) -> tuple[list[dict], list[dict]]:
    """review_rows の行（1明細）→（1行=1グループ, グループ⇔明細の対応）。

    グループ = routing × 加盟店キー × 推定科目。並び順: routing（人間確認が先）→ 推定科目（件数の多い科目から）
    → グループ内 confidence 最大の降順 → 加盟店キー。
    """
    mk = merchant_keys([r["transaction_content"] for r in rows])
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["routing"], mk[r["transaction_content"]], r["proposed_primary_account_id"]), []).append(r)
    acct_size = Counter()
    for (routing, _, acct), rs in groups.items():
        acct_size[(routing, acct)] += len(rs)
    conf = lambda rs: max((float(r["confidence"]) for r in rs if r["confidence"] != ""), default=-1.0)
    keys = sorted(groups, key=lambda k: (GROUP_ORDER.index(k[0]), -acct_size[(k[0], k[2])], k[2], -conf(groups[k]), k[1]))
    per_merchant = Counter((k[0], k[1]) for k in keys)
    out, members = [], []
    for n, k in enumerate(keys, start=1):
        rs = sorted(groups[k], key=lambda r: (r["transaction_date"], r["transaction_id"]))
        gid = f"G{n:04d}"
        rep = max(rs, key=lambda r: (float(r["confidence"]) if r["confidence"] != "" else -1.0, r["transaction_date"]))
        descs = Counter(r["transaction_content"] for r in rs)
        pm = [past[r["transaction_id"]] for r in rs if r["transaction_id"] in past]
        rep_past = past.get(rep["transaction_id"], (0, ""))
        out.append({
            "review_group": gid,
            "group_size": len(rs),
            "routing": k[0],
            "representative_description": rep["transaction_content"],
            "descriptions": " | ".join(f"{d}×{c}" if c > 1 else d for d, c in descs.most_common()),
            "date_range": _range([r["transaction_date"] for r in rs]),
            "amount_range": _range([int(r["amount"]) for r in rs], lambda v: f"{v:,}"),
            "amount_total": sum(int(r["amount"]) for r in rs),
            "side": "/".join(sorted({r["side"] for r in rs})),
            "connected_account": " | ".join(sorted({r["connected_account"].split(" / ")[-1] for r in rs})),
            "proposed_account": rep["proposed_primary_account"],
            "inference_source": "/".join(sorted({r["inference_source"] for r in rs})),
            "confidence_range": _range([float(r["confidence"]) for r in rs if r["confidence"] != ""], lambda v: f"{v:.2f}"),
            "confidence_max": "" if conf(rs) < 0 else conf(rs),
            "layer": "/".join(sorted({r["layer"] for r in rs})),
            "past_match_count": max((p[0] for p in pm), default=0),
            "past_accounts": rep_past[1],
            "reason": rep["reason"],
            "reason_category": rep["reason_category"],
            "needs_review_count": sum(1 for r in rs if r["needs_review"] is True),
            "same_merchant_other_groups": per_merchant[(k[0], k[1])] - 1,
            "human_decision": "",
            "human_correction_account": "",
            "note": "",
            "proposed_primary_account_id": k[2],
            "review_ai_groups": " ".join(str(g) for g in sorted({r["review_group"] for r in rs})),
        })
        for r in rs:
            members.append({"review_group": gid, "transaction_id": r["transaction_id"], "transaction_date": r["transaction_date"],
                            "transaction_content": r["transaction_content"], "amount": r["amount"], "side": r["side"],
                            "connected_account": r["connected_account"], "proposed_primary_account": r["proposed_primary_account"],
                            "confidence": r["confidence"], "review_ai_group": r["review_group"],
                            "row_decision_override": "", "row_correction_account": "", "row_note": ""})
    return out, members


REVIEW_GROUP_COLUMNS = ["review_group", "group_size", "routing", "representative_description", "descriptions", "date_range",
                        "amount_range", "amount_total", "side", "connected_account", "proposed_account", "inference_source",
                        "confidence_range", "confidence_max", "layer", "past_match_count", "past_accounts", "reason", "reason_category",
                        "needs_review_count", "same_merchant_other_groups", "human_decision", "human_correction_account", "note",
                        "proposed_primary_account_id", "review_ai_groups"]
REVIEW_MEMBER_COLUMNS = ["review_group", "transaction_id", "transaction_date", "transaction_content", "amount", "side",
                         "connected_account", "proposed_primary_account", "confidence", "review_ai_group",
                         "row_decision_override", "row_correction_account", "row_note"]


# ---- レビュー判断の展開（group → transaction） ------------------------------------------
CONFIRMED, UNDECIDED, HELD, INVALID = "confirmed", "undecided", "hold", "invalid"


def expand_decisions(groups: list[dict], members: list[dict], account_ids: dict[str, str]) -> list[dict]:
    """review_groups.csv / review_group_members.csv の判断を明細単位に展開する。

    優先順位: 行の row_decision_override（approve / correct / hold）> グループの human_decision。
    approve → 推定科目、correct → 修正科目（科目名または ID）、split → 行の判断が必要、hold・空欄 → 未確定。
    account_ids: 科目名 → ID（利用可能な科目のみ）。ID そのものも受け付ける。
    """
    gby = {g["review_group"]: g for g in groups}
    valid_ids = set(account_ids.values())
    resolve = lambda v: account_ids.get(v.strip()) or (v.strip() if v.strip() in valid_ids else None)
    out = []
    for m in members:
        g = gby.get(m["review_group"]) or {}
        gd, rd = (g.get("human_decision") or "").strip().lower(), (m.get("row_decision_override") or "").strip().lower()
        if rd:
            decision, corr, source = rd, m.get("row_correction_account") or "", "row"
        else:
            decision, corr, source = gd, g.get("human_correction_account") or "", "group"
        status, acct, problem = UNDECIDED, None, ""
        if decision == "approve":
            acct = g.get("proposed_primary_account_id") or None
            status, problem = (CONFIRMED, "") if acct else (INVALID, "approve だが推定科目がない")
        elif decision == "correct":
            acct = resolve(corr)
            status, problem = (CONFIRMED, "") if acct else (INVALID, f"修正科目を解決できない: {corr!r}")
        elif decision == "hold":
            status = HELD
        elif decision == "split":
            problem = "split: 行の row_decision_override が未記入" if source == "group" else "split は行では使えない"
            status = UNDECIDED if source == "group" else INVALID
        elif decision:
            status, problem = INVALID, f"不明な判断: {decision!r}"
        out.append({**m, "group_decision": gd, "applied_decision": decision, "decision_source": source if decision else "",
                    "final_status": status, "final_account_id": acct or "", "problem": problem})
    return out


# ---- 資金源（funding source）と最終仕訳構造 -------------------------------------------------
# 勘定科目推定（ACCOUNT_LINEAGES）とは独立。仕訳構造（未払金 → 長期借入金の振替の有無）だけに使う。
# (連携サービス名, サブ口座名に含まれる文字列（大文字小文字無視, None=全サブ口座）) → 資金源。上から順に最初に一致したもの。
# 一致しない口座は "unconfirmed"（仕訳を作らず確認待ち）。カード種別から自動で推測しない。
FOUNDER_PERSONAL_CARD, CORPORATE_CARD, BANK, OTHER, UNCONFIRMED = "founder_personal_card", "corporate_card", "bank", "other", "unconfirmed"
FUNDING_SOURCES = [
    ("楽天カード", "mastercard", FOUNDER_PERSONAL_CARD),  # 4004 / 9023 / 2676 / 【利用不可】（創業者個人カードの更新系列）
    ("楽天カード", "(visa) xxxx - xxxx - xxxx - 4235", FOUNDER_PERSONAL_CARD),  # 追加 Visa。個人カード（2026-09-29 ユーザー確認）
    ("アメリカン・エキスプレスカード", "デルタ", FOUNDER_PERSONAL_CARD),  # 創業者個人カード（2026-09-29 ユーザー確認）
    # 法人カード（FY2025 より後に導入）は、連携口座が確定したら CORPORATE_CARD として口座単位で追加する
    ("アメリカン・エキスプレスカード", "ポイント", OTHER),
    ("GMOあおぞら", None, BANK),
    ("【法人】三井住友銀行", None, BANK),
    ("【法人】ゆうちょ銀行（ゆうちょダイレクト）", None, BANK),
    (AMAZON_CONNECTED_ACCOUNT, None, OTHER),
]
FOUNDER_LOAN_ACCOUNT = "長期借入金"  # 創業者からの借入（個人カードの立替分）


def funding_source(connected_name: str, sub_name: str) -> str:
    for name, part, src in FUNDING_SOURCES:
        if name == connected_name and (part is None or part.lower() in (sub_name or "").lower()):
            return src
    return UNCONFIRMED


def _line(acct: tuple, value: int) -> dict:
    return {"account_id": acct[0], "sub_account_id": acct[1], "value": value}


def journal_branches(side: str, value: int, primary: str, source_acct: tuple, funding: str, loan_account: str,
                     primary_sub: str | None = None) -> list[dict]:
    """1明細の仕訳（branches: debitor / creditor）。source_acct = 連携口座の (account_id, sub_account_id)。

    個人カード: 費用/未払金(カード) + 未払金(カード)/長期借入金 → 未払金は同じサブ口座で相殺され、実質 費用/長期借入金。
    入金（返金等）は同じ構造の貸借逆。法人カード・銀行は振替なし。
    """
    p = (primary, primary_sub)
    first = {"debitor": _line(p, value), "creditor": _line(source_acct, value)}
    if side == "INCOME":
        first = {"debitor": _line(source_acct, value), "creditor": _line(p, value)}
    branches = [first]
    if funding == FOUNDER_PERSONAL_CARD:
        loan = (loan_account, None)
        tr = {"debitor": _line(source_acct, value), "creditor": _line(loan, value)}
        if side == "INCOME":
            tr = {"debitor": _line(loan, value), "creditor": _line(source_acct, value)}
        branches.append(tr)
    return branches


def reconcile(journals: list[dict], liability_account: str, loan_account: str) -> dict:
    """journals: [{funding, side, value, branches}]。資金源別の件数・金額と、未払金・長期借入金の増減を検算する。"""
    def tot(js, acct, side):
        return sum(b[side]["value"] for j in js for b in j["branches"] if b[side]["account_id"] == acct)
    out = {}
    for src in sorted({j["funding"] for j in journals}):
        js = [j for j in journals if j["funding"] == src]
        use, ref = [j for j in js if j["side"] == "EXPENSE"], [j for j in js if j["side"] == "INCOME"]
        kind = ("withdrawal", "deposit") if src == BANK else ("usage", "refund")  # カードの入金は返金・取消等
        r = {"count": len(js), f"{kind[0]}_count": len(use), f"{kind[0]}_total": sum(j["value"] for j in use),
             f"{kind[1]}_count": len(ref), f"{kind[1]}_total": sum(j["value"] for j in ref)}
        if src != BANK:
            ap_dr, ap_cr = tot(js, liability_account, "debitor"), tot(js, liability_account, "creditor")
            ln_cr, ln_dr = tot(js, loan_account, "creditor"), tot(js, loan_account, "debitor")
            per_tx_ap_zero = all(sum(b["creditor"]["value"] for b in j["branches"] if b["creditor"]["account_id"] == liability_account)
                                 == sum(b["debitor"]["value"] for b in j["branches"] if b["debitor"]["account_id"] == liability_account) for j in js)
            r.update({"loan_increase": ln_cr, "loan_decrease": ln_dr, "loan_net": ln_cr - ln_dr,
                      "payable_debit_total": ap_dr, "payable_credit_total": ap_cr, "payable_net": ap_cr - ap_dr,
                      "payable_net_zero_every_transaction": per_tx_ap_zero,
                      "loan_net_equals_usage_minus_refund": (ln_cr - ln_dr) == r["usage_total"] - r["refund_total"] if src == FOUNDER_PERSONAL_CARD else None})
        out[src] = r
    return out


# ---- 最終レビュー結果（assistant_* 列）の反映と仕訳検証 --------------------------------------
EXCLUDED = "excluded"
AUTO_ROUTINGS = (RULE_CANDIDATE, SONNET_AUTO)


def final_decision(group: dict, account_ids: dict[str, str]) -> tuple[str, str, str]:
    """assistant_recommendation → (status, account_id, problem)。human_decision 列は使わない（上書きもしない）。

    approve → 推定科目 / correct → assistant_correction_account / exclude → 仕訳対象外
    existing_auto_candidate → rule_candidate・sonnet_auto_candidate の既存自動判定（推定科目）
    """
    rec = (group.get("assistant_recommendation") or "").strip()
    proposed = group.get("proposed_primary_account_id") or ""
    if rec == "exclude":
        return EXCLUDED, "", ""
    if rec == "approve" or (rec == "existing_auto_candidate" and group.get("routing") in AUTO_ROUTINGS):
        return (CONFIRMED, proposed, "") if proposed else (INVALID, "", f"{rec} だが推定科目がない")
    if rec == "correct":
        name = (group.get("assistant_correction_account") or "").strip()
        return (CONFIRMED, account_ids[name], "") if name in account_ids else (INVALID, "", f"修正科目を解決できない: {name!r}")
    if rec == "existing_auto_candidate":
        return INVALID, "", f"existing_auto_candidate だが routing が {group.get('routing')}"
    return (UNDECIDED if rec == "ask_hiro" else INVALID), "", f"assistant_recommendation={rec!r}"


def validate_final(transactions: list[dict], journals: list[dict], fy_start: str, fy_end: str, payable: str) -> list[dict]:
    """transactions: 明細ごとの最終状態（status, journal_status, funding_source 等）。journals: {transaction_id, date, funding, branches}。"""
    checks = []
    def add(check, level, bad, detail=""):
        checks.append({"check": check, "level": level, "result": "fail" if bad else "pass", "count": len(bad),
                       "detail": detail, "transaction_ids": " ".join(sorted(bad))[:2000]})
    lines = [(j, b, s) for j in journals for b in j["branches"] for s in ("debitor", "creditor")]
    dr = sum(b["debitor"]["value"] for j in journals for b in j["branches"])
    cr = sum(b["creditor"]["value"] for j in journals for b in j["branches"])
    add("借方合計 = 貸方合計（全体・仕訳ごと）", "error",
        {j["transaction_id"] for j in journals if sum(b["debitor"]["value"] for b in j["branches"]) != sum(b["creditor"]["value"] for b in j["branches"])}
        | ({"TOTAL"} if dr != cr else set()), f"借方 {dr:,} / 貸方 {cr:,}")
    add("金額0の仕訳行がない", "error", {j["transaction_id"] for j, b, s in lines if not b[s]["value"] or b[s]["value"] <= 0})
    excluded = {t["transaction_id"] for t in transactions if t["status"] in (EXCLUDED, MERGED)}
    add("exclude・統合済み明細が仕訳に含まれていない", "error", {j["transaction_id"] for j in journals} & excluded)
    pending = [t for t in transactions if t["status"] not in (EXCLUDED, MERGED) and t["journal_status"] != "generated"]
    add("未確定科目がない（仕訳対象はすべて仕訳化）", "error", {t["transaction_id"] for t in pending},
        "; ".join(sorted({f"{t['journal_status']}: {t['problem']}" if t["problem"] else t["journal_status"] for t in pending})))
    jids = {j["transaction_id"] for j in journals}
    add("統合済み明細の統合先に仕訳がある", "error",
        {t["transaction_id"] for t in transactions if t["status"] == MERGED and t.get("merged_into") not in jids})
    add("ask_hiro が0件", "error", {t["transaction_id"] for t in transactions if t["recommendation"] == "ask_hiro"})
    bad_ap = set()
    for j in journals:
        if j["funding"] != FOUNDER_PERSONAL_CARD:
            continue
        net = Counter()
        for b in j["branches"]:
            if b["debitor"]["account_id"] == payable:
                net[b["debitor"]["sub_account_id"]] += b["debitor"]["value"]
            if b["creditor"]["account_id"] == payable:
                net[b["creditor"]["sub_account_id"]] -= b["creditor"]["value"]
        if any(net.values()) or None in net or not net:
            bad_ap.add(j["transaction_id"])
    add("個人カードの未払金が同じカード補助科目・同額で相殺", "error", bad_ap)
    add("FY期間外の取引がない", "error", {j["transaction_id"] for j in journals if not fy_start <= j["date"] <= fy_end}, f"{fy_start}〜{fy_end}")
    ids = Counter(j["transaction_id"] for j in journals)
    add("同一取引の重複仕訳がない", "error", {k for k, v in ids.items() if v > 1})
    return checks


# ---- 最終レビュー後の明細単位の上書き（final_overrides.csv） ----------------------------------
# action: exclude（帳簿対象外）/ set_counter_sub_account（主科目側の補助科目, 例: 口座間振替の相手口座）/
#         merged_into（口座間振替の片側。仕訳は target_transaction_id 側の1仕訳に統合）/
#         confirm_refund（自動判定のカード返金を人間が確認済み。refund_review を解除し、購入時と逆の仕訳を作る）/
#         set_account（主科目を人間の確認結果で置き換える。例: 創業者個人口座への返済 → 長期借入金）
MERGED = "merged"
OVERRIDE_ACTIONS = ("exclude", "set_counter_sub_account", "merged_into", "confirm_refund", "set_account")


def load_overrides(rows: list[dict], sub_ids: dict[str, str], known_tx: set[str], account_ids: dict[str, str] | None = None) -> dict[str, dict]:
    """final_overrides.csv の行 → transaction_id ごとの上書き。補助科目は名前で指定し ID に解決する。不正な行は ValueError。"""
    out = {}
    for r in rows:
        tid, action = r["transaction_id"].strip(), r["action"].strip()
        if tid not in known_tx:
            raise ValueError(f"overrides: 未知の transaction_id {tid}")
        if action not in OVERRIDE_ACTIONS:
            raise ValueError(f"overrides: 不明な action {action!r}")
        if tid in out:
            raise ValueError(f"overrides: transaction_id が重複 {tid}")
        o = {"action": action, "reason": r.get("reason", "")}
        if action == "set_counter_sub_account":
            name = r.get("counter_sub_account", "").strip()
            if name not in sub_ids:
                raise ValueError(f"overrides: 補助科目を解決できない {name!r}")
            o["sub_account_id"] = sub_ids[name]
        if action == "set_account":
            name = r.get("account", "").strip()
            if name not in (account_ids or {}):
                raise ValueError(f"overrides: 科目を解決できない {name!r}")
            o["account_id"] = account_ids[name]
        if action == "merged_into":
            target = r.get("target_transaction_id", "").strip()
            if target not in known_tx or target == tid:
                raise ValueError(f"overrides: merged_into の統合先が不正 {target!r}")
            o["target"] = target
        out[tid] = o
    for tid, o in out.items():
        if o["action"] == "merged_into" and (out.get(o["target"]) or {}).get("action") == "exclude":
            raise ValueError(f"overrides: 統合先 {o['target']} が exclude")
    return out
