"""APIキーをJWTに交換し、メモリ上でキャッシュする。

https://developers.biz.moneyforward.com/docs/tutorials/api-keys/step-2-exchange-for-jwt
APIキーやJWTはログ・ファイルに一切出力しない。
"""

import logging
import time
from collections.abc import Callable

import requests

from .guard import AUTH_EXCHANGE_PATH, AUTH_HOST

logger = logging.getLogger(__name__)

EXCHANGE_URL = f"https://{AUTH_HOST}{AUTH_EXCHANGE_PATH}"
REFRESH_MARGIN_SEC = 300  # 期限の5分前に更新（公式推奨）


class AuthError(RuntimeError):
    pass


class TokenProvider:
    def __init__(
        self,
        api_key: str,
        session: requests.Session,
        *,
        max_retries: int = 3,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._api_key = api_key
        self._session = session
        self._max_retries = max_retries
        self._clock = clock
        self._sleep = sleep
        self._token: str | None = None
        self._expires_at = 0.0

    def __repr__(self) -> str:  # 誤って表示してもキーが出ないように
        return "TokenProvider(<redacted>)"

    def get_token(self) -> str:
        if self._token is None or self._clock() >= self._expires_at - REFRESH_MARGIN_SEC:
            self._exchange()
        assert self._token is not None
        return self._token

    def invalidate(self) -> None:
        self._token = None
        self._expires_at = 0.0

    def _exchange(self) -> None:
        last_error = ""
        for attempt in range(self._max_retries):
            try:
                resp = self._session.post(
                    EXCHANGE_URL,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    timeout=10,
                    allow_redirects=False,
                )
            except requests.RequestException as e:
                last_error = f"通信エラー: {type(e).__name__}"
                self._sleep(2.0**attempt)
                continue

            if resp.status_code == 200:
                body = resp.json()
                self._token = body["access_token"]
                self._expires_at = self._clock() + float(body.get("expires_in", 3600))
                logger.info("JWTを取得しました（有効期限 %s 秒）", body.get("expires_in"))
                return
            if resp.status_code == 401:
                raise AuthError("APIキーが無効です（401）。.env の MF_API_KEY を確認してください。")
            if resp.status_code == 429:
                last_error = "レート制限（429）"
                logger.warning("トークン交換がレート制限に達しました。60秒待機します。")
                self._sleep(60)
                continue
            if resp.status_code >= 500:
                last_error = f"サーバーエラー（{resp.status_code}）"
                self._sleep(2.0**attempt)
                continue
            raise AuthError(f"トークン交換に失敗しました: HTTP {resp.status_code}")

        raise AuthError(f"トークン交換に {self._max_retries} 回失敗しました: {last_error}")
