"""Fetch and store trade history for traders."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polymarket.clients.data_api import DataAPIClient
from polymarket.models.trade import Trade
from polymarket.models.trader import Trader

logger = logging.getLogger(__name__)


async def fetch_and_store_trades(
    session: AsyncSession,
    client: DataAPIClient,
    trader: Trader,
) -> int:
    """Fetch all trades for a trader and store new ones. Returns count of new trades."""
    # Find the most recent stored trade timestamp for incremental fetch
    result = await session.execute(
        select(Trade.timestamp)
        .where(Trade.trader_id == trader.id)
        .order_by(Trade.timestamp.desc())
        .limit(1)
    )
    latest_ts = result.scalar_one_or_none()

    all_trades = await client.get_all_trades(trader.proxy_wallet)
    new_count = 0

    for t in all_trades:
        tx_hash = t.get("transactionHash")
        ts = int(t.get("timestamp", 0))

        # Skip trades we already have (incremental update)
        if latest_ts and ts <= latest_ts:
            continue

        # Dedup on transaction hash
        if tx_hash:
            exists = await session.execute(
                select(Trade.id).where(Trade.transaction_hash == tx_hash)
            )
            if exists.scalar_one_or_none() is not None:
                continue

        trade = Trade(
            trader_id=trader.id,
            proxy_wallet=trader.proxy_wallet,
            side=t.get("side", "UNKNOWN"),
            asset=t.get("asset", ""),
            condition_id=t.get("conditionId", ""),
            size=float(t.get("size", 0)),
            price=float(t.get("price", 0)),
            usdc_size=float(t.get("usdcSize", 0)) if t.get("usdcSize") else None,
            timestamp=ts,
            transaction_hash=tx_hash,
            title=t.get("title"),
            slug=t.get("slug"),
            event_slug=t.get("eventSlug"),
            outcome=t.get("outcome"),
            outcome_index=t.get("outcomeIndex"),
        )
        session.add(trade)
        new_count += 1

    if new_count > 0:
        trader.last_updated_at = datetime.utcnow()
        await session.flush()

    logger.info("Stored %d new trades for %s", new_count, trader.proxy_wallet[:10])
    return new_count


async def fetch_trades_batch(
    session: AsyncSession,
    client: DataAPIClient,
    traders: list[Trader],
    concurrency: int = 5,
) -> int:
    """Fetch trades for multiple traders with concurrency limit."""
    sem = asyncio.Semaphore(concurrency)
    total = 0

    async def _fetch_one(trader: Trader) -> int:
        async with sem:
            try:
                return await fetch_and_store_trades(session, client, trader)
            except Exception as e:
                logger.error("Failed to fetch trades for %s: %s", trader.proxy_wallet[:10], e)
                return 0

    tasks = [_fetch_one(t) for t in traders]
    results = await asyncio.gather(*tasks)
    total = sum(results)
    logger.info("Batch fetch complete: %d new trades across %d traders", total, len(traders))
    return total
