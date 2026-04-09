"""Discover trader wallets from leaderboard and whale tracking."""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polymarket.clients.leaderboard import LeaderboardClient
from polymarket.clients.polymarketscan import PolymarketScanClient
from polymarket.models.trader import Trader

logger = logging.getLogger(__name__)


async def discover_and_upsert_traders(
    session: AsyncSession,
    leaderboard: LeaderboardClient,
    scan_client: PolymarketScanClient | None = None,
    alltime_limit: int = 500,
    monthly_limit: int = 200,
) -> list[Trader]:
    """Discover traders from leaderboard + whale tracking, upsert into DB."""

    # 1. Leaderboard wallets
    wallets = await leaderboard.discover_wallets(alltime_limit, monthly_limit)

    # 2. PolymarketScan whale wallets (optional enrichment)
    if scan_client:
        try:
            whales = await scan_client.get_whales()
            for whale in whales:
                w = whale.get("address") or whale.get("wallet")
                if w and w not in wallets:
                    wallets.append(w)
            logger.info("Added %d whale wallets from PolymarketScan", len(whales))
        except Exception as e:
            logger.warning("Failed to fetch whale wallets: %s", e)

    # 3. Upsert into DB
    traders: list[Trader] = []
    for wallet in wallets:
        result = await session.execute(
            select(Trader).where(Trader.proxy_wallet == wallet)
        )
        trader = result.scalar_one_or_none()
        if trader is None:
            trader = Trader(
                proxy_wallet=wallet,
                first_seen_at=datetime.utcnow(),
                is_active=True,
            )
            session.add(trader)
        traders.append(trader)

    await session.flush()
    logger.info("Upserted %d traders into database", len(traders))
    return traders
