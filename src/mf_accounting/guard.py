"""送信前にすべてのHTTPリクエストを検査する書き込み防止ガード。

許可リスト方式。ここに列挙したもの以外は送信前に例外で止める。

- 会計API (api-accounting.moneyforward.com /api/v3/*) : GET のみ
- 認証 (api.biz.moneyforward.com /auth/exchange)        : POST のみ（APIキー→JWT交換）
"""

from urllib.parse import urlsplit

import requests

ACCOUNTING_HOST = "api-accounting.moneyforward.com"
ACCOUNTING_PATH_PREFIX = "/api/v3/"
AUTH_HOST = "api.biz.moneyforward.com"
AUTH_EXCHANGE_PATH = "/auth/exchange"


class ForbiddenRequestError(RuntimeError):
    """許可されていないHTTPリクエストを送ろうとした。"""


def check_request_allowed(method: str, url: str) -> None:
    """許可されたリクエストでなければ ForbiddenRequestError を送出する。"""
    m = (method or "").upper()
    parts = urlsplit(url)

    if parts.scheme != "https":
        raise ForbiddenRequestError(f"HTTPS以外は禁止: {m} {parts.scheme}://{parts.hostname}")
    if parts.port not in (None, 443):
        raise ForbiddenRequestError(f"443以外のポートは禁止: {m} {parts.hostname}:{parts.port}")
    if parts.username or parts.password:
        raise ForbiddenRequestError(f"URLに認証情報を含むリクエストは禁止: {m} {parts.hostname}")

    host = parts.hostname
    if host == ACCOUNTING_HOST:
        if m != "GET":
            raise ForbiddenRequestError(f"会計APIへの {m} は禁止されています（GETのみ許可）: {parts.path}")
        if not parts.path.startswith(ACCOUNTING_PATH_PREFIX):
            raise ForbiddenRequestError(f"許可されていない会計APIパス: {parts.path}")
        return

    if host == AUTH_HOST and parts.path == AUTH_EXCHANGE_PATH:
        if m != "POST":
            raise ForbiddenRequestError(f"認証エンドポイントへの {m} は禁止されています")
        return

    raise ForbiddenRequestError(f"許可されていない送信先: {m} {host}{parts.path}")


class GuardedSession(requests.Session):
    """送信直前（リダイレクト時を含む）に必ずガードを通すSession。"""

    def send(self, request, **kwargs):
        check_request_allowed(request.method, request.url)
        return super().send(request, **kwargs)
