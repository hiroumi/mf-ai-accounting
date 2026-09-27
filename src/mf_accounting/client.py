"""会計API用の読み取り専用クライアント。

GET 以外のメソッドは意図的に実装していない。さらに GuardedSession が送信前に検査する。
"""

import logging
import time
from collections.abc import Callable
from typing import Any

import requests

from .auth import TokenProvider
from .guard import ACCOUNTING_HOST

logger = logging.getLogger(__name__)

BASE_URL = f"https://{ACCOUNTING_HOST}"
MIN_INTERVAL_SEC = 0.4  # 公式レート制限: 1トークンあたり 3 req/秒


class MFApiError(RuntimeError):
    def __init__(self, status: int, path: str, errors: Any):
        self.status = status
        self.path = path
        self.errors = errors
        super().__init__(f"HTTP {status} {path}: {errors}")


class MFAccountingClient:
    def __init__(
        self,
        tokens: TokenProvider,
        session: requests.Session,
        *,
        office_code: str | None = None,
        max_retries: int = 5,
        min_interval: float = MIN_INTERVAL_SEC,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._tokens = tokens
        self._session = session
        self.office_code = office_code
        self._max_retries = max_retries
        self._min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._last_request_at: float | None = None

    def _throttle(self) -> None:
        if self._last_request_at is not None:
            wait = self._min_interval - (self._clock() - self._last_request_at)
            if wait > 0:
                self._sleep(wait)
        self._last_request_at = self._clock()

    def get(self, path: str, params: dict[str, Any] | None = None, *, office_scoped: bool = True) -> dict:
        query = {k: v for k, v in (params or {}).items() if v is not None}
        if office_scoped:
            if not self.office_code:
                raise ValueError(f"{path} には office_code が必要です")
            query["office_code"] = self.office_code

        refreshed = False
        for attempt in range(self._max_retries):
            self._throttle()
            try:
                resp = self._session.get(
                    BASE_URL + path,
                    params=query,
                    headers={"Authorization": f"Bearer {self._tokens.get_token()}", "Accept": "application/json"},
                    timeout=60,
                    allow_redirects=False,
                )
            except requests.RequestException as e:
                logger.warning("通信エラー（%s）。再試行します: %s", type(e).__name__, path)
                self._sleep(2.0**attempt)
                continue

            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 401 and not refreshed:
                self._tokens.invalidate()
                refreshed = True
                continue
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After") or 2.0 ** (attempt + 1))
                logger.warning("レート制限（429）。%.1f秒待機します: %s", wait, path)
                self._sleep(wait)
                continue
            if resp.status_code >= 500:
                logger.warning("サーバーエラー（%s）。再試行します: %s", resp.status_code, path)
                self._sleep(2.0 ** (attempt + 1))
                continue
            raise MFApiError(resp.status_code, path, _error_body(resp))

        raise MFApiError(-1, path, f"{self._max_retries}回の再試行に失敗しました")

    def get_paginated(
        self,
        path: str,
        items_key: str,
        params: dict[str, Any] | None = None,
        *,
        per_page: int,
        max_items: int | None = None,
    ) -> tuple[list[dict], dict]:
        """page / per_page / metadata.total_pages に従い全ページを取得する。

        max_items を指定すると、その件数に達した時点で打ち切る（少量取得用）。
        """
        items: list[dict] = []
        page = 1
        metadata: dict = {}
        while True:
            body = self.get(path, {**(params or {}), "page": page, "per_page": per_page})
            page_items = body.get(items_key) or []
            metadata = body.get("metadata") or {}
            items.extend(page_items)
            total_pages = int(metadata.get("total_pages") or 0)
            logger.info("%s page %d/%d: %d件", path, page, total_pages, len(page_items))
            if max_items is not None and len(items) >= max_items:
                items = items[:max_items]
                break
            if not page_items or page >= total_pages:
                break
            page += 1

        return items, {
            "total_count": metadata.get("total_count"),
            "total_pages": metadata.get("total_pages"),
            "pages_fetched": page,
            "fetched_count": len(items),
            "truncated": metadata.get("total_count") is not None and len(items) < int(metadata["total_count"]),
        }


def _error_body(resp: requests.Response) -> Any:
    try:
        return resp.json().get("errors", resp.text[:500])
    except ValueError:
        return resp.text[:500]
