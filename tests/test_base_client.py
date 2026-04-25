from __future__ import annotations

import asyncio

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
