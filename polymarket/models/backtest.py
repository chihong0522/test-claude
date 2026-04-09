from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from polymarket.db import Base


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    trader_id: Mapped[int] = mapped_column(ForeignKey("traders.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    config: Mapped[dict] = mapped_column(JSON, nullable=False)

    # Results
    total_trades_copied: Mapped[int] = mapped_column(Integer, default=0)
    profitable_trades: Mapped[int] = mapped_column(Integer, default=0)
    losing_trades: Mapped[int] = mapped_column(Integer, default=0)
    initial_capital: Mapped[float] = mapped_column(Float, nullable=False)
    final_capital: Mapped[float] = mapped_column(Float, default=0.0)
    total_return: Mapped[float] = mapped_column(Float, default=0.0)
    max_drawdown: Mapped[float] = mapped_column(Float, default=0.0)
    sharpe_ratio: Mapped[float] = mapped_column(Float, default=0.0)
    win_rate: Mapped[float] = mapped_column(Float, default=0.0)
    avg_trade_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    best_trade_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    worst_trade_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    equity_curve: Mapped[list] = mapped_column(JSON, default=list)

    trader: Mapped["Trader"] = relationship(back_populates="backtests")  # noqa: F821
