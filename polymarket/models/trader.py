from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from polymarket.db import Base


class Trader(Base):
    __tablename__ = "traders"

    id: Mapped[int] = mapped_column(primary_key=True)
    proxy_wallet: Mapped[str] = mapped_column(String(42), unique=True, nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(255))
    pseudonym: Mapped[str | None] = mapped_column(String(255))
    bio: Mapped[str | None] = mapped_column(Text)
    profile_image: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    trades: Mapped[list["Trade"]] = relationship(back_populates="trader")  # noqa: F821
    positions: Mapped[list["Position"]] = relationship(back_populates="trader")  # noqa: F821
    scores: Mapped[list["TraderScore"]] = relationship(back_populates="trader")  # noqa: F821
    backtests: Mapped[list["BacktestRun"]] = relationship(back_populates="trader")  # noqa: F821
