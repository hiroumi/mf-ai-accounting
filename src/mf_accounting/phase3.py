"""Phase 3: content 正規化 + fuzzy matching による主科目（primary account）推定の評価。

- 元の tx_content は変更せず、正規化は派生値として計算する
- ローリング評価: 各明細は「その取引日より前」のデータのみを検索対象にする（同日も含めない）
- 仕訳構造（simple/complex）は予測しない。主科目の当否と分けて集計する
"""

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from rapidfuzz import fuzz, process

LABEL = "primary_account_id"

_WS = re.compile(r"\s+")
_SYMBOLS = re.compile(r"[^\w\sー]")  # 長音記号「ー」は語の一部として残す
_SYMBOLS_KEEP_MASK = re.compile(r"[^\w\sー#]")  # マスク記号 # は残す
_DATE = re.compile(r"\d{1,2}/\d{1,2}|\d{1,2}月\d{1,2}日|20\d{6}|\d{2}\.\d{1,2}\.\d{1,2}")


def _n1(c: str) -> str:
    return _WS.sub(" ", unicodedata.normalize("NFKC", c).casefold()).strip()


def _n2(c: str) -> str:
    return _WS.sub(" ", _SYMBOLS.sub(" ", _n1(c))).strip()


def _n3(c: str) -> str:
    return _WS.sub("", _n2(c))


NORMALIZERS = {
    "raw": lambda c: c.strip(),
    "n1 NFKC+casefold+空白整理": _n1,
    "n2 +記号を空白に": _n2,
    "n3 +空白除去": _n3,
    "n4 +6桁以上の数字をマスク": lambda c: re.sub(r"\d{6,}", "#", _n3(c)),
    # 日付は記号（/ 等）を除去する前にマスクする
    "n5 +4桁以上の数字と日付をマスク": lambda c: _WS.sub("", _SYMBOLS_KEEP_MASK.sub(" ", re.sub(r"\d{4,}", "#", _DATE.sub("#", _n1(c))))),
}

SCORERS = {"ratio": fuzz.ratio, "WRatio": fuzz.WRatio}


@dataclass
class Hist:
    labels: list[str] = field(default_factory=list)
    dates: list[str] = field(default_factory=list)
    complex: list[bool] = field(default_factory=list)

    def add(self, r: dict) -> None:
        self.labels.append(r[LABEL])
        self.dates.append(r["tx_date"])
        self.complex.append(bool(r.get("complex_journal")))

    def mode(self) -> str:
        c = Counter(self.labels)
        last = {l: i for i, l in enumerate(self.labels)}
        return max(c, key=lambda l: (c[l], last[l]))

    def agreement(self) -> float:
        return Counter(self.labels)[self.mode()] / len(self.labels)

    def complex_history(self) -> str:
        return "simple" if not any(self.complex) else "complex" if all(self.complex) else "mixed"


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(b[:10]) - date.fromisoformat(a[:10])).days


def rolling_predictions(rows: list[dict], target_fy: int, norm: str, scorer: str = "ratio", fuzzy: bool = True) -> list[dict]:
    """評価年度の各明細について、raw exact → normalized exact → fuzzy(top3) の候補情報を返す。

    fuzzy は閾値を適用せず top3 のスコアを保持する（閾値は集計時に比較する）。
    """
    f_norm = NORMALIZERS[norm]
    f_score = SCORERS[scorer]
    ordered = sorted(
        (r for r in rows if r.get("tx_content") and r.get(LABEL) and r["fiscal_year"] is not None and r["fiscal_year"] <= target_fy),
        key=lambda r: (r["tx_date"], r["transaction_id"]),
    )
    raw_hist: dict[str, Hist] = {}
    norm_hist: dict[str, Hist] = {}
    keys: list[str] = []  # fuzzy の検索対象（過去の正規化済み content、重複なし）
    out = []
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
            rec = {"transaction_id": r["transaction_id"], "actual": r[LABEL], "actual_complex": bool(r.get("complex_journal")),
                   "tx_content": r["tx_content"], "normalized_content": nk, "tx_date": r["tx_date"]}
            h, stage, score, top3 = None, "none", None, []
            if raw in raw_hist:
                h, stage, score = raw_hist[raw], "raw_exact", 100.0
            elif nk in norm_hist:
                h, stage, score = norm_hist[nk], "norm_exact", 100.0
            elif fuzzy and keys and nk:
                top3 = process.extract(nk, keys, scorer=f_score, limit=3)
                if top3:
                    h, stage, score = norm_hist[top3[0][0]], "fuzzy", float(top3[0][1])
            if h is not None:
                preds3 = [norm_hist[k].mode() for k, _, _ in top3] if top3 else [h.mode()]
                rec.update({
                    "stage": stage,
                    "pred": h.mode(),
                    "score": score,
                    "top3_keys": [k for k, _, _ in top3],
                    "top3_scores": [round(float(s), 1) for _, s, _ in top3],
                    "top3_preds": preds3,
                    "top3_agree": len(set(preds3)) == 1 and len(preds3) > 1,
                    "hist_n": len(h.labels),
                    "hist_agreement": round(h.agreement(), 4),
                    "days_since_last": _days(h.dates[-1], r["tx_date"]),
                    "hist_complex": h.complex_history(),
                })
            else:
                rec.update({"stage": "none", "pred": None, "score": None})
            rec["correct"] = rec["pred"] == rec["actual"] if rec["pred"] else None
            out.append(rec)
        for r in batch:
            raw, nk = r["tx_content"].strip(), f_norm(r["tx_content"])
            raw_hist.setdefault(raw, Hist()).add(r)
            if nk not in norm_hist:
                norm_hist[nk] = Hist()
                if nk:
                    keys.append(nk)
            norm_hist[nk].add(r)
    return out


def apply_threshold(preds: list[dict], threshold: float | None) -> list[dict]:
    """fuzzy の候補を閾値で採否する（threshold=None は fuzzy を使わない）。"""
    out = []
    for p in preds:
        if p["stage"] == "fuzzy" and (threshold is None or p["score"] < threshold):
            p = {**p, "stage": "none", "pred": None, "correct": None}
        out.append(p)
    return out


def _rate(a: int, b: int) -> float | None:
    return round(a / b, 4) if b else None


def stage_summary(preds: list[dict]) -> dict:
    n = len(preds)
    out = {"test": n}
    for st in ("raw_exact", "norm_exact", "fuzzy", "none"):
        ps = [p for p in preds if p["stage"] == st]
        ok = sum(1 for p in ps if p["correct"])
        out[st] = {"n": len(ps), "share": _rate(len(ps), n), "accuracy": _rate(ok, len(ps)) if st != "none" else None,
                   "simple_acc": _rate(sum(1 for p in ps if p["correct"] and not p["actual_complex"]), sum(1 for p in ps if not p["actual_complex"])) if st != "none" else None,
                   "complex_acc": _rate(sum(1 for p in ps if p["correct"] and p["actual_complex"]), sum(1 for p in ps if p["actual_complex"])) if st != "none" else None,
                   "simple_n": sum(1 for p in ps if not p["actual_complex"]), "complex_n": sum(1 for p in ps if p["actual_complex"])}
    covered = [p for p in preds if p["stage"] != "none"]
    ok = sum(1 for p in covered if p["correct"])
    out["total"] = {"covered": len(covered), "coverage": _rate(len(covered), n), "accuracy": _rate(ok, len(covered)), "correct": ok}
    return out


def bucket_accuracy(preds: list[dict], key, order: list | None = None) -> list[tuple]:
    groups: dict = {}
    for p in preds:
        if p["stage"] == "none":
            continue
        groups.setdefault(key(p), []).append(p)
    keys = order or sorted(groups, key=str)
    return [(k, len(groups[k]), _rate(sum(1 for p in groups[k] if p["correct"]), len(groups[k]))) for k in keys if k in groups]
