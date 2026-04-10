"""Trader API endpoints."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from polymarket.api.schemas import PositionRecord, TradeRecord, TraderDetail, TraderSummary
from polymarket.db import get_session
from polymarket.models.position import Position
from polymarket.models.score import TraderScore
from polymarket.models.trade import Trade
from polymarket.models.trader import Trader

router = APIRouter(prefix="/api/traders", tags=["traders"])


@router.get("", response_model=list[TraderSummary])
async def list_traders(
    sort: str = Query("composite_score", enum=["composite_score", "roi", "win_rate", "trade_count"]),
    order: str = Query("desc", enum=["asc", "desc"]),
    tier: str | None = None,
    min_score: float = 0.0,
    passing_only: bool = True,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    """List scored traders with filtering and sorting."""
    # Subquery for latest score per trader
    latest_score = (
        select(
            TraderScore.trader_id,
            func.max(TraderScore.scored_at).label("latest"),
        )
        .group_by(TraderScore.trader_id)
        .subquery()
    )

    query = (
        select(TraderScore, Trader)
        .join(Trader, TraderScore.trader_id == Trader.id)
        .join(
            latest_score,
            (TraderScore.trader_id == latest_score.c.trader_id)
            & (TraderScore.scored_at == latest_score.c.latest),
        )
        .where(TraderScore.composite_score >= min_score)
    )

    if passing_only:
        query = query.where(TraderScore.passes_checklist.is_(True))
    if tier:
        query = query.where(TraderScore.tier == tier)

    sort_col = getattr(TraderScore, sort, TraderScore.composite_score)
    query = query.order_by(sort_col.desc() if order == "desc" else sort_col.asc())
    query = query.offset(offset).limit(limit)

    result = await session.execute(query)
    rows = result.all()

    return [
        TraderSummary(
            rank=offset + i + 1,
            trader_id=score.trader_id,
            proxy_wallet=trader.proxy_wallet,
            name=trader.name or trader.pseudonym,
            composite_score=round(score.composite_score, 1),
            tier=score.tier,
            roi=round(score.roi * 100, 1),
            win_rate=round(score.win_rate * 100, 1),
            profit_factor=round(score.profit_factor, 2),
            sharpe_ratio=round(score.sharpe_ratio, 2),
            trade_count=score.trade_count,
            liquidity_score=round(score.liquidity_score * 100, 1),
            red_flags=score.red_flags or [],
        )
        for i, (score, trader) in enumerate(rows)
    ]


@router.get("/{wallet}", response_model=TraderDetail)
async def get_trader(wallet: str, session: AsyncSession = Depends(get_session)):
    """Get detailed trader profile with latest score."""
    result = await session.execute(select(Trader).where(Trader.proxy_wallet == wallet))
    trader = result.scalar_one_or_none()
    if not trader:
        raise HTTPException(404, "Trader not found")

    score_result = await session.execute(
        select(TraderScore)
        .where(TraderScore.trader_id == trader.id)
        .order_by(TraderScore.scored_at.desc())
        .limit(1)
    )
    score = score_result.scalar_one_or_none()
    if not score:
        raise HTTPException(404, "No score data for this trader")

    return TraderDetail(
        trader_id=trader.id,
        proxy_wallet=trader.proxy_wallet,
        name=trader.name or trader.pseudonym,
        pseudonym=trader.pseudonym,
        bio=trader.bio,
        profile_image=trader.profile_image,
        composite_score=round(score.composite_score, 1),
        tier=score.tier,
        roi=round(score.roi * 100, 1),
        win_rate=round(score.win_rate * 100, 1),
        profit_factor=round(score.profit_factor, 2),
        sharpe_ratio=round(score.sharpe_ratio, 2),
        trade_count=score.trade_count,
        liquidity_score=round(score.liquidity_score * 100, 1),
        red_flags=score.red_flags or [],
        net_profit=round(score.net_profit, 2),
        max_drawdown=round(score.max_drawdown * 100, 2),
        recovery_factor=round(score.recovery_factor, 2),
        consistency_score=round(score.consistency_score, 2),
        market_diversity=round(score.market_diversity, 2),
        position_sizing_score=round(score.position_sizing_score, 2),
        unique_markets=score.unique_markets,
        active_days=score.active_days,
        time_span_days=score.time_span_days,
        total_volume=round(score.total_volume, 2),
        passes_checklist=score.passes_checklist,
        last_updated_at=trader.last_updated_at,
    )


@router.get("/{wallet}/trades", response_model=list[TradeRecord])
async def get_trader_trades(
    wallet: str,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    """Get trade history for a trader."""
    result = await session.execute(select(Trader).where(Trader.proxy_wallet == wallet))
    trader = result.scalar_one_or_none()
    if not trader:
        raise HTTPException(404, "Trader not found")

    trades_result = await session.execute(
        select(Trade)
        .where(Trade.trader_id == trader.id)
        .order_by(Trade.timestamp.desc())
        .offset(offset)
        .limit(limit)
    )
    trades = trades_result.scalars().all()

    return [
        TradeRecord(
            side=t.side,
            size=t.size,
            price=t.price,
            timestamp=t.timestamp,
            title=t.title,
            outcome=t.outcome,
            condition_id=t.condition_id,
            transaction_hash=t.transaction_hash,
        )
        for t in trades
    ]


@router.get("/_rotation/recent-changes")
async def get_rotation_changes(
    hours: float = Query(6.0, ge=0.01, le=720),
    session: AsyncSession = Depends(get_session),
):
    """Compare latest scores vs scores from N hours ago.

    Returns rising, declining, new, and dropped traders — helps decide
    who to start/stop copying.
    """
    from datetime import timedelta

    now = datetime.utcnow()
    cutoff = now - timedelta(hours=hours)

    # Latest score per trader
    latest_sub = (
        select(
            TraderScore.trader_id,
            func.max(TraderScore.scored_at).label("latest"),
        )
        .group_by(TraderScore.trader_id)
        .subquery()
    )
    latest_result = await session.execute(
        select(TraderScore, Trader)
        .join(Trader, TraderScore.trader_id == Trader.id)
        .join(
            latest_sub,
            (TraderScore.trader_id == latest_sub.c.trader_id)
            & (TraderScore.scored_at == latest_sub.c.latest),
        )
    )
    latest_rows = latest_result.all()

    # Previous score per trader (most recent before cutoff)
    prev_sub = (
        select(
            TraderScore.trader_id,
            func.max(TraderScore.scored_at).label("prev"),
        )
        .where(TraderScore.scored_at < cutoff)
        .group_by(TraderScore.trader_id)
        .subquery()
    )
    prev_result = await session.execute(
        select(TraderScore)
        .join(
            prev_sub,
            (TraderScore.trader_id == prev_sub.c.trader_id)
            & (TraderScore.scored_at == prev_sub.c.prev),
        )
    )
    prev_by_trader = {s.trader_id: s for s in prev_result.scalars().all()}

    rising = []
    declining = []
    new_traders = []
    stable = []

    for latest_score, trader in latest_rows:
        prev = prev_by_trader.get(latest_score.trader_id)
        row = {
            "wallet": trader.proxy_wallet,
            "name": trader.name or trader.proxy_wallet[:10],
            "current_score": round(latest_score.composite_score, 1),
            "current_tier": latest_score.tier,
            "current_rank": None,
            "passes_checklist": latest_score.passes_checklist,
        }

        if prev is None:
            row["delta"] = None
            row["prev_score"] = None
            row["prev_tier"] = None
            new_traders.append(row)
            continue

        delta = round(latest_score.composite_score - prev.composite_score, 1)
        row["delta"] = delta
        row["prev_score"] = round(prev.composite_score, 1)
        row["prev_tier"] = prev.tier

        if delta > 3:
            rising.append(row)
        elif delta < -3:
            declining.append(row)
        else:
            stable.append(row)

    rising.sort(key=lambda r: r["delta"], reverse=True)
    declining.sort(key=lambda r: r["delta"])
    new_traders.sort(key=lambda r: r["current_score"], reverse=True)
    stable.sort(key=lambda r: r["current_score"], reverse=True)

    return {
        "compared_hours": hours,
        "cutoff": cutoff.isoformat(),
        "now": now.isoformat(),
        "counts": {
            "rising": len(rising),
            "declining": len(declining),
            "new": len(new_traders),
            "stable": len(stable),
        },
        "rising": rising[:20],
        "declining": declining[:20],
        "new_traders": new_traders[:20],
        "stable_top": stable[:10],
    }


@router.get("/{wallet}/score-history")
async def get_trader_score_history(
    wallet: str,
    limit: int = Query(30, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    """Return score history (most recent first) for tracking trader trends."""
    result = await session.execute(select(Trader).where(Trader.proxy_wallet == wallet))
    trader = result.scalar_one_or_none()
    if not trader:
        raise HTTPException(404, "Trader not found")

    history_result = await session.execute(
        select(TraderScore)
        .where(TraderScore.trader_id == trader.id)
        .order_by(TraderScore.scored_at.desc())
        .limit(limit)
    )
    history = history_result.scalars().all()

    return [
        {
            "scored_at": s.scored_at.isoformat(),
            "composite_score": round(s.composite_score, 1),
            "tier": s.tier,
            "roi": round(s.roi * 100, 2),
            "win_rate": round(s.win_rate * 100, 1),
            "trade_count": s.trade_count,
            "net_profit": round(s.net_profit, 2),
            "passes_checklist": s.passes_checklist,
        }
        for s in history
    ]


@router.get("/{wallet}/positions", response_model=list[PositionRecord])
async def get_trader_positions(
    wallet: str,
    session: AsyncSession = Depends(get_session),
):
    """Get current open positions for a trader."""
    result = await session.execute(select(Trader).where(Trader.proxy_wallet == wallet))
    trader = result.scalar_one_or_none()
    if not trader:
        raise HTTPException(404, "Trader not found")

    pos_result = await session.execute(
        select(Position)
        .where(Position.trader_id == trader.id, Position.is_closed.is_(False))
    )
    positions = pos_result.scalars().all()

    return [
        PositionRecord(
            condition_id=p.condition_id,
            size=p.size,
            avg_price=p.avg_price,
            current_value=p.current_value,
            cash_pnl=p.cash_pnl,
            realized_pnl=p.realized_pnl,
            title=p.title,
            outcome=p.outcome,
            is_closed=p.is_closed,
        )
        for p in positions
    ]
