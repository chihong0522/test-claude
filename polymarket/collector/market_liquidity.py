"""Fetch and cache market liquidity data."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polymarket.clients.clob import CLOBClient
from polymarket.clients.gamma import GammaClient
from polymarket.models.market import Market

logger = logging.getLogger(__name__)


async def fetch_market_liquidity(
    session: AsyncSession,
    gamma: GammaClient,
    clob: CLOBClient,
    condition_ids: list[str],
    concurrency: int = 5,
) -> int:
    """Fetch liquidity data for markets and cache in DB. Returns count updated."""
    sem = asyncio.Semaphore(concurrency)
    updated = 0

    async def _fetch_one(cid: str) -> int:
        async with sem:
            try:
                return await _upsert_market(session, gamma, clob, cid)
            except Exception as e:
                logger.debug("Failed to fetch market %s: %s", cid[:10], e)
                return 0

    tasks = [_fetch_one(cid) for cid in condition_ids]
    results = await asyncio.gather(*tasks)
    updated = sum(results)
    logger.info("Updated liquidity for %d / %d markets", updated, len(condition_ids))
    return updated


async def _upsert_market(
    session: AsyncSession,
    gamma: GammaClient,
    clob: CLOBClient,
    condition_id: str,
) -> int:
    """Fetch and upsert a single market's liquidity data."""
    # Check if recently cached (< 6 hours)
    result = await session.execute(
        select(Market).where(Market.condition_id == condition_id)
    )
    existing = result.scalar_one_or_none()
    if existing and existing.fetched_at:
        age = (datetime.utcnow() - existing.fetched_at).total_seconds()
        if age < 6 * 3600:
            return 0  # Fresh enough

    # Fetch from Gamma API
    market_data = await gamma.get_market_by_condition(condition_id)
    if not market_data:
        return 0

    liquidity = _safe_float(market_data.get("liquidity"))
    volume = _safe_float(market_data.get("volume"))

    # Try to get spread from CLOB
    spread_pct = None
    tokens = market_data.get("clobTokenIds")
    if tokens and isinstance(tokens, list) and len(tokens) > 0:
        # Use first token to check spread
        spread_pct = await clob.get_spread_pct(tokens[0])

    if existing:
        existing.liquidity = liquidity
        existing.volume = volume
        existing.spread_pct = spread_pct
        existing.active = market_data.get("active", True)
        existing.closed = market_data.get("closed", False)
        existing.fetched_at = datetime.utcnow()
    else:
        market = Market(
            condition_id=condition_id,
            slug=market_data.get("slug"),
            title=market_data.get("question") or market_data.get("title"),
            category=market_data.get("groupItemTitle"),
            active=market_data.get("active", True),
            closed=market_data.get("closed", False),
            volume=volume,
            liquidity=liquidity,
            spread_pct=spread_pct,
            end_date=market_data.get("endDate"),
            outcomes=market_data.get("outcomes"),
            outcome_prices=market_data.get("outcomePrices"),
            fetched_at=datetime.utcnow(),
        )
        session.add(market)

    return 1


def _safe_float(val) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
