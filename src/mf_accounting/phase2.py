"""Phase 2 準備: 過去データだけ（AI/LLMなし）で相手勘定科目をどこまで推定できるかの分析。

- 口座側（bank side）の推定: fiscal_year × 連携口座ごとに、紐付いた過去仕訳から口座側の科目を推定
- 相手科目（分類ラベル）の抽出: 口座側以外。複合仕訳は complex_journal=True で識別
- exact content 分析と時系列バックテスト（未来データを使わない）

分類機能そのものは含まない。元データは変更せず、派生データとして出力する。
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .transform import term_lookup

Key = tuple[str | None, str | None]  # (account_id, sub_account_id)


def _gross(side: dict) -> int:
    return (side.get("value") or 0) + (side.get("tax_value") or 0)


def _debit_nets(journal: dict) -> dict[Key, int]:
    """(科目, 補助科目) ごとの 借方 − 貸方（税込）。"""
    nets: dict[Key, int] = defaultdict(int)
    for b in journal.get("branches") or []:
        if d := b.get("debitor"):
            nets[(d.get("account_id"), d.get("sub_account_id"))] += _gross(d)
        if c := b.get("creditor"):
            nets[(c.get("account_id"), c.get("sub_account_id"))] -= _gross(c)
    return nets


def _bank_oriented(nets: dict[Key, int], tx_side: str | None) -> dict[Key, int]:
    """口座側から見た純額（支出: 貸方−借方、入金: 借方−貸方）。"""
    sign = -1 if tx_side == "EXPENSE" else 1
    return {k: sign * v for k, v in nets.items()}


def bank_candidates(journal: dict, tx: dict) -> list[Key]:
    """口座側の純額が明細金額と一致する (科目, 補助科目)。"""
    return [k for k, v in _bank_oriented(_debit_nets(journal), tx.get("side")).items() if v == tx.get("value")]


# ---- 1. 口座側の推定 ------------------------------------------------------------


@dataclass
class BankMap:
    by_sub: dict[tuple, dict] = field(default_factory=dict)  # (fy, connected_sub_account_id) -> info
    by_service: dict[tuple, dict] = field(default_factory=dict)  # (fy, connected_account_id) -> info


def infer_bank_map(pairs: list[tuple[dict, dict, int]]) -> BankMap:
    """pairs: (明細, 仕訳, fiscal_year)。期×口座ごとに最頻の口座側科目を推定する。"""
    votes_sub: dict[tuple, Counter] = defaultdict(Counter)
    votes_svc: dict[tuple, Counter] = defaultdict(Counter)
    n_sub: Counter = Counter()
    n_svc: Counter = Counter()
    for tx, j, fy in pairs:
        ks, kv = (fy, tx.get("connected_sub_account_id")), (fy, tx.get("connected_account_id"))
        n_sub[ks] += 1
        n_svc[kv] += 1
        for k in set(bank_candidates(j, tx)):
            votes_sub[ks][k] += 1
            votes_svc[kv][k] += 1

    def summarize(votes, totals):
        out = {}
        for key, n in totals.items():
            if votes[key]:
                (best, cnt), *_ = votes[key].most_common(1)
                out[key] = {"key": best, "support": cnt, "journals": n, "ratio": cnt / n}
            else:
                out[key] = {"key": None, "support": 0, "journals": n, "ratio": 0.0}
        return out

    return BankMap(summarize(votes_sub, n_sub), summarize(votes_svc, n_svc))


MIN_MAP_SUPPORT = 3  # 口座×期の推定に使う最小の裏付け件数（未満はサービス単位にフォールバック）


def resolve_bank_key(tx: dict, j: dict, fy: int, bank_map: BankMap, master_key: Key | None) -> tuple[Key | None, str]:
    nets = _debit_nets(j)
    present = set(nets)
    for level, table, key in (
        ("fy_sub_account_map", bank_map.by_sub, (fy, tx.get("connected_sub_account_id"))),
        ("fy_service_map", bank_map.by_service, (fy, tx.get("connected_account_id"))),
    ):
        info = table.get(key)
        if info and info["key"] and info["support"] >= MIN_MAP_SUPPORT and info["key"] in present:
            return info["key"], level
    cands = bank_candidates(j, tx)
    if len(cands) == 1:
        return cands[0], "journal_amount"
    if master_key in present:
        return master_key, "current_master"
    return None, "unresolved"


# ---- 2. 相手科目の抽出 -----------------------------------------------------------


def counter_label(tx: dict, j: dict, bank_key: Key) -> dict:
    """口座側以外（相手側）の科目・税区分。純額0の中間科目（未払金経由など）は除外する。"""
    counter_net = {k: -v for k, v in _bank_oriented(_debit_nets(j), tx.get("side")).items() if k != bank_key}
    positive = {k: v for k, v in counter_net.items() if v > 0}
    negative = {k: v for k, v in counter_net.items() if v < 0}
    names: dict[Key, tuple] = {}
    taxes: dict[Key, set] = defaultdict(set)
    for b in j.get("branches") or []:
        for s in (b.get("debitor"), b.get("creditor")):
            if not s:
                continue
            k = (s.get("account_id"), s.get("sub_account_id"))
            names[k] = (s.get("account_name"), s.get("sub_account_name"))
            if k in positive or k in negative:
                taxes[k].add((s.get("tax_id"), s.get("tax_name"), s.get("invoice_kind")))

    keys = sorted(positive, key=lambda k: -positive[k]) + sorted(negative, key=lambda k: negative[k])
    primary = keys[0] if keys else None
    all_taxes = sorted({t for k in keys for t in taxes[k]}, key=str)
    return {
        "complex_journal": len(keys) > 1,
        "counter_count": len(keys),
        "has_negative_counter": bool(negative),
        # 分類ラベル: 相手科目の集合（複合仕訳も1つのラベルとして比較できる）
        # 相手側でマイナス（手数料差引など）の科目は "-" を付けて区別する
        "label_account": "|".join(sorted({("-" if k in negative else "") + str(k[0]) for k in keys})) or None,
        "label_account_sub": "|".join(sorted(("-" if k in negative else "") + f"{k[0]}/{k[1]}" for k in keys)) or None,
        "primary_account_id": primary[0] if primary else None,
        "primary_account_name": names.get(primary, (None, None))[0] if primary else None,
        "primary_sub_account_id": primary[1] if primary else None,
        "primary_sub_account_name": names.get(primary, (None, None))[1] if primary else None,
        "primary_tax_id": "|".join(sorted({str(t[0]) for t in taxes[primary]})) if primary else None,
        "primary_tax_name": "|".join(sorted({str(t[1]) for t in taxes[primary]})) if primary else None,
        "primary_invoice_kind": "|".join(sorted({str(t[2]) for t in taxes[primary]})) if primary else None,
        "counter_accounts": "|".join(f"{names.get(k, (None,))[0]}" for k in keys) or None,
        "label_tax": "|".join(f"{t[0]}:{t[2]}" for t in all_taxes) or None,
    }


def build_labels(journals: list[dict], transactions: list[dict], terms: list[dict], connected_accounts: list[dict]) -> tuple[list[dict], BankMap]:
    """紐付いた明細ごとに 相手科目ラベル を作る（1明細に複数仕訳がある場合は最初の仕訳を使い件数を記録）。"""
    term_of = term_lookup(terms)
    master = {c.get("id"): (c.get("account_id"), c.get("sub_account_id")) for a in connected_accounts or [] for c in a.get("connected_sub_accounts") or []}
    by_tx: dict[str, list[dict]] = defaultdict(list)
    for j in journals:
        if j.get("transaction_id"):
            by_tx[j["transaction_id"]].append(j)
    pairs = []
    for t in transactions:
        js = sorted(by_tx.get(t["id"], []), key=lambda j: j.get("number") or 0)
        if js:
            pairs.append((t, js[0], term_of(t.get("date")).get("fiscal_year"), len(js)))
    bank_map = infer_bank_map([(t, j, fy) for t, j, fy, _ in pairs])

    rows = []
    for t, j, fy, n_j in pairs:
        mk = master.get(t.get("connected_sub_account_id"))
        bank_key, method = resolve_bank_key(t, j, fy, bank_map, mk)
        row = {
            "transaction_id": t["id"],
            "fiscal_year": fy,
            "tx_date": t.get("date"),
            "tx_side": t.get("side"),
            "tx_value": t.get("value"),
            "tx_content": (t.get("content") or "").strip(),
            "connected_account_id": t.get("connected_account_id"),
            "connected_sub_account_id": t.get("connected_sub_account_id"),
            "journal_id": j.get("id"),
            "journals_per_transaction": n_j,
            "bank_method": method,
            "bank_account_id": bank_key[0] if bank_key else None,
            "bank_sub_account_id": bank_key[1] if bank_key else None,
            "bank_matches_current_master": bank_key == mk if bank_key else None,
            "bank_amount_matches_tx": (bank_key in bank_candidates(j, t)) if bank_key else None,
        }
        row.update(counter_label(t, j, bank_key) if bank_key else {"complex_journal": None, "label_account": None, "label_account_sub": None})
        rows.append(row)
    return rows, bank_map


# ---- 3. exact content 分析 ------------------------------------------------------


def content_stats(rows: list[dict], label: str = "label_account") -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r.get("tx_content") and r.get(label):
            groups[r["tx_content"]].append(r)
    out = []
    for content, rs in groups.items():
        c = Counter(r[label] for r in rs)
        (top, top_n), *_ = c.most_common(1)
        latest = max(rs, key=lambda r: (r["tx_date"], r["transaction_id"]))
        by_fy: dict = defaultdict(Counter)
        for r in rs:
            by_fy[r["fiscal_year"]][r[label]] += 1
        out.append(
            {
                "content": content,
                "count": len(rs),
                "distinct_labels": len(c),
                "top_label": top,
                "top_ratio": top_n / len(rs),
                "latest_label": latest[label],
                "latest_date": latest["tx_date"],
                "labels": dict(c),
                "labels_by_fy": {fy: dict(v) for fy, v in sorted(by_fy.items())},
            }
        )
    return out


# ---- 4/5. 時系列バックテスト ------------------------------------------------------


def _history(train: list[dict], label: str) -> dict[str, dict]:
    h: dict[str, dict] = {}
    for r in sorted(train, key=lambda r: (r["tx_date"], r["transaction_id"])):
        if not (r.get("tx_content") and r.get(label)):
            continue
        e = h.setdefault(r["tx_content"], {"labels": Counter(), "latest": None, "n": 0})
        e["labels"][r[label]] += 1
        e["latest"] = r[label]
        e["n"] += 1
    return h


def backtest(rows: list[dict], target_fy: int, label: str = "label_account", thresholds=(10, 5, 3, 2, 1)) -> dict:
    train = [r for r in rows if r["fiscal_year"] is not None and r["fiscal_year"] < target_fy]
    test = [r for r in rows if r["fiscal_year"] == target_fy and r.get(label) and r.get("tx_content")]
    hist = _history(train, label)

    covered = mode_ok = latest_ok = 0
    conf = {n: {"n": 0, "ok": 0} for n in thresholds}
    conf_complex = {"n": 0, "ok": 0}
    for r in test:
        h = hist.get(r["tx_content"])
        if not h:
            continue
        covered += 1
        (top, top_n), *_ = h["labels"].most_common(1)
        mode_ok += top == r[label]
        latest_ok += h["latest"] == r[label]
        if top_n == h["n"]:  # 過去すべて同一科目
            for n in thresholds:
                if h["n"] >= n:
                    conf[n]["n"] += 1
                    conf[n]["ok"] += top == r[label]
            if "|" in top:
                conf_complex["n"] += 1
                conf_complex["ok"] += top == r[label]

    rate = lambda a, b: round(a / b, 4) if b else None
    return {
        "target_fy": target_fy,
        "train_fys": sorted({r["fiscal_year"] for r in train}),
        "label": label,
        "test": len(test),
        "test_complex": sum(1 for r in test if r.get("complex_journal")),
        "covered": covered,
        "coverage": rate(covered, len(test)),
        "mode_correct": mode_ok,
        "mode_accuracy_on_covered": rate(mode_ok, covered),
        "latest_correct": latest_ok,
        "latest_accuracy_on_covered": rate(latest_ok, covered),
        "mode_correct_of_all_test": rate(mode_ok, len(test)),
        "confidence_unanimous": {
            f">={n}": {"n": v["n"], "share_of_test": rate(v["n"], len(test)), "accuracy": rate(v["ok"], v["n"])} for n, v in conf.items()
        },
        "confidence_unanimous_complex_label": {**conf_complex, "accuracy": rate(conf_complex["ok"], conf_complex["n"])},
    }
