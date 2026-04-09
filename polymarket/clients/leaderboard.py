"""Leaderboard discovery — finds top trader wallets."""

from __future__ import annotations

import logging

from polymarket.clients.data_api import DataAPIClient

logger = logging.getLogger(__name__)


class LeaderboardClient:
    """Wraps the Data API leaderboard endpoint for trader discovery."""

    def __init__(self, data_client: DataAPIClient | None = None) -> None:
        self._data = data_client or DataAPIClient()
        self._owns_client = data_client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._data.close()

    async def get_top_traders(
        self,
        time_period: str = "ALL",
        max_entries: int = 500,
    ) -> list[dict]:
        """Fetch top traders by all-time P&L."""
        entries = await self._data.get_leaderboard_all(
            category="OVERALL",
            time_period=time_period,
            order_by="PNL",
            max_entries=max_entries,
        )
        logger.info("Fetched %d leaderboard entries (period=%s)", len(entries), time_period)
        return entries

    async def discover_wallets(
        self,
        alltime_limit: int = 500,
        monthly_limit: int = 200,
    ) -> list[str]:
        """Discover unique wallet addresses from multiple leaderboard views."""
        wallets: set[str] = set()

        # All-time top traders
        alltime = await self.get_top_traders("ALL", alltime_limit)
        for entry in alltime:
            w = entry.get("proxyWallet")
            if w:
                wallets.add(w)

        # 30-day rising stars
        monthly = await self.get_top_traders("MONTH", monthly_limit)
        for entry in monthly:
            w = entry.get("proxyWallet")
            if w:
                wallets.add(w)

        logger.info("Discovered %d unique wallets from leaderboard", len(wallets))
        return list(wallets)
