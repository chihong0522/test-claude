from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from polymarket.db import Base


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(primary_key=True)
    trader_id: Mapped[int] = mapped_column(ForeignKey("traders.id"), index=True)
    proxy_wallet: Mapped[str] = mapped_column(String(42), nullable=False)
    asset: Mapped[str] = mapped_column(Text, nullable=False)
    condition_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    size: Mapped[float | None] = mapped_column(Float)
    avg_price: Mapped[float | None] = mapped_column(Float)
    initial_value: Mapped[float | None] = mapped_column(Float)
    current_value: Mapped[float | None] = mapped_column(Float)
    cash_pnl: Mapped[float | None] = mapped_column(Float)
    percent_pnl: Mapped[float | None] = mapped_column(Float)
    total_bought: Mapped[float | None] = mapped_column(Float)
    realized_pnl: Mapped[float | None] = mapped_column(Float)
    cur_price: Mapped[float | None] = mapped_column(Float)
    title: Mapped[str | None] = mapped_column(Text)
    slug: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(String(50))
    outcome_index: Mapped[int | None] = mapped_column(Integer)
    end_date: Mapped[str | None] = mapped_column(Text)
    is_closed: Mapped[bool] = mapped_column(Boolean, default=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    trader: Mapped["Trader"] = relationship(back_populates="positions")  # noqa: F821
