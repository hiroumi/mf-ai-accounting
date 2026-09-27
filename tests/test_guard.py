"""書き込み防止ガードのテスト。ネットワークには一切接続しない。"""

import pytest
import requests
from requests.adapters import BaseAdapter

from mf_accounting.guard import ForbiddenRequestError, GuardedSession, check_request_allowed

ACC = "https://api-accounting.moneyforward.com/api/v3"
EXCHANGE = "https://api.biz.moneyforward.com/auth/exchange"


class RecordingAdapter(BaseAdapter):
    """実際には送信せず、ここまで到達したリクエストを記録する。"""

    def __init__(self):
        super().__init__()
        self.sent = []

    def send(self, request, **kwargs):
        self.sent.append((request.method, request.url))
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"{}"
        resp.url = request.url
        resp.request = request
        return resp

    def close(self):
        pass


@pytest.fixture
def session():
    s = GuardedSession()
    adapter = RecordingAdapter()
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.adapter = adapter
    return s


@pytest.mark.parametrize("path", ["/journals", "/transactions", "/accounts", "/accessible_offices"])
def test_get_accounting_api_allowed(path):
    check_request_allowed("GET", ACC + path)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
@pytest.mark.parametrize(
    "path", ["/journals", "/journals/abc", "/vouchers", "/transactions", "/transactions/journalize", "/trade_partners"]
)
def test_non_get_accounting_api_forbidden(method, path):
    with pytest.raises(ForbiddenRequestError):
        check_request_allowed(method, ACC + path)


@pytest.mark.parametrize("method", ["post", "Post", "put", "delete", "patch"])
def test_method_case_insensitive(method):
    with pytest.raises(ForbiddenRequestError):
        check_request_allowed(method, ACC + "/journals")


def test_auth_exchange_post_allowed():
    check_request_allowed("POST", EXCHANGE)


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "PATCH"])
def test_auth_exchange_other_methods_forbidden(method):
    with pytest.raises(ForbiddenRequestError):
        check_request_allowed(method, EXCHANGE)


@pytest.mark.parametrize(
    "method,url",
    [
        ("POST", "https://api.biz.moneyforward.com/token"),
        ("POST", "https://api.biz.moneyforward.com/auth/exchange/extra"),
        ("GET", "https://api.biz.moneyforward.com/v2/tenant/tenant_user"),
        ("GET", "http://api-accounting.moneyforward.com/api/v3/journals"),
        ("GET", "https://api-accounting.moneyforward.com:8443/api/v3/journals"),
        ("GET", "https://api-accounting.moneyforward.com/api/v2/journals"),
        ("GET", "https://api-accounting.moneyforward.com.evil.example/api/v3/journals"),
        ("GET", "https://api-accounting.moneyforward.com@evil.example/api/v3/journals"),
        ("POST", "https://evil.example/auth/exchange"),
    ],
)
def test_other_destinations_forbidden(method, url):
    with pytest.raises(ForbiddenRequestError):
        check_request_allowed(method, url)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_session_blocks_before_sending(session, method):
    with pytest.raises(ForbiddenRequestError):
        session.request(method, ACC + "/journals", json={"journal": {}})
    assert session.adapter.sent == []


def test_session_passes_allowed_requests(session):
    session.get(ACC + "/journals", params={"office_code": "0000-0000"})
    session.post(EXCHANGE)
    assert [m for m, _ in session.adapter.sent] == ["GET", "POST"]


def test_session_blocks_prepared_request_send(session):
    req = requests.Request("DELETE", ACC + "/journals/abc").prepare()
    with pytest.raises(ForbiddenRequestError):
        session.send(req)
    assert session.adapter.sent == []


def test_client_has_no_write_methods():
    from mf_accounting.client import MFAccountingClient

    for name in ("post", "put", "patch", "delete", "request"):
        assert not hasattr(MFAccountingClient, name)
