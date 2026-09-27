"""AI分類用データとしての品質集計（年度別 + 全体）。

出力は件数・率のみ。摘要・金額・取引先名などの値は含めない。
"""

from collections import Counter, defaultdict

from .transform import term_lookup

JOURNALIZED = {"registered", "modified", "new_voucher_attached"}
OPENING = "JOURNAL_TYPE_OPENING"


def _rate(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def _has_partner(j: dict) -> bool:
    return any(
        (side or {}).get("trade_partner_code") or (side or {}).get("trade_partner_name")
        for b in j.get("branches") or []
        for side in (b.get("debitor"), b.get("creditor"))
    )


def _bank_net(j: dict, bank_sub: str | None, side: str | None) -> int:
    dr = sum((b.get("debitor") or {}).get("value") or 0 for b in j.get("branches") or [] if (b.get("debitor") or {}).get("sub_account_id") == bank_sub)
    cr = sum((b.get("creditor") or {}).get("value") or 0 for b in j.get("branches") or [] if (b.get("creditor") or {}).get("sub_account_id") == bank_sub)
    return cr - dr if side == "EXPENSE" else dr - cr


def quality_report(journals: list[dict], transactions: list[dict], terms: list[dict], connected_accounts: list[dict], fiscal_years: list[int]) -> dict:
    term_of = term_lookup(terms)
    bank_sub_of = {c.get("id"): c.get("sub_account_id") for a in connected_accounts or [] for c in a.get("connected_sub_accounts") or []}
    fy_of = lambda d: term_of(d).get("fiscal_year")

    journals_by_tx: dict[str, list[dict]] = defaultdict(list)
    for j in journals:
        if j.get("transaction_id"):
            journals_by_tx[j["transaction_id"]].append(j)
    tx_ids = {t["id"] for t in transactions}

    # content の出現回数は全期間で数える（同じ店・同じ振込先の繰り返しを見るため）
    content_counts = Counter((t.get("content") or "").strip() for t in transactions if (t.get("content") or "").strip())

    def block(js: list[dict], txs: list[dict]) -> dict:
        normal = [j for j in js if j.get("entered_by") != OPENING]
        journalized = [t for t in txs if t.get("journalizing_status") in JOURNALIZED]
        linked = [t for t in txs if journals_by_tx.get(t["id"])]
        multi_journal = [t for t in linked if len(journals_by_tx[t["id"]]) > 1]
        amount_ok = [
            t for t in linked
            if sum(_bank_net(j, bank_sub_of.get(t.get("connected_sub_account_id")), t.get("side")) for j in journals_by_tx[t["id"]]) == t.get("value")
        ]
        contents = [(t.get("content") or "").strip() for t in txs]
        filled = [c for c in contents if c]
        with_tx_id = [j for j in normal if j.get("transaction_id")]
        return {
            "journals_total": len(js),
            "journals_normal": len(normal),
            "journals_entered_by": dict(Counter(j.get("entered_by") for j in js)),
            "journals_with_transaction_id": len(with_tx_id),
            "journals_with_transaction_id_matched": sum(1 for j in with_tx_id if j["transaction_id"] in tx_ids),
            "journals_multi_branch": sum(1 for j in normal if len(j.get("branches") or []) > 1),
            "journals_with_remark": sum(1 for j in normal if any(b.get("remark") for b in j.get("branches") or [])),
            "journals_with_remark_rate": _rate(sum(1 for j in normal if any(b.get("remark") for b in j.get("branches") or [])), len(normal)),
            "journals_with_trade_partner": sum(1 for j in normal if _has_partner(j)),
            "journals_with_trade_partner_rate": _rate(sum(1 for j in normal if _has_partner(j)), len(normal)),
            "transactions": len(txs),
            "transactions_status": dict(Counter(t.get("journalizing_status") for t in txs)),
            "transactions_side": dict(Counter(t.get("side") for t in txs)),
            "transactions_journalized": len(journalized),
            "transactions_linked": len(linked),
            "transactions_linked_rate": _rate(len(linked), len(txs)),
            "transactions_linked_rate_of_journalized": _rate(sum(1 for t in journalized if journals_by_tx.get(t["id"])), len(journalized)),
            "transactions_multi_journal": len(multi_journal),
            "transactions_amount_match": len(amount_ok),
            "transactions_amount_match_rate": _rate(len(amount_ok), len(linked)),
            "content_present": len(filled),
            "content_present_rate": _rate(len(filled), len(txs)),
            "content_empty": len(txs) - len(filled),
            "content_repeated_rows": sum(1 for c in filled if content_counts[c] > 1),
            "content_repeated_distinct": len({c for c in filled if content_counts[c] > 1}),
            "content_unique_rows": sum(1 for c in filled if content_counts[c] == 1),
        }

    by_fy = {}
    for fy in fiscal_years:
        by_fy[fy] = block([j for j in journals if fy_of(j.get("transaction_date")) == fy], [t for t in transactions if fy_of(t.get("date")) == fy])
    total = block(
        [j for j in journals if fy_of(j.get("transaction_date")) in fiscal_years],
        [t for t in transactions if fy_of(t.get("date")) in fiscal_years],
    )
    return {"by_fiscal_year": by_fy, "total": total}


ROWS = [
    ("仕訳総件数", "journals_total", None),
    ("通常仕訳件数", "journals_normal", None),
    ("連携明細件数", "transactions", None),
    ("仕訳化済み明細", "transactions_journalized", None),
    ("明細→仕訳 紐付き", "transactions_linked", "transactions_linked_rate"),
    ("  (仕訳化済みに対する率)", None, "transactions_linked_rate_of_journalized"),
    ("1明細→複数仕訳", "transactions_multi_journal", None),
    ("複数branch仕訳(通常)", "journals_multi_branch", None),
    ("口座側純額=明細金額", "transactions_amount_match", "transactions_amount_match_rate"),
    ("content あり", "content_present", "content_present_rate"),
    ("remark あり(通常仕訳)", "journals_with_remark", "journals_with_remark_rate"),
    ("取引先あり(通常仕訳)", "journals_with_trade_partner", "journals_with_trade_partner_rate"),
    ("通常仕訳で transaction_id あり", "journals_with_transaction_id", None),
    ("content 空欄", "content_empty", None),
    ("content 複数回出現(行)", "content_repeated_rows", None),
    ("content 複数回出現(種類)", "content_repeated_distinct", None),
    ("content 1回のみ(行)", "content_unique_rows", None),
]


def format_report(report: dict) -> str:
    cols = [(f"FY{fy}", b) for fy, b in report["by_fiscal_year"].items()] + [("合計", report["total"])]
    lines = ["| 項目 | " + " | ".join(c for c, _ in cols) + " |", "|---|" + "---:|" * len(cols)]
    for label, key, rate in ROWS:
        cells = []
        for _, b in cols:
            v = f"{b[key]:,}" if key else ""
            if rate and b.get(rate) is not None:
                v = f"{v} ({b[rate] * 100:.1f}%)" if v else f"{b[rate] * 100:.1f}%"
            cells.append(v or "-")
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    return "\n".join(lines)
