"""Phase 4: LLM による主科目（primary account）推定の評価基盤。

- 対象選定: 過去履歴だけでは判断しにくい明細（A: exactなし / B: fuzzy候補のみ / C: 科目が割れている / D: 365日以上未使用）
- payload: 分類に必要な最小限の情報のみ。口座番号・カード番号・メール・電話番号らしき文字列はマスク
- 出力: JSON Schema（主科目は取得済み勘定科目の短縮コードの enum のみ → 存在しない科目は出力不可）
- 正解（評価年度の仕訳）は payload に含めず、予測後の評価でのみ参照する

MF への書き込みは一切行わない。LLM API の呼び出しは run_llm() のみで、明示的な承認フラグが必要。
"""

import json
import random
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date

from rapidfuzz import fuzz, process

from .phase3 import NORMALIZERS

NORMALIZER = "n4 +6桁以上の数字をマスク"
MAX_CANDIDATES = 8
FUZZY_CANDIDATE_MIN = 70  # これ未満の fuzzy 最上位は「候補なし」扱い（Phase 3 で accuracy 40%前後）

# ---- 送信前マスキング ----------------------------------------------------------------

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"0\d{1,4}-\d{1,4}-\d{3,4}|\b0[5789]0\d{8}\b")
_CARD = re.compile(r"(?:\d[ -]?){12,19}|\*{2,}\d{2,4}|x{2,}\d{2,4}", re.IGNORECASE)
_LONG_DIGITS = re.compile(r"\d{7,}")


def sanitize(text: str | None) -> str:
    """分類に不要な識別子をマスクする。加盟店名・サービス名や短い数字は残す。"""
    if not text:
        return ""
    t = _EMAIL.sub("<EMAIL>", text)
    t = _PHONE.sub("<PHONE>", t)
    t = _CARD.sub("<NUM>", t)
    t = _LONG_DIGITS.sub("<NUM>", t)
    return t.strip()


AMOUNT_BANDS = [(1_000, "〜1千円"), (5_000, "1千〜5千円"), (10_000, "5千〜1万円"), (50_000, "1万〜5万円"),
                (100_000, "5万〜10万円"), (500_000, "10万〜50万円"), (1_000_000, "50万〜100万円")]


def amount_band(v: int | None) -> str:
    if v is None:
        return "不明"
    for upper, label in AMOUNT_BANDS:
        if abs(v) < upper:
            return label
    return "100万円以上"


# ---- 勘定科目カタログ（出力可能な科目） ------------------------------------------------


@dataclass
class Catalog:
    code_to_id: dict[str, str]
    id_to_code: dict[str, str]
    rows: list[dict]  # {code, name, group, category}

    @classmethod
    def from_accounts(cls, accounts: list[dict], available_only: bool = True) -> "Catalog":
        acc = [a for a in accounts if a.get("available") or not available_only]
        acc.sort(key=lambda a: (a.get("account_group") or "", a.get("category") or "", a.get("name") or ""))
        rows, c2i, i2c = [], {}, {}
        for n, a in enumerate(acc, start=1):
            code = f"A{n:03d}"
            c2i[code], i2c[a["id"]] = a["id"], code
            rows.append({"code": code, "name": a.get("name"), "group": a.get("account_group"), "category": a.get("category")})
        return cls(c2i, i2c, rows)

    def text(self) -> str:
        return "\n".join(f"{r['code']}\t{r['name']}\t{r['group']}\t{r['category']}" for r in self.rows)


def output_schema(catalog: Catalog) -> dict:
    return {
        "type": "object",
        "properties": {
            "primary_account_code": {"anyOf": [{"type": "string", "enum": sorted(catalog.code_to_id)}, {"type": "null"}]},
            "confidence": {"type": "number"},
            "reason": {"type": "string"},
            "needs_review": {"type": "boolean"},
            "insufficient_information": {"type": "boolean"},
        },
        "required": ["primary_account_code", "confidence", "reason", "needs_review", "insufficient_information"],
        "additionalProperties": False,
    }


SYSTEM_PROMPT = """あなたは日本の中小企業の経理担当者を補助するアシスタントです。
銀行口座・クレジットカード等の連携明細1件について、仕訳の「主科目」（口座の相手側の勘定科目のうち、金額が最も大きいもの）を推定します。

判断材料:
- 明細の摘要（content）、入出金の別、金額帯、取引日、口座の種類
- 同じ会社の過去の類似明細と、そのとき実際に使われた主科目（件数・最終利用日つき）
- 選択可能な勘定科目の一覧（コードで回答）

方針:
- 過去の類似明細で一貫して使われている科目があり、今回の明細と同じ取引先・同じ性質と判断できる場合は、その科目を優先してください。
- 過去の候補が別の取引先に見える場合や、科目が割れている場合は、摘要から取引の性質を判断してください。
- 判断材料が不足している場合は insufficient_information を true にし、推定できる科目があれば回答、なければ primary_account_code を null にしてください。
- confidence は 0.0〜1.0 の数値で、この推定が正しい確率の見積もりです。
- 人の確認が望ましい場合は needs_review を true にしてください。
- reason は日本語で1〜2文、判断根拠を簡潔に書いてください。明細に含まれる個人名や番号は reason に書き写さないでください。
- 消費税区分・仕訳の分割（複合仕訳）は今回は判断しません。主科目のみを回答してください。

選択可能な勘定科目（コード\t科目名\tグループ\t区分）:
"""


def system_text(catalog: Catalog) -> str:
    return SYSTEM_PROMPT + catalog.text()


# ---- 対象選定 -----------------------------------------------------------------------


def classify_target(p: dict) -> dict:
    """phase3.rolling_predictions の結果から、LLM対象かどうかとカテゴリを判定する。"""
    exact = p["stage"] in ("raw_exact", "norm_exact")
    high_conf = exact and p["hist_n"] >= 3 and p["hist_agreement"] == 1 and p["days_since_last"] <= 365
    cats = []
    if not exact:
        if p["stage"] == "fuzzy" and p["score"] >= FUZZY_CANDIDATE_MIN:
            cats.append("B_fuzzy候補のみ")
        else:
            cats.append("A_候補なし")
    if exact and p["hist_agreement"] < 1:
        cats.append("C_科目が割れている")
    if exact and p.get("days_since_last", 0) > 365:
        cats.append("D_365日以上未使用")
    return {"high_confidence": high_conf, "llm_target": bool(cats) and not high_conf, "categories": cats}


def stratified_sample(targets: list[dict], n: int = 40, seed: int = 20260927) -> list[dict]:
    """カテゴリ（候補なし / fuzzyのみ / 割れ / 長期未使用）× simple/complex が混ざるように抽出。

    simple/complex は抽出の偏りを避けるためにのみ使い、LLM には渡さない。
    """
    rng = random.Random(seed)
    def stratum(t):
        c = t["categories"]
        kind = "B" if "B_fuzzy候補のみ" in c else "A" if "A_候補なし" in c else "C" if "C_科目が割れている" in c else "D"
        return kind, t["actual_complex"]
    groups: dict = {}
    for t in targets:
        groups.setdefault(stratum(t), []).append(t)
    for g in groups.values():
        rng.shuffle(g)
    picked: list[dict] = []
    keys = sorted(groups, key=str)
    while len(picked) < n and any(groups[k] for k in keys):
        for k in keys:
            if groups[k] and len(picked) < n:
                picked.append(groups[k].pop())
    return sorted(picked, key=lambda t: (t["tx_date"], t["transaction_id"]))


# ---- 過去候補（ローリング: 取引日より前のみ） -------------------------------------------


def build_candidate_index(rows: list[dict]):
    """取引日順に並べた過去データから、任意の日付時点の候補を取り出せるようにする。"""
    f = NORMALIZERS[NORMALIZER]
    ordered = sorted((r for r in rows if r.get("tx_content") and r.get("primary_account_id")), key=lambda r: (r["tx_date"], r["transaction_id"]))
    for r in ordered:
        r["_nk"] = f(r["tx_content"])
    return ordered


def candidates_before(ordered: list[dict], tx_date: str, query_content: str, id_to_code: dict, names: dict, limit: int = MAX_CANDIDATES) -> list[dict]:
    f = NORMALIZERS[NORMALIZER]
    past = [r for r in ordered if r["tx_date"] < tx_date]  # 同日・未来は使わない
    by_key: dict[str, list[dict]] = {}
    for r in past:
        by_key.setdefault(r["_nk"], []).append(r)
    q = f(query_content)
    keys = [k for k in by_key if k]
    scored = []
    if q in by_key:
        scored.append((q, 100.0))
    if keys:
        for k, s, _ in process.extract(q, [k for k in keys if k != q], scorer=fuzz.ratio, limit=limit):
            scored.append((k, float(s)))
    out = []
    for k, s in scored[:limit]:
        rs = by_key[k]
        c = Counter(r["primary_account_id"] for r in rs)
        out.append({
            "content": sanitize(rs[-1]["tx_content"]),
            "similarity": round(s),
            "occurrences": len(rs),
            "last_used": rs[-1]["tx_date"],
            "accounts": [
                {"code": id_to_code.get(a, "対象外の科目"), "name": names.get(a), "count": n}
                for a, n in c.most_common(3)
            ],
        })
    return out


def build_payload(target: dict, candidates: list[dict], account_kind: str) -> dict:
    """LLM へ送る user メッセージ本体。正解や仕訳の中身は含めない。"""
    return {
        "transaction": {
            "content": sanitize(target["tx_content"]),
            "side": {"EXPENSE": "出金", "INCOME": "入金"}.get(target.get("tx_side"), target.get("tx_side")),
            "amount_band": amount_band(target.get("tx_value")),
            "date": target["tx_date"],
            "account_kind": account_kind,
        },
        "past_similar_transactions": candidates,
    }


def user_text(payload: dict) -> str:
    return "次の明細の主科目を推定してください。\n" + json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=1)


# ---- コスト見積もり（ローカル概算。API を呼ばない） ------------------------------------

PRICES = {  # USD / 1M tokens（input, output, cache read）
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-sonnet-5": (2.00, 10.00, 0.20),
    "claude-haiku-4-5": (1.00, 5.00, 0.10),
}


def estimate_tokens(text: str) -> int:
    """概算: ASCII は約3.5文字/トークン、日本語など非ASCIIは約1文字/トークン（多めに見積もる）。"""
    ascii_n = sum(1 for ch in text if ord(ch) < 128)
    return int(ascii_n / 3.5 + (len(text) - ascii_n) * 1.0) + 1


def estimate_cost(model: str, system_tokens: int, user_tokens: list[int], out_tokens_per_item: tuple[int, int]) -> dict:
    pin, pout, pcache = PRICES[model]
    n = len(user_tokens)
    in_nocache = sum(user_tokens) + system_tokens * n
    in_cached = sum(user_tokens) + system_tokens * 1.25 + system_tokens * max(n - 1, 0) * (pcache / pin)  # 初回書き込み1.25倍 + 以降キャッシュ読み
    lo, hi = out_tokens_per_item
    return {
        "model": model,
        "items": n,
        "input_tokens": in_nocache,
        "output_tokens_range": (lo * n, hi * n),
        "cost_usd_no_cache": (in_nocache * pin + lo * n * pout) / 1e6,
        "cost_usd_range_with_cache": ((in_cached * pin + lo * n * pout) / 1e6, (in_cached * pin + hi * n * pout) / 1e6),
    }


# ---- LLM 呼び出し（承認後のみ） ----------------------------------------------------------


class LLMRunError(RuntimeError):
    """API エラー・拒否・想定外のレスポンス。fallback せずに停止する。"""


def _request(system: str, schema: dict, user_text: str, model: str, effort: str) -> dict:
    return {
        "model": model,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user_text}],
    }


def make_client(max_retries: int = 0):
    """Anthropic クライアント。ワークスペースに紐付かないキーの場合は ANTHROPIC_WORKSPACE_ID をヘッダーで指定する。"""
    import os

    import anthropic

    ws = os.getenv("ANTHROPIC_WORKSPACE_ID", "").strip()
    headers = {"anthropic-workspace-id": ws} if ws else None
    return anthropic.Anthropic(max_retries=max_retries, default_headers=headers)


def count_tokens(items: list[dict], system: str, schema: dict, model: str, effort: str, approved: bool) -> list[int]:
    """count_tokens API で実際の入力トークン数を数える（payload は Anthropic に送信される）。"""
    if not approved:
        raise PermissionError("count_tokens も payload を送信するため、承認フラグが必要です（--approve）。")
    import anthropic

    client = make_client()
    counts = []
    for it in items:
        try:
            counts.append(client.messages.count_tokens(**_request(system, schema, it["user_text"], model, effort)).input_tokens)
        except anthropic.APIError as e:
            raise LLMRunError(f"count_tokens で API エラー: {type(e).__name__} (status={getattr(e, 'status_code', None)}): {_error_message(e)}") from None
    return counts


def _error_message(e) -> str:
    """API エラーの説明文のみ（リクエスト内容や認証情報は含めない）。"""
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        return str((body.get("error") or {}).get("message", ""))[:300]
    return ""


def parse_output(resp, catalog: Catalog) -> dict:
    if resp.stop_reason != "end_turn":
        raise LLMRunError(f"想定外の stop_reason: {resp.stop_reason}")
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if text is None:
        raise LLMRunError("テキストブロックがありません")
    try:
        out = json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMRunError("JSON として解釈できない出力") from e
    code = out.get("primary_account_code")
    if code is not None and code not in catalog.code_to_id:
        raise LLMRunError("選択肢にない勘定科目コード")
    conf = out.get("confidence")
    if not isinstance(conf, (int, float)) or not 0.0 <= conf <= 1.0:
        raise LLMRunError("confidence が 0〜1 の数値ではありません")
    out["primary_account_id"] = catalog.code_to_id.get(code) if code else None
    return out


def run_llm(items: list[dict], system: str, schema: dict, catalog: Catalog, model: str, effort: str, approved: bool,
            on_result=None) -> list[dict]:
    """items: [{"transaction_id", "user_text"}]。fallback なし。異常時は LLMRunError で停止する。"""
    if not approved:
        raise PermissionError("LLM API の呼び出しにはユーザーの承認フラグが必要です（--approve）。")
    import anthropic

    client = make_client(max_retries=0)  # 自動リトライ・fallback なし。認証情報はログに出さない
    results = []
    for it in items:
        try:
            resp = client.messages.create(max_tokens=4096, **_request(system, schema, it["user_text"], model, effort))
        except anthropic.APIError as e:
            raise LLMRunError(f"API エラー: {type(e).__name__} (status={getattr(e, 'status_code', None)}): {_error_message(e)}") from None
        rec = {"transaction_id": it["transaction_id"], "model": resp.model, "stop_reason": resp.stop_reason,
               "usage": {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens,
                         "cache_read": resp.usage.cache_read_input_tokens or 0, "cache_write": resp.usage.cache_creation_input_tokens or 0}}
        rec["output"] = parse_output(resp, catalog)  # 異常なら LLMRunError
        results.append(rec)
        if on_result:
            on_result(rec)
    return results


REASON_CATEGORIES = [
    ("情報不足", ("不足", "不明", "判断できない", "特定できない", "判別できない", "情報が少な")),
    ("過去履歴を根拠", ("過去", "履歴", "これまで", "一貫", "従来", "前回")),
    ("類似取引を根拠", ("類似", "似た", "同様", "近い", "同種")),
    ("金額/入出金方向を根拠", ("金額", "入金", "出金", "少額", "高額")),
    ("contentの意味から判断", ("摘要", "内容", "名称", "サービス", "店", "利用", "手数料", "から判断", "と考え", "と推定")),
]


def reason_categories(reason: str | None) -> list[str]:
    r = reason or ""
    cats = [name for name, kws in REASON_CATEGORIES if any(k in r for k in kws)]
    return cats or ["その他"]


def days_between(a: str, b: str) -> int:
    return (date.fromisoformat(b[:10]) - date.fromisoformat(a[:10])).days
