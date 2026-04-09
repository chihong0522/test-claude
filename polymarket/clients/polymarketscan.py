"""PolymarketScan API client — whale tiers, anomaly badges, wallet profiles."""

from __future__ import annotations

import logging

from polymarket.clients.base import BaseClient
from polymarket.config import settings

logger = logging.getLogger(__name__)


class PolymarketScanClient(BaseClient):
    """Client for PolymarketScan's public API (30 req/min)."""

    def __init__(self) -> None:
        super().__init__(
            base_url=settings.polymarketscan_api_url,
            rate_limit=25,  # 30 req/min documented, use 25 to stay safe
        )

    async def get_wallet_profile(self, address: str) -> dict | None:
        """Get wallet PnL, volume, win rate, best trade."""
        try:
            return await self.get("/wallet_profile", {"address": address})
        except Exception as e:
            logger.debug("PolymarketScan profile failed for %s: %s", address, e)
            return None

    async def get_wallet_trades(
        self, address: str, limit: int = 50, offset: int = 0,
    ) -> list[dict]:
        """Get paginated trade history for a wallet."""
        try:
            data = await self.get(
                "/wallet_trades",
                {"address": address, "limit": limit, "offset": offset},
            )
            return data if isinstance(data, list) else []
        except Exception:
            return []

    async def get_whales(self) -> list[dict]:
        """Get recent whale trades (> $5,000 USD)."""
        try:
            data = await self.get("/whales")
            return data if isinstance(data, list) else []
        except Exception:
            return []

    async def get_leaderboards(self) -> list[dict]:
        """Get trader rankings."""
        try:
            data = await self.get("/leaderboards")
            return data if isinstance(data, list) else []
        except Exception:
            return []
