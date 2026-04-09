"""Fetch and store positions (open + closed) for traders."""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from polymarket.clients.data_api import DataAPIClient
from polymarket.models.position import Position
from polymarket.models.trader import Trader

logger = logging.getLogger(__name__)


async def fetch_and_store_positions(
    session: AsyncSession,
    client: DataAPIClient,
    trader: Trader,
) -> int:
    """Fetch open + closed positions and upsert. Returns total count."""
    count = 0

    # Open positions
    open_positions = await client.get_positions(trader.proxy_wallet)
    for p in open_positions:
        count += await _upsert_position(session, trader, p, is_closed=False)

    # Closed positions
    closed_positions = await client.get_closed_positions(trader.proxy_wallet)
    for p in closed_positions:
        count += await _upsert_position(session, trader, p, is_closed=True)

    logger.info(
        "Stored %d positions for %s (%d open, %d closed)",
        count, trader.proxy_wallet[:10], len(open_positions), len(closed_positions),
    )
    return count


async def _upsert_position(
    session: AsyncSession,
    trader: Trader,
    data: dict,
    is_closed: bool,
) -> int:
    """Upsert a single position. Returns 1 if new, 0 if updated."""
    condition_id = data.get("conditionId", "")
    asset = data.get("asset", "")
    outcome_index = data.get("outcomeIndex")

    # Check for existing position with same condition + asset
    result = await session.execute(
        select(Position).where(
            and_(
                Position.trader_id == trader.id,
                Position.condition_id == condition_id,
                Position.asset == asset,
                Position.is_closed == is_closed,
            )
        )
    )
    existing = result.scalar_one_or_none()

    def _float(val) -> float | None:
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    if existing:
        # Update existing position
        existing.size = _float(data.get("size"))
        existing.avg_price = _float(data.get("avgPrice"))
        existing.initial_value = _float(data.get("initialValue"))
        existing.current_value = _float(data.get("currentValue"))
        existing.cash_pnl = _float(data.get("cashPnl"))
        existing.percent_pnl = _float(data.get("percentPnl"))
        existing.total_bought = _float(data.get("totalBought"))
        existing.realized_pnl = _float(data.get("realizedPnl"))
        existing.cur_price = _float(data.get("curPrice"))
        existing.fetched_at = datetime.utcnow()
        return 0

    position = Position(
        trader_id=trader.id,
        proxy_wallet=trader.proxy_wallet,
        asset=asset,
        condition_id=condition_id,
        size=_float(data.get("size")),
        avg_price=_float(data.get("avgPrice")),
        initial_value=_float(data.get("initialValue")),
        current_value=_float(data.get("currentValue")),
        cash_pnl=_float(data.get("cashPnl")),
        percent_pnl=_float(data.get("percentPnl")),
        total_bought=_float(data.get("totalBought")),
        realized_pnl=_float(data.get("realizedPnl")),
        cur_price=_float(data.get("curPrice")),
        title=data.get("title"),
        slug=data.get("slug"),
        outcome=data.get("outcome"),
        outcome_index=outcome_index,
        end_date=data.get("endDate"),
        is_closed=is_closed,
        fetched_at=datetime.utcnow(),
    )
    session.add(position)
    return 1
