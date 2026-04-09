from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from polymarket.db import Base


class TraderScore(Base):
    __tablename__ = "trader_scores"

    id: Mapped[int] = mapped_column(primary_key=True)
    trader_id: Mapped[int] = mapped_column(ForeignKey("traders.id"), index=True)
    scored_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    # Volume / activity
    trade_count: Mapped[int] = mapped_column(Integer, default=0)
    active_days: Mapped[int] = mapped_column(Integer, default=0)
    time_span_days: Mapped[int] = mapped_column(Integer, default=0)
    total_volume: Mapped[float] = mapped_column(Float, default=0.0)
    unique_markets: Mapped[int] = mapped_column(Integer, default=0)
    days_since_last_trade: Mapped[int] = mapped_column(Integer, default=9999)

    # Performance
    net_profit: Mapped[float] = mapped_column(Float, default=0.0)
    roi: Mapped[float] = mapped_column(Float, default=0.0)
    win_rate: Mapped[float] = mapped_column(Float, default=0.0)
    profit_factor: Mapped[float] = mapped_column(Float, default=0.0)
    sharpe_ratio: Mapped[float] = mapped_column(Float, default=0.0)
    max_drawdown: Mapped[float] = mapped_column(Float, default=0.0)
    recovery_factor: Mapped[float] = mapped_column(Float, default=0.0)
    calmar_ratio: Mapped[float] = mapped_column(Float, default=0.0)

    # Quality
    market_diversity: Mapped[float] = mapped_column(Float, default=0.0)
    consistency_score: Mapped[float] = mapped_column(Float, default=0.0)
    position_sizing_score: Mapped[float] = mapped_column(Float, default=0.0)
    liquidity_score: Mapped[float] = mapped_column(Float, default=0.0)

    # Composite
    composite_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, index=True)
    tier: Mapped[str] = mapped_column(String(1), default="F")
    red_flags: Mapped[list] = mapped_column(JSON, default=list)
    passes_checklist: Mapped[bool] = mapped_column(Boolean, default=False)

    trader: Mapped["Trader"] = relationship(back_populates="scores")  # noqa: F821
