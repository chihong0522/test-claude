"""Rank and compare scored traders."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polymarket.models.score import TraderScore
from polymarket.models.trader import Trader


async def get_top_traders(
    session: AsyncSession,
    limit: int = 10,
    tier: str | None = None,
    min_score: float = 0.0,
    passing_only: bool = True,
) -> list[dict]:
    """Get top-ranked traders by composite score."""
    query = (
        select(TraderScore, Trader)
        .join(Trader, TraderScore.trader_id == Trader.id)
        .where(TraderScore.composite_score >= min_score)
    )
    if passing_only:
        query = query.where(TraderScore.passes_checklist.is_(True))
    if tier:
        query = query.where(TraderScore.tier == tier)

    # Get the most recent score for each trader
    query = query.order_by(TraderScore.composite_score.desc()).limit(limit)

    result = await session.execute(query)
    rows = result.all()

    return [
        {
            "rank": i + 1,
            "trader_id": score.trader_id,
            "proxy_wallet": trader.proxy_wallet,
            "name": trader.name or trader.pseudonym or trader.proxy_wallet[:10],
            "composite_score": round(score.composite_score, 1),
            "tier": score.tier,
            "roi": round(score.roi * 100, 1),
            "win_rate": round(score.win_rate * 100, 1),
            "profit_factor": round(score.profit_factor, 2),
            "sharpe_ratio": round(score.sharpe_ratio, 2),
            "trade_count": score.trade_count,
            "liquidity_score": round(score.liquidity_score * 100, 1),
            "red_flags": score.red_flags,
        }
        for i, (score, trader) in enumerate(rows)
    ]
