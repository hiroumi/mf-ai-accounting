"""保存済みJSONを分析しやすいCSVに変換する。

- journal_lines.csv            : 仕訳の明細行（branch）1つ = 1行、借方/貸方を横持ち
- transactions.csv             : 連携明細 1件 = 1行
- training_pairs.csv           : 元明細 → 採用された仕訳（journal.transaction_id で結合）
                                 1行 = (明細, 仕訳の明細行branch)。複数行仕訳は潰さず journal_id + branch_index で復元可能
- {master}.csv                 : マスター（ネストはドット区切りで平坦化）
"""

import csv
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

SIDE_FIELDS = [
    ("account_id", "account_id"),
    ("account_name", "account_name"),
    ("sub_account_id", "sub_account_id"),
    ("sub_account_name", "sub_account_name"),
    ("tax_id", "tax_id"),
    ("tax_name", "tax_name"),
    ("tax_long_name", "tax_long_name"),
    ("value", "value"),
    ("tax_value", "tax_value"),
    ("department_id", "department_id"),
    ("department_name", "department_name"),
    ("trade_partner_code", "trade_partner_code"),
    ("trade_partner_name", "trade_partner_name"),
    ("invoice_kind", "invoice_kind"),
]

JOURNAL_FIELDS = [
    "journal_id",
    "branch_index",
    "branch_count",
    "number",
    "transaction_date",
    "term_period",
    "journal_type",
    "entered_by",
    "is_realized",
    "memo",
    "tags",
    "remark",
    "transaction_id",
    "voucher_file_count",
    "create_time",
    "update_time",
    "fiscal_year",
    "term_tax_method",
    "term_accounting_method",
]

JOURNAL_LINE_COLUMNS = (
    JOURNAL_FIELDS + [f"debit_{c}" for c, _ in SIDE_FIELDS] + [f"credit_{c}" for c, _ in SIDE_FIELDS]
)

TRANSACTION_COLUMNS = [
    "transaction_id",
    "date",
    "value",
    "side",
    "content",
    "memo",
    "journalizing_status",
    "connected_account_id",
    "connected_account_name",
    "connected_sub_account_id",
    "connected_sub_account_name",
    "bank_sub_account_id",
    "voucher_file_count",
    "linked_journal_count",
    "fiscal_year",
    "term_tax_method",
]

TRAINING_PAIR_COLUMNS = (
    [
        "transaction_id",
        "tx_date",
        "tx_content",
        "tx_value",
        "tx_side",
        "tx_memo",
        "tx_journalizing_status",
        "tx_connected_account_id",
        "tx_connected_account_name",
        "tx_connected_sub_account_id",
        "tx_connected_sub_account_name",
        "tx_bank_sub_account_id",
        "fiscal_year",
        "term_tax_method",
        "term_accounting_method",
        "journal_id",
        "journal_number",
        "journal_transaction_date",
        "journal_type",
        "entered_by",
        "journal_memo",
        "journal_tags",
        "journals_per_transaction",
        "branch_index",
        "branch_count",
        "bank_side",
        "bank_net_amount",
        "amount_matches_tx",
        "remark",
    ]
    + [f"debit_{c}" for c, _ in SIDE_FIELDS]
    + [f"credit_{c}" for c, _ in SIDE_FIELDS]
)


def _cell(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return v


def term_lookup(terms: list[dict] | None):
    """取引日(YYYY-MM-DD) → その日を含む会計期間（term_settings の要素）を返す関数。"""
    ranges = sorted((t["start_date"], t["end_date"], t) for t in terms or [])

    def find(d: str | None) -> dict:
        if d:
            for start, end, t in ranges:
                if start <= d[:10] <= end:
                    return t
        return {}

    return find


def _term_cols(t: dict) -> dict:
    return {
        "fiscal_year": t.get("fiscal_year"),
        "term_tax_method": t.get("tax_method"),
        "term_accounting_method": t.get("accounting_method"),
    }


def journal_lines(journals: Iterable[dict], terms: list[dict] | None = None) -> list[dict]:
    term_of = term_lookup(terms)
    rows = []
    for j in journals:
        base = {
            "journal_id": j.get("id"),
            "number": j.get("number"),
            "transaction_date": j.get("transaction_date"),
            "term_period": j.get("term_period"),
            "journal_type": j.get("journal_type"),
            "entered_by": j.get("entered_by"),
            "is_realized": j.get("is_realized"),
            "memo": j.get("memo"),
            "tags": "|".join(j.get("tags") or []),
            "transaction_id": j.get("transaction_id"),
            "voucher_file_count": len(j.get("voucher_file_ids") or []),
            "create_time": j.get("create_time"),
            "update_time": j.get("update_time"),
            "branch_count": len(j.get("branches") or []),
            **_term_cols(term_of(j.get("transaction_date"))),
        }
        for i, br in enumerate(j.get("branches") or [], start=1):
            row = {**base, "branch_index": i, "remark": br.get("remark")}
            for prefix, side in (("debit", br.get("debitor") or {}), ("credit", br.get("creditor") or {})):
                for col, key in SIDE_FIELDS:
                    row[f"{prefix}_{col}"] = side.get(key)
            rows.append(row)
    return rows


def _connected_maps(connected_accounts: list[dict] | None) -> tuple[dict, dict]:
    accounts = {a.get("id"): a for a in connected_accounts or []}
    subs = {c.get("id"): c for a in connected_accounts or [] for c in a.get("connected_sub_accounts") or []}
    return accounts, subs


def transaction_rows(
    transactions: Iterable[dict],
    connected_accounts: list[dict] | None = None,
    terms: list[dict] | None = None,
    linked_counts: dict[str, int] | None = None,
) -> list[dict]:
    accounts, subs = _connected_maps(connected_accounts)
    term_of = term_lookup(terms)
    rows = []
    for t in transactions:
        sub = subs.get(t.get("connected_sub_account_id"), {})
        term = term_of(t.get("date"))
        rows.append(
            {
                "transaction_id": t.get("id"),
                "date": t.get("date"),
                "value": t.get("value"),
                "side": t.get("side"),
                "content": t.get("content"),
                "memo": t.get("memo"),
                "journalizing_status": t.get("journalizing_status"),
                "connected_account_id": t.get("connected_account_id"),
                "connected_account_name": accounts.get(t.get("connected_account_id"), {}).get("name"),
                "connected_sub_account_id": t.get("connected_sub_account_id"),
                "connected_sub_account_name": sub.get("name"),
                "bank_sub_account_id": sub.get("sub_account_id"),  # 仕訳側で口座を表す補助科目
                "voucher_file_count": len(t.get("voucher_file_ids") or []),
                "linked_journal_count": (linked_counts or {}).get(t.get("id"), 0),
                "fiscal_year": term.get("fiscal_year"),
                "term_tax_method": term.get("tax_method"),
            }
        )
    return rows


def _bank_net(journal: dict, bank_sub: str | None, side: str | None) -> int | None:
    """仕訳のうち口座（連携口座の補助科目）側の純額。支出なら貸方−借方、入金なら借方−貸方。"""
    if not bank_sub:
        return None
    dr = sum((b.get("debitor") or {}).get("value") or 0 for b in journal.get("branches") or [] if (b.get("debitor") or {}).get("sub_account_id") == bank_sub)
    cr = sum((b.get("creditor") or {}).get("value") or 0 for b in journal.get("branches") or [] if (b.get("creditor") or {}).get("sub_account_id") == bank_sub)
    return cr - dr if side == "EXPENSE" else dr - cr


def training_pairs(tx_rows: list[dict], journals: list[dict], terms: list[dict] | None = None) -> tuple[list[dict], dict]:
    """元明細と、その明細から作られた仕訳を結合する。

    1行 = (明細, 仕訳の明細行)。複数行仕訳・1明細に複数仕訳の場合も行を潰さない。
    分類ロジックは含まない（fiscal_year / term_tax_method 等は将来の判断材料として保持するだけ）。
    """
    term_of = term_lookup(terms)
    tx_by_id = {r["transaction_id"]: r for r in tx_rows}
    journals_by_tx: dict[str, list[dict]] = {}
    for j in journals:
        if j.get("transaction_id") in tx_by_id:
            journals_by_tx.setdefault(j["transaction_id"], []).append(j)

    rows = []
    amount_match = 0
    for tx_id, js in journals_by_tx.items():
        tx = tx_by_id[tx_id]
        bank_sub = tx.get("bank_sub_account_id")
        for j in sorted(js, key=lambda j: (j.get("number") or 0)):
            net = _bank_net(j, bank_sub, tx.get("side"))
            matches = net == tx.get("value") if net is not None else None
            amount_match += bool(matches)
            branches = j.get("branches") or []
            base = {
                **{f"tx_{k}": tx.get(k) for k in ("date", "content", "value", "side", "memo", "journalizing_status")},
                "tx_connected_account_id": tx.get("connected_account_id"),
                "tx_connected_account_name": tx.get("connected_account_name"),
                "tx_connected_sub_account_id": tx.get("connected_sub_account_id"),
                "tx_connected_sub_account_name": tx.get("connected_sub_account_name"),
                "tx_bank_sub_account_id": bank_sub,
                **_term_cols(term_of(j.get("transaction_date") or tx.get("date"))),
                "transaction_id": tx_id,
                "journal_id": j.get("id"),
                "journal_number": j.get("number"),
                "journal_transaction_date": j.get("transaction_date"),
                "journal_type": j.get("journal_type"),
                "entered_by": j.get("entered_by"),
                "journal_memo": j.get("memo"),
                "journal_tags": "|".join(j.get("tags") or []),
                "journals_per_transaction": len(js),
                "branch_count": len(branches),
                "bank_net_amount": net,
                "amount_matches_tx": matches,
            }
            for i, br in enumerate(branches, start=1):
                d, c = br.get("debitor") or {}, br.get("creditor") or {}
                on_d = bool(bank_sub) and d.get("sub_account_id") == bank_sub
                on_c = bool(bank_sub) and c.get("sub_account_id") == bank_sub
                row = {
                    **base,
                    "branch_index": i,
                    "bank_side": "both" if on_d and on_c else "debit" if on_d else "credit" if on_c else "none",
                    "remark": br.get("remark"),
                }
                for prefix, side in (("debit", d), ("credit", c)):
                    for col, key in SIDE_FIELDS:
                        row[f"{prefix}_{col}"] = side.get(key)
                rows.append(row)

    n_tx = len(tx_rows)
    linked = len(journals_by_tx)
    n_journals = sum(len(v) for v in journals_by_tx.values())
    stats = {
        "transactions": n_tx,
        "transactions_linked": linked,
        "link_rate": round(linked / n_tx, 4) if n_tx else None,
        "journals_linked": n_journals,
        "journals_with_transaction_id": sum(1 for j in journals if j.get("transaction_id")),
        "journals_per_transaction": sorted({len(v) for v in journals_by_tx.values()}),
        "multi_branch_journals": sum(1 for v in journals_by_tx.values() for j in v if len(j.get("branches") or []) > 1),
        "amount_matches_tx": amount_match,
        "rows": len(rows),
    }
    return rows, stats


def flatten(obj: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in obj.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, key + "."))
        else:
            out[key] = v
    return out


def write_csv(path: Path, rows: list[dict], columns: list[str] | None = None) -> Path:
    if columns is None:
        columns = []
        for r in rows:
            for k in r:
                if k not in columns:
                    columns.append(k)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:  # BOM付き: Excelで文字化けしない
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: _cell(r.get(c)) for c in columns})
    return path
