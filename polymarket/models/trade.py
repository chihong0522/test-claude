from __future__ import annotations

from sqlalchemy import Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from polymarket.db import Base


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    trader_id: Mapped[int] = mapped_column(ForeignKey("traders.id"), index=True)
    proxy_wallet: Mapped[str] = mapped_column(String(42), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(4), nullable=False)  # BUY / SELL
    asset: Mapped[str] = mapped_column(Text, nullable=False)
    condition_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    size: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    usdc_size: Mapped[float | None] = mapped_column(Float)
    timestamp: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    transaction_hash: Mapped[str | None] = mapped_column(String(66), unique=True)
    title: Mapped[str | None] = mapped_column(Text)
    slug: Mapped[str | None] = mapped_column(Text)
    event_slug: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(String(50))
    outcome_index: Mapped[int | None] = mapped_column(Integer)

    trader: Mapped["Trader"] = relationship(back_populates="trades")  # noqa: F821
