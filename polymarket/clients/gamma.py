"""Gamma API client — markets, events, liquidity data."""

from __future__ import annotations

from typing import Any

from polymarket.clients.base import BaseClient
from polymarket.config import settings


class GammaClient(BaseClient):
    def __init__(self) -> None:
        super().__init__(
            base_url=settings.gamma_api_url,
            rate_limit=settings.gamma_rate_limit,
        )

    async def get_markets(
        self,
        active: bool | None = True,
        closed: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if active is not None:
            params["active"] = str(active).lower()
        if closed is not None:
            params["closed"] = str(closed).lower()
        data = await self.get("/markets", params)
        return data if isinstance(data, list) else []

    async def get_market_by_condition(self, condition_id: str) -> dict | None:
        data = await self.get("/markets", {"condition_id": condition_id})
        if isinstance(data, list) and data:
            return data[0]
        return None

    async def get_market_by_slug(self, slug: str) -> dict | None:
        data = await self.get("/markets", {"slug": slug})
        if isinstance(data, list) and data:
            return data[0]
        return None

    async def get_events(
        self,
        active: bool | None = True,
        tag_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if active is not None:
            params["active"] = str(active).lower()
        if tag_id:
            params["tag_id"] = tag_id
        data = await self.get("/events", params)
        return data if isinstance(data, list) else []

    async def get_all_markets(self, max_pages: int = 50) -> list[dict]:
        """Fetch all markets with pagination."""
        return await self.get_paginated("/markets", {"active": "true"}, max_pages=max_pages)

    async def get_events_by_ticker(
        self,
        ticker: str,
        limit: int = 100,
        offset: int = 0,
        order: str = "startDate",
        ascending: bool = False,
    ) -> list[dict]:
        """Fetch events filtered by ticker (e.g. 'btc-updown-5m')."""
        params: dict[str, Any] = {
            "ticker": ticker,
            "limit": limit,
            "offset": offset,
            "order": order,
            "ascending": str(ascending).lower(),
        }
        data = await self.get("/events", params)
        return data if isinstance(data, list) else []

    async def get_market_liquidity(self, condition_id: str) -> float | None:
        """Get liquidity for a specific market."""
        market = await self.get_market_by_condition(condition_id)
        if market:
            try:
                return float(market.get("liquidity", 0))
            except (TypeError, ValueError):
                return None
        return None
