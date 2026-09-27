"""会計APIのGETエンドポイント。仕様: https://developers.api-accounting.moneyforward.com/v3/openapi.yaml"""

from datetime import date, timedelta
from urllib.parse import unquote

from .client import MFAccountingClient

# マスター系: (保存名, パス, レスポンスのリストキー or None=オブジェクト)
MASTER_ENDPOINTS = [
    ("office", "/api/v3/offices", None),
    ("term_settings", "/api/v3/term_settings", "term_settings"),
    ("accounts", "/api/v3/accounts", "accounts"),
    ("sub_accounts", "/api/v3/sub_accounts", "sub_accounts"),
    ("taxes", "/api/v3/taxes", "taxes"),
    ("departments", "/api/v3/departments", "departments"),
    ("trade_partners", "/api/v3/trade_partners", "trade_partners"),
    ("connected_accounts", "/api/v3/connected_accounts", "connected_accounts"),
]

TRANSACTIONS_MAX_SPAN_DAYS = 366  # start_date と end_date の差の上限（仕様）


def accessible_offices(client: MFAccountingClient) -> dict:
    return client.get("/api/v3/accessible_offices", office_scoped=False)


def term_settings(client: MFAccountingClient) -> list[dict]:
    return client.get("/api/v3/term_settings").get("term_settings") or []


OPENING_ENTERED_BY = "JOURNAL_TYPE_OPENING"
TRANSACTION_IDS_MAX = 50  # transaction_ids の最大指定数（仕様）


def journals(
    client: MFAccountingClient,
    start_date: date,
    end_date: date,
    *,
    per_page: int,
    max_items: int | None = None,
    transaction_ids: list[str] | None = None,
):
    """仕訳一覧。指定日を含む会計期間の仕訳のみ返るため、会計期間ごとに呼ぶこと。

    transaction_ids を指定すると、その明細から作られた仕訳のみ返る。
    """
    params: dict = {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()}
    if transaction_ids:
        if len(transaction_ids) > TRANSACTION_IDS_MAX:
            raise ValueError(f"transaction_ids は {TRANSACTION_IDS_MAX} 件以内で指定してください")
        # APIのIDはURLエンコード済みの文字列（例: ...%2B...%3D%3D）。requests が再エンコードするため一度デコードする
        params["transaction_ids"] = [unquote(t) for t in transaction_ids]
    return client.get_paginated("/api/v3/journals", "journals", params, per_page=per_page, max_items=max_items)


def transactions(client: MFAccountingClient, start_date: date, end_date: date, *, per_page: int, max_items: int | None = None):
    """連携サービスの明細一覧（取得のみ。仕訳化 /transactions/journalize は使用しない）。"""
    if (end_date - start_date).days > TRANSACTIONS_MAX_SPAN_DAYS:
        raise ValueError(f"transactions の期間は {TRANSACTIONS_MAX_SPAN_DAYS} 日以内にしてください")
    return client.get_paginated(
        "/api/v3/transactions",
        "transactions",
        {"start_date": start_date.isoformat(), "end_date": end_date.isoformat(), "order": "asc"},
        per_page=per_page,
        max_items=max_items,
    )


def split_date_range(start: date, end: date, max_span_days: int = 365) -> list[tuple[date, date]]:
    """[start, end] を差が max_span_days 以下の区間に分割する。"""
    windows = []
    cur = start
    while cur <= end:
        w_end = min(cur + timedelta(days=max_span_days), end)
        windows.append((cur, w_end))
        cur = w_end + timedelta(days=1)
    return windows


def periods_from_term_settings(terms: list[dict], clip_start: date | None = None, clip_end: date | None = None) -> list[dict]:
    """term_settings を古い順に並べ、任意の範囲で切り詰めた会計期間リストを返す。"""
    periods = []
    seen = set()
    for t in sorted(terms, key=lambda t: t["start_date"]):
        s, e = date.fromisoformat(t["start_date"]), date.fromisoformat(t["end_date"])
        if (s, e) in seen:
            continue
        seen.add((s, e))
        if clip_start and e < clip_start or clip_end and s > clip_end:
            continue
        periods.append(
            {
                "fiscal_year": t.get("fiscal_year"),
                "start_date": max(s, clip_start) if clip_start else s,
                "end_date": min(e, clip_end) if clip_end else e,
            }
        )
    return periods
