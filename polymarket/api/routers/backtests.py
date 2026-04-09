"""Backtest API endpoints."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polymarket.api.schemas import BacktestDetail, BacktestRequest, BacktestSummary
from polymarket.backtester.simulator import BacktestConfig, run_backtest
from polymarket.clients.data_api import DataAPIClient
from polymarket.db import async_session, get_session
from polymarket.models.backtest import BacktestRun
from polymarket.models.trader import Trader

router = APIRouter(prefix="/api/backtests", tags=["backtests"])


async def _run_backtest_task(backtest_id: int, wallet: str, config: BacktestConfig):
    """Background task to run a backtest."""
    client = DataAPIClient()
    try:
        trades = await client.get_all_trades(wallet)
        result = run_backtest(trades, config)

        async with async_session() as session:
            bt_result = await session.execute(
                select(BacktestRun).where(BacktestRun.id == backtest_id)
            )
            bt = bt_result.scalar_one_or_none()
            if bt:
                bt.total_trades_copied = result.total_trades_copied
                bt.profitable_trades = result.profitable_trades
                bt.losing_trades = result.losing_trades
                bt.final_capital = result.final_capital
                bt.total_return = result.total_return
                bt.max_drawdown = result.max_drawdown
                bt.sharpe_ratio = result.sharpe_ratio
                bt.win_rate = result.win_rate
                bt.avg_trade_pnl = result.avg_trade_pnl
                bt.best_trade_pnl = result.best_trade_pnl
                bt.worst_trade_pnl = result.worst_trade_pnl
                bt.equity_curve = result.equity_curve
                await session.commit()
    finally:
        await client.close()


@router.post("", response_model=BacktestSummary, status_code=201)
async def create_backtest(
    req: BacktestRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
):
    """Submit a new backtest. Runs in background."""
    # Find or create trader
    result = await session.execute(
        select(Trader).where(Trader.proxy_wallet == req.wallet)
    )
    trader = result.scalar_one_or_none()
    if not trader:
        trader = Trader(proxy_wallet=req.wallet, first_seen_at=datetime.utcnow())
        session.add(trader)
        await session.flush()

    config = BacktestConfig(
        initial_capital=req.initial_capital,
        position_pct=req.position_pct,
        slippage_bps=req.slippage_bps,
        delay_seconds=req.delay_seconds,
        start_date=req.start_date,
        end_date=req.end_date,
    )

    bt = BacktestRun(
        trader_id=trader.id,
        created_at=datetime.utcnow(),
        config={
            "initial_capital": config.initial_capital,
            "position_pct": config.position_pct,
            "slippage_bps": config.slippage_bps,
            "delay_seconds": config.delay_seconds,
        },
        initial_capital=config.initial_capital,
        final_capital=0.0,
    )
    session.add(bt)
    await session.commit()
    await session.refresh(bt)

    background_tasks.add_task(_run_backtest_task, bt.id, req.wallet, config)

    return BacktestSummary(
        id=bt.id,
        trader_id=bt.trader_id,
        created_at=bt.created_at,
        initial_capital=bt.initial_capital,
        final_capital=bt.final_capital,
        total_return=bt.total_return,
        max_drawdown=bt.max_drawdown,
        sharpe_ratio=bt.sharpe_ratio,
        win_rate=bt.win_rate,
        total_trades_copied=bt.total_trades_copied,
        profitable_trades=bt.profitable_trades,
        losing_trades=bt.losing_trades,
    )


@router.get("/{backtest_id}", response_model=BacktestDetail)
async def get_backtest(backtest_id: int, session: AsyncSession = Depends(get_session)):
    """Get backtest results."""
    result = await session.execute(
        select(BacktestRun).where(BacktestRun.id == backtest_id)
    )
    bt = result.scalar_one_or_none()
    if not bt:
        raise HTTPException(404, "Backtest not found")

    return BacktestDetail(
        id=bt.id,
        trader_id=bt.trader_id,
        created_at=bt.created_at,
        initial_capital=bt.initial_capital,
        final_capital=bt.final_capital,
        total_return=bt.total_return,
        max_drawdown=bt.max_drawdown,
        sharpe_ratio=bt.sharpe_ratio,
        win_rate=bt.win_rate,
        total_trades_copied=bt.total_trades_copied,
        profitable_trades=bt.profitable_trades,
        losing_trades=bt.losing_trades,
        avg_trade_pnl=bt.avg_trade_pnl,
        best_trade_pnl=bt.best_trade_pnl,
        worst_trade_pnl=bt.worst_trade_pnl,
        config=bt.config or {},
        equity_curve=bt.equity_curve or [],
    )


@router.get("", response_model=list[BacktestSummary])
async def list_backtests(
    wallet: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
):
    """List backtests, optionally filtered by wallet."""
    query = select(BacktestRun).order_by(BacktestRun.created_at.desc()).limit(limit)
    if wallet:
        query = query.join(Trader).where(Trader.proxy_wallet == wallet)

    result = await session.execute(query)
    backtests = result.scalars().all()

    return [
        BacktestSummary(
            id=bt.id,
            trader_id=bt.trader_id,
            created_at=bt.created_at,
            initial_capital=bt.initial_capital,
            final_capital=bt.final_capital,
            total_return=bt.total_return,
            max_drawdown=bt.max_drawdown,
            sharpe_ratio=bt.sharpe_ratio,
            win_rate=bt.win_rate,
            total_trades_copied=bt.total_trades_copied,
            profitable_trades=bt.profitable_trades,
            losing_trades=bt.losing_trades,
        )
        for bt in backtests
    ]
