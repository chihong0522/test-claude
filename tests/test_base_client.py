from __future__ import annotations

import asyncio

import httpx

from polymarket.clients.base import BaseClient


class _FakeResponse:
    def __init__(self, status_code: int, payload, headers: dict[str, str] | None = None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"unexpected status {self.status_code}")


def test_base_client_429_retry_after_zero_still_waits_positive_backoff(monkeypatch):
    client = BaseClient("https://example.test")
    sleeps: list[float] = []
    responses = iter(
        [
            _FakeResponse(429, [], {"Retry-After": "0"}),
            _FakeResponse(200, [{"ok": True}]),
        ]
    )

    async def fake_get(path, params=None):
        return next(responses)

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(client._client, "get", fake_get)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    try:
        result = asyncio.run(client.get("/trades", {"market": "abc"}))
    finally:
        asyncio.run(client.close())

    assert result == [{"ok": True}]
    assert sleeps
    assert sleeps[0] > 0


def test_base_client_get_paginated_keeps_earlier_results_on_http_400_offset_cap(monkeypatch):
    client = BaseClient("https://example.test")
    calls: list[int] = []

    async def fake_get(path, params=None):
        offset = params["offset"]
        calls.append(offset)
        if offset == 0:
            return [{"id": 1}, {"id": 2}]
        request = httpx.Request("GET", f"https://example.test{path}")
        response = httpx.Response(400, request=request)
        raise httpx.HTTPStatusError("offset cap", request=request, response=response)

    monkeypatch.setattr(client, "get", fake_get)

    try:
        result = asyncio.run(client.get_paginated("/trades", {"user": "wallet-1"}, limit=2, max_pages=5))
    finally:
        asyncio.run(client.close())

    assert result == [{"id": 1}, {"id": 2}]
    assert calls == [0, 2]
