"""Oddpool API client — cross-venue liquidity and orderbook data."""

from __future__ import annotations

import logging

from polymarket.clients.base import BaseClient
from polymarket.config import settings

logger = logging.getLogger(__name__)


class OddpoolClient(BaseClient):
    """Client for Oddpool's public API (cross-venue liquidity data)."""

    def __init__(self) -> None:
        super().__init__(
            base_url=settings.oddpool_api_url,
            rate_limit=500,  # conservative — no documented limit
        )

    async def get_book(self, market_id: str) -> dict | None:
        """Get aggregated orderbook from Oddpool."""
        try:
            return await self.get(f"/book/{market_id}")
        except Exception as e:
            logger.debug("Oddpool book fetch failed for %s: %s", market_id, e)
            return None

    async def get_snapshot(self, market_id: str) -> dict | None:
        """Get orderbook snapshot."""
        try:
            return await self.get(f"/snapshot/{market_id}")
        except Exception as e:
            logger.debug("Oddpool snapshot failed for %s: %s", market_id, e)
            return None

    async def get_distribution(self, market_id: str) -> dict | None:
        """Get probability distribution (free tier)."""
        try:
            return await self.get(f"/dist/{market_id}")
        except Exception as e:
            logger.debug("Oddpool dist failed for %s: %s", market_id, e)
            return None
