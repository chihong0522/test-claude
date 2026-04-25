from __future__ import annotations

import pytest

from polymarket.collector.btc_5min_discovery import _fetch_all_trades_for_market


class _RetryThenSucceedDataAPI:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, int]] = []
        self._failures_remaining = {4: 1}

    async def get_trades(self, market: str, limit: int, offset: int):
        self.calls.append((market, limit, offset))
        if self._failures_remaining.get(offset, 0):
            self._failures_remaining[offset] -= 1
            raise RuntimeError("Failed after 5 retries on /trades")
        return {
            0: [{"id": 1}, {"id": 2}],
            2: [{"id": 3}, {"id": 4}],
            4: [{"id": 5}],
        }.get(offset, [])


class _PersistentLaterFailureDataAPI:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, int]] = []

    async def get_trades(self, market: str, limit: int, offset: int):
        self.calls.append((market, limit, offset))
        if offset == 2:
            raise RuntimeError("persistent later-page failure")
        return {
            0: [{"id": 1}, {"id": 2}],
            4: [{"id": 5}],
        }.get(offset, [])


@pytest.mark.asyncio
async def test_fetch_all_trades_retries_failed_page_instead_of_returning_partial_results():
    data_api = _RetryThenSucceedDataAPI()

    trades = await _fetch_all_trades_for_market(
        data_api,
        "condition-123",
        page_size=2,
        max_pages=5,
    )

    assert [trade["id"] for trade in trades] == [1, 2, 3, 4, 5]
    assert data_api.calls.count(("condition-123", 2, 4)) == 2


@pytest.mark.asyncio
async def test_fetch_all_trades_preserves_earlier_pages_when_a_later_page_keeps_failing():
    data_api = _PersistentLaterFailureDataAPI()

    trades = await _fetch_all_trades_for_market(
        data_api,
        "condition-123",
        page_size=2,
        max_pages=5,
    )

    assert [trade["id"] for trade in trades] == [1, 2]
    assert data_api.calls.count(("condition-123", 2, 2)) == 2
