"""Polymarket Data API client — trades, positions, activity, leaderboard."""

from __future__ import annotations

from typing import Any

from polymarket.clients.base import BaseClient
from polymarket.config import settings


class DataAPIClient(BaseClient):
    def __init__(self) -> None:
        super().__init__(
            base_url=settings.data_api_url,
            rate_limit=settings.data_rate_limit,
        )

    # ── Trades ──────────────────────────────────────────────────────────

    async def get_trades(
        self,
        user: str | None = None,
        market: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if user:
            params["user"] = user
        if market:
            params["market"] = market
        data = await self.get("/trades", params)
        return data if isinstance(data, list) else []

    async def get_all_trades(self, wallet: str, max_pages: int = 200) -> list[dict]:
        """Paginated fetch of entire trade history for a wallet."""
        return await self.get_paginated("/trades", {"user": wallet}, max_pages=max_pages)

    # ── Positions ───────────────────────────────────────────────────────

    async def get_positions(self, wallet: str) -> list[dict]:
        data = await self.get("/positions", {"user": wallet})
        return data if isinstance(data, list) else []

    async def get_closed_positions(self, wallet: str) -> list[dict]:
        data = await self.get("/closed-positions", {"user": wallet})
        return data if isinstance(data, list) else []

    # ── Activity ────────────────────────────────────────────────────────

    async def get_activity(
        self,
        wallet: str,
        activity_type: str | None = None,
        side: str | None = None,
    ) -> list[dict]:
        params: dict[str, Any] = {"user": wallet}
        if activity_type:
            params["type"] = activity_type
        if side:
            params["side"] = side
        data = await self.get("/activity", params)
        return data if isinstance(data, list) else []

    # ── Portfolio value ─────────────────────────────────────────────────

    async def get_portfolio_value(self, wallet: str) -> float:
        data = await self.get("/value", {"user": wallet})
        if isinstance(data, list) and data:
            return float(data[0].get("value", 0))
        return 0.0

    # ── Leaderboard ─────────────────────────────────────────────────────

    async def get_leaderboard(
        self,
        category: str = "OVERALL",
        time_period: str = "ALL",
        order_by: str = "PNL",
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        params = {
            "category": category,
            "timePeriod": time_period,
            "orderBy": order_by,
            "limit": limit,
            "offset": offset,
        }
        data = await self.get("/v1/leaderboard", params)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("data", data.get("results", []))
        return []

    async def get_leaderboard_all(
        self,
        category: str = "OVERALL",
        time_period: str = "ALL",
        order_by: str = "PNL",
        max_entries: int = 500,
    ) -> list[dict]:
        """Fetch full leaderboard by paginating."""
        all_entries: list[dict] = []
        batch = 50
        for offset in range(0, max_entries, batch):
            entries = await self.get_leaderboard(
                category=category,
                time_period=time_period,
                order_by=order_by,
                limit=batch,
                offset=offset,
            )
            all_entries.extend(entries)
            if len(entries) < batch:
                break
        return all_entries

    # ── Public profile ──────────────────────────────────────────────────

    async def get_public_profile(self, address: str) -> dict | None:
        try:
            return await self.get("/public-profile", {"address": address})
        except Exception:
            return None
