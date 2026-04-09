from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Text
from sqlalchemy.orm import Mapped, mapped_column

from polymarket.db import Base


class Market(Base):
    __tablename__ = "markets"

    id: Mapped[int] = mapped_column(primary_key=True)
    condition_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False, index=True)
    slug: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    closed: Mapped[bool] = mapped_column(Boolean, default=False)
    volume: Mapped[float | None] = mapped_column(Float)
    liquidity: Mapped[float | None] = mapped_column(Float)
    spread_pct: Mapped[float | None] = mapped_column(Float)
    end_date: Mapped[str | None] = mapped_column(Text)
    outcomes: Mapped[list | None] = mapped_column(JSON)
    outcome_prices: Mapped[list | None] = mapped_column(JSON)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    @property
    def liquidity_tier(self) -> str:
        if self.liquidity is None:
            return "UNKNOWN"
        if self.liquidity > 200_000:
            return "HIGH"
        if self.liquidity > 50_000:
            return "MEDIUM"
        return "LOW"
