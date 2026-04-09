from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from polymarket.db import Base


class DailyReport(Base):
    __tablename__ = "daily_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    report_date: Mapped[date] = mapped_column(Date, unique=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    traders_scanned: Mapped[int] = mapped_column(Integer, default=0)
    traders_passing: Mapped[int] = mapped_column(Integer, default=0)
    top_10: Mapped[list] = mapped_column(JSON, default=list)
    summary: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)
