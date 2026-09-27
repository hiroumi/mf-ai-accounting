"""Phase 2.5: exact content 推定の改善検証（直近重視・年度重み・記帳方針変更の検知）。

分類機能ではなく、バックテストで比較するための候補ルール群。重みや閾値は固定の正解ではない。
"""

from collections import Counter
from dataclasses import dataclass
from itertools import product

LABEL = "label_account"

FY_WEIGHTS = {
    "fy_w(1/.7/.5/.3)": {1: 1.0, 2: 0.7, 3: 0.5, "older": 0.3},
    "fy_w(1/.5/.25/.1)": {1: 1.0, 2: 0.5, 3: 0.25, "older": 0.1},
}


@dataclass
class Obs:
    date: str
    fy: int
    label: str
    complex: bool


def histories(rows: list[dict], before_fy: int, label: str = LABEL) -> dict[str, list[Obs]]:
    """before_fy より前の年度のみから content ごとの時系列を作る（未来データを使わない）。"""
    h: dict[str, list[Obs]] = {}
    for r in sorted(rows, key=lambda r: (r["tx_date"], r["transaction_id"])):
        if r["fiscal_year"] is None or r["fiscal_year"] >= before_fy or not r.get("tx_content") or not r.get(label):
            continue
        h.setdefault(r["tx_content"], []).append(Obs(r["tx_date"], r["fiscal_year"], r[label], bool(r.get("complex_journal"))))
    return h


def mode(obs: list[Obs]) -> tuple[str, float]:
    """最頻ラベルと一致率。同数の場合はより最近使われたラベルを優先。"""
    c = Counter(o.label for o in obs)
    last_idx = {o.label: i for i, o in enumerate(obs)}
    best = max(c, key=lambda l: (c[l], last_idx[l]))
    return best, c[best] / len(obs)


def window_stats(obs: list[Obs], k: int | None) -> dict:
    w = obs if k is None else obs[-k:]
    m, ratio = mode(w)
    return {"mode": m, "agreement": ratio, "n": len(w), "last_date": w[-1].date, "changed_in_window": len({o.label for o in w}) > 1}


def detect_change(obs: list[Obs], min_run: int = 2) -> dict:
    """A A A A B B B → 最後の連続(B)の手前が別ラベルなら変更とみなす。"""
    current = obs[-1].label
    run = 0
    for o in reversed(obs):
        if o.label != current:
            break
        run += 1
    prefix = obs[: len(obs) - run]
    detected = bool(prefix) and run >= min_run
    return {
        "change_detected": detected,
        "previous_account": prefix[-1].label if prefix else None,
        "previous_mode": mode(prefix)[0] if prefix else None,
        "current_account": current,
        "change_date": obs[len(obs) - run].date if prefix else None,
        "observations_since_change": run if prefix else None,
        "observations_total": len(obs),
    }


def fy_weighted(obs: list[Obs], target_fy: int, weights: dict) -> str:
    score: Counter = Counter()
    for o in obs:
        d = max(target_fy - o.fy, 1)  # ローリング評価では同年度の観測も「直近」として扱う
        score[o.label] += weights.get(d, weights["older"])
    last_idx = {o.label: i for i, o in enumerate(obs)}
    return max(score, key=lambda l: (score[l], last_idx[l]))


def predictors(target_fy: int) -> dict:
    p = {
        "A 全期間の最頻": lambda o: mode(o)[0],
        "B 直近1件": lambda o: o[-1].label,
        "C 直近3件の最頻": lambda o: mode(o[-3:])[0],
        "D 直近5件の最頻": lambda o: mode(o[-5:])[0],
    }
    for name, w in FY_WEIGHTS.items():
        p[f"E {name}"] = lambda o, w=w: fy_weighted(o, target_fy, w)
    for m in (2, 3):
        p[f"F 変更検知(連続{m}件)→なければA"] = lambda o, m=m: (lambda c: c["current_account"] if c["change_detected"] else mode(o)[0])(detect_change(o, m))
    return p


def _rate(a: int, b: int) -> float | None:
    return round(a / b, 4) if b else None


def _is_test(r: dict, target_fy: int) -> bool:
    return r["fiscal_year"] == target_fy and bool(r.get(LABEL)) and bool(r.get("tx_content"))


def year_block_pairs(rows: list[dict], target_fy: int) -> list[tuple[dict, list[Obs] | None]]:
    """評価年度より前の年度だけを履歴とする（年度単位のバックテスト）。"""
    hist = histories(rows, target_fy)
    return [(r, hist.get(r["tx_content"])) for r in rows if _is_test(r, target_fy)]


def rolling_pairs(rows: list[dict], target_fy: int) -> list[tuple[dict, list[Obs] | None]]:
    """各明細を「その取引日より前」の全データで予測する（同日のデータは使わない）。"""
    ordered = sorted((r for r in rows if r.get("tx_content") and r.get(LABEL) and r["fiscal_year"] is not None and r["fiscal_year"] <= target_fy),
                     key=lambda r: (r["tx_date"], r["transaction_id"]))
    hist: dict[str, list[Obs]] = {}
    pairs = []
    i = 0
    while i < len(ordered):
        day = ordered[i]["tx_date"]
        same_day = []
        while i < len(ordered) and ordered[i]["tx_date"] == day:
            same_day.append(ordered[i])
            i += 1
        for r in same_day:
            if r["fiscal_year"] == target_fy:
                o = hist.get(r["tx_content"])
                pairs.append((r, list(o) if o else None))
        for r in same_day:
            hist.setdefault(r["tx_content"], []).append(Obs(r["tx_date"], r["fiscal_year"], r[LABEL], bool(r.get("complex_journal"))))
    return pairs


def compare_methods(pairs: list[tuple[dict, list[Obs] | None]], target_fy: int) -> dict:
    out = {}
    for name, f in predictors(target_fy).items():
        s = {"all": [0, 0], "simple": [0, 0], "complex": [0, 0]}
        for r, o in pairs:
            if not o:
                continue
            ok = f(o) == r[LABEL]
            for k in ("all", "complex" if r.get("complex_journal") else "simple"):
                s[k][0] += 1
                s[k][1] += ok
        out[name] = {
            "test": len(pairs),
            "covered": s["all"][0],
            "coverage": _rate(s["all"][0], len(pairs)),
            "accuracy": _rate(s["all"][1], s["all"][0]),
            "simple_n": s["simple"][0],
            "simple_accuracy": _rate(s["simple"][1], s["simple"][0]),
            "complex_n": s["complex"][0],
            "complex_accuracy": _rate(s["complex"][1], s["complex"][0]),
        }
    return out


def structure_shift(rows: list[dict], target_fy: int) -> dict:
    """過去は simple のみだった content が、評価年度に complex になった件数など。"""
    hist = histories(rows, target_fy)
    test = [r for r in rows if r["fiscal_year"] == target_fy and r.get(LABEL) and r.get("tx_content")]
    c = Counter()
    pairs = set()
    for r in test:
        o = hist.get(r["tx_content"])
        if not o:
            continue
        past = "simple" if not any(x.complex for x in o) else "complex" if all(x.complex for x in o) else "mixed"
        now = "complex" if r.get("complex_journal") else "simple"
        c[f"{past}→{now}"] += 1
        pairs.add((r["tx_content"], f"{past}→{now}"))
    return {"rows": dict(c), "contents": dict(Counter(k for _, k in pairs))}


# ---- 6. High-confidence 条件 -------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    last_k: int  # 直近 k 件がすべて同一ラベル
    min_total: int  # 過去の総観測数
    recency: str  # "any" / "prev_fy"（直近の観測が前年度） / "within2"
    simple_only: bool  # 過去（直近k件）がすべて simple
    all_unanimous: bool  # 過去全期間でも100%同一

    def name(self) -> str:
        parts = [f"直近{self.last_k}件同一"]
        if self.min_total > self.last_k:
            parts.append(f"総{self.min_total}件以上")
        if self.all_unanimous:
            parts.append("全期間100%")
        if self.recency != "any":
            parts.append({"prev_fy": "前年度以降に使用", "within2": "2年以内に使用"}[self.recency])
        if self.simple_only:
            parts.append("simpleのみ")
        return "・".join(parts)

    def applies(self, o: list[Obs], target_fy: int) -> str | None:
        if len(o) < max(self.last_k, self.min_total):
            return None
        tail = o[-self.last_k :]
        label = tail[-1].label
        if any(x.label != label for x in tail):
            return None
        if self.all_unanimous and any(x.label != label for x in o):
            return None
        if self.simple_only and any(x.complex for x in tail):
            return None
        gap = target_fy - o[-1].fy  # 年度単位の評価では >=1、ローリング評価では同年度(0)もありうる
        if self.recency == "prev_fy" and gap > 1 or self.recency == "within2" and gap > 2:
            return None
        return label


def rule_grid() -> list[Rule]:
    rules = []
    for k, extra, rec, simple, unan in product((2, 3, 4, 5, 7, 10), (0, 5, 10), ("any", "within2", "prev_fy"), (False, True), (False, True)):
        total = max(k, extra)
        rules.append(Rule(k, total, rec, simple, unan))
    return sorted(set(rules), key=lambda r: r.name())


def evaluate_rule(rule: Rule, pairs: list[tuple[dict, list[Obs] | None]], target_fy: int) -> dict:
    n = ok = 0
    for r, o in pairs:
        if not o:
            continue
        pred = rule.applies(o, target_fy)
        if pred is None:
            continue
        n += 1
        ok += pred == r[LABEL]
    return {"n": n, "coverage": _rate(n, len(pairs)), "accuracy": _rate(ok, n), "correct": ok, "test": len(pairs)}
