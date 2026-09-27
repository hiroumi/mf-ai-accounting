"""保存済みJSONを分析しやすいCSVに変換する。

- journal_lines.csv            : 仕訳の明細行（branch）1つ = 1行、借方/貸方を横持ち
- transactions.csv             : 連携明細 1件 = 1行
- transaction_journal_lines.csv: 元明細 → 採用された仕訳（journal.transaction_id で結合）
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
    "voucher_file_count",
]


def _cell(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return v


def journal_lines(journals: Iterable[dict]) -> list[dict]:
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
        }
        for i, br in enumerate(j.get("branches") or [], start=1):
            row = {**base, "branch_index": i, "remark": br.get("remark")}
            for prefix, side in (("debit", br.get("debitor") or {}), ("credit", br.get("creditor") or {})):
                for col, key in SIDE_FIELDS:
                    row[f"{prefix}_{col}"] = side.get(key)
            rows.append(row)
    return rows


def transaction_rows(transactions: Iterable[dict], connected_accounts: list[dict] | None = None) -> list[dict]:
    names = {a.get("id"): a.get("name") for a in connected_accounts or []}
    return [
        {
            "transaction_id": t.get("id"),
            "date": t.get("date"),
            "value": t.get("value"),
            "side": t.get("side"),
            "content": t.get("content"),
            "memo": t.get("memo"),
            "journalizing_status": t.get("journalizing_status"),
            "connected_account_id": t.get("connected_account_id"),
            "connected_account_name": names.get(t.get("connected_account_id")),
            "connected_sub_account_id": t.get("connected_sub_account_id"),
            "voucher_file_count": len(t.get("voucher_file_ids") or []),
        }
        for t in transactions
    ]


def transaction_journal_lines(tx_rows: list[dict], jl_rows: list[dict]) -> tuple[list[dict], dict]:
    """元明細と、その明細から作られた仕訳明細行を結合する。"""
    tx_by_id = {r["transaction_id"]: r for r in tx_rows}
    tx_cols = ["date", "value", "side", "content", "memo", "journalizing_status", "connected_account_name"]
    joined = []
    linked_journal_ids = set()
    for jl in jl_rows:
        tx = tx_by_id.get(jl.get("transaction_id")) if jl.get("transaction_id") else None
        if tx is None:
            continue
        linked_journal_ids.add(jl["journal_id"])
        joined.append({"transaction_id": tx["transaction_id"], **{f"tx_{c}": tx[c] for c in tx_cols}, **{k: v for k, v in jl.items() if k != "transaction_id"}})
    journal_with_tx = {jl["journal_id"] for jl in jl_rows if jl.get("transaction_id")}
    stats = {
        "journals_with_transaction_id": len(journal_with_tx),
        "journals_matched_to_fetched_transactions": len(linked_journal_ids),
        "joined_rows": len(joined),
    }
    return joined, stats


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
