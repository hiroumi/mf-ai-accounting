"""認証・ページネーション・リトライのテスト（モック。実APIには接続しない）。"""

import json

import pytest
import requests
from requests.adapters import BaseAdapter

from mf_accounting.auth import AuthError, TokenProvider
from mf_accounting.client import MFAccountingClient, MFApiError
from mf_accounting.guard import GuardedSession


class FakeAdapter(BaseAdapter):
    def __init__(self, handler):
        super().__init__()
        self.handler = handler
        self.sent = []

    def send(self, request, **kwargs):
        self.sent.append(request)
        status, body, headers = self.handler(request, len(self.sent))
        resp = requests.Response()
        resp.status_code = status
        resp._content = json.dumps(body).encode()
        resp.headers.update(headers or {})
        resp.url = request.url
        resp.request = request
        return resp

    def close(self):
        pass


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def make(handler):
    clock = FakeClock()
    session = GuardedSession()
    adapter = FakeAdapter(handler)
    session.mount("https://", adapter)
    tokens = TokenProvider("mf_api_prd_TESTKEY", session, clock=clock, sleep=clock.sleep)
    client = MFAccountingClient(tokens, session, office_code="1234-5678", clock=clock, sleep=clock.sleep)
    return client, adapter, clock


def token_ok(request):
    assert request.headers["Authorization"] == "Bearer mf_api_prd_TESTKEY"
    return 200, {"access_token": "jwt-1", "token_type": "Bearer", "expires_in": 3600}, None


def test_pagination_fetches_all_pages_and_sends_office_code():
    def handler(req, n):
        if req.url.endswith("/auth/exchange"):
            return token_ok(req)
        assert req.method == "GET"
        assert req.headers["Authorization"] == "Bearer jwt-1"
        assert "office_code=1234-5678" in req.url
        page = int(req.url.split("page=")[1].split("&")[0])
        items = [{"id": f"{page}-{i}"} for i in range(2 if page < 3 else 1)]
        return 200, {"journals": items, "metadata": {"total_count": 5, "total_pages": 3}}, None

    client, adapter, _ = make(handler)
    items, meta = client.get_paginated("/api/v3/journals", "journals", {"start_date": "2024-04-01"}, per_page=2)
    assert [i["id"] for i in items] == ["1-0", "1-1", "2-0", "2-1", "3-0"]
    assert meta["pages_fetched"] == 3 and meta["truncated"] is False
    # トークン交換は1回だけ（キャッシュ）
    assert sum(r.url.endswith("/auth/exchange") for r in adapter.sent) == 1


def test_max_items_stops_early():
    def handler(req, n):
        if req.url.endswith("/auth/exchange"):
            return token_ok(req)
        return 200, {"journals": [{"id": i} for i in range(10)], "metadata": {"total_count": 100, "total_pages": 10}}, None

    client, adapter, _ = make(handler)
    items, meta = client.get_paginated("/api/v3/journals", "journals", per_page=10, max_items=3)
    assert len(items) == 3 and meta["truncated"] is True
    assert sum("/api/v3/journals" in r.url for r in adapter.sent) == 1


def test_retry_on_429_and_500():
    calls = {"n": 0}

    def handler(req, n):
        if req.url.endswith("/auth/exchange"):
            return token_ok(req)
        calls["n"] += 1
        if calls["n"] == 1:
            return 429, {"errors": []}, {"Retry-After": "1"}
        if calls["n"] == 2:
            return 503, {"errors": []}, None
        return 200, {"accounts": [{"id": "a"}]}, None

    client, _, _ = make(handler)
    assert client.get("/api/v3/accounts") == {"accounts": [{"id": "a"}]}
    assert calls["n"] == 3


def test_401_refreshes_token_once():
    state = {"tokens": 0, "gets": 0}

    def handler(req, n):
        if req.url.endswith("/auth/exchange"):
            state["tokens"] += 1
            return 200, {"access_token": f"jwt-{state['tokens']}", "expires_in": 3600}, None
        state["gets"] += 1
        if req.headers["Authorization"] == "Bearer jwt-1":
            return 401, {"errors": [{"code": "unauthorized"}]}, None
        return 200, {"ok": True}, None

    client, _, _ = make(handler)
    assert client.get("/api/v3/offices") == {"ok": True}
    assert state["tokens"] == 2


def test_403_raises_without_retry():
    def handler(req, n):
        if req.url.endswith("/auth/exchange"):
            return token_ok(req)
        return 403, {"errors": [{"code": "forbidden", "message": "権限がありません"}]}, None

    client, adapter, _ = make(handler)
    with pytest.raises(MFApiError) as e:
        client.get("/api/v3/taxes")
    assert e.value.status == 403
    assert sum("/api/v3/taxes" in r.url for r in adapter.sent) == 1


def test_token_refreshed_before_expiry():
    count = {"n": 0}

    def handler(req, n):
        if req.url.endswith("/auth/exchange"):
            count["n"] += 1
            return 200, {"access_token": f"jwt-{count['n']}", "expires_in": 3600}, None
        return 200, {}, None

    client, _, clock = make(handler)
    client.get("/api/v3/offices")
    clock.t += 3600 - 299  # 期限の5分前を過ぎた
    client.get("/api/v3/offices")
    assert count["n"] == 2


def test_invalid_api_key():
    client, _, _ = make(lambda req, n: (401, {"error": "invalid"}, None))
    with pytest.raises(AuthError):
        client.get("/api/v3/offices")


def test_rate_limit_interval():
    times = []

    def handler(req, n):
        if req.url.endswith("/auth/exchange"):
            return token_ok(req)
        times.append(clock.t)
        return 200, {}, None

    client, _, clock = make(handler)
    for _ in range(4):
        client.get("/api/v3/offices")
    assert all(b - a >= 0.4 - 1e-9 for a, b in zip(times, times[1:]))


def test_token_provider_repr_hides_key():
    tp = TokenProvider("mf_api_prd_SECRET", GuardedSession())
    assert "SECRET" not in repr(tp)


def test_transaction_ids_not_double_encoded():
    from datetime import date

    from mf_accounting import endpoints as ep

    def handler(req, n):
        if req.url.endswith("/auth/exchange"):
            return token_ok(req)
        assert "transaction_ids=Bow%2B3QQ%3D%3D&transaction_ids=Cow%2F1" in req.url
        assert "%25" not in req.url
        return 200, {"journals": [], "metadata": {"total_count": 0, "total_pages": 0}}, None

    client, _, _ = make(handler)
    ep.journals(client, date(2024, 8, 1), date(2025, 7, 31), per_page=10, transaction_ids=["Bow%2B3QQ%3D%3D", "Cow%2F1"])
