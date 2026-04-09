"""Daily reports API endpoints."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polymarket.api.schemas import DailyReportDetail, DailyReportSummary
from polymarket.db import get_session
from polymarket.models.daily_report import DailyReport

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.get("", response_model=list[DailyReportSummary])
async def list_reports(
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    """List daily reports, most recent first."""
    result = await session.execute(
        select(DailyReport)
        .order_by(DailyReport.report_date.desc())
        .offset(offset)
        .limit(limit)
    )
    reports = result.scalars().all()

    return [
        DailyReportSummary(
            id=r.id,
            report_date=r.report_date,
            traders_scanned=r.traders_scanned,
            traders_passing=r.traders_passing,
            summary=r.summary,
        )
        for r in reports
    ]


@router.get("/latest", response_model=DailyReportDetail)
async def get_latest_report(session: AsyncSession = Depends(get_session)):
    """Get the most recent daily report."""
    result = await session.execute(
        select(DailyReport).order_by(DailyReport.report_date.desc()).limit(1)
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(404, "No reports available yet")

    return DailyReportDetail(
        id=report.id,
        report_date=report.report_date,
        traders_scanned=report.traders_scanned,
        traders_passing=report.traders_passing,
        summary=report.summary,
        top_10=report.top_10 or [],
        created_at=report.created_at,
    )


@router.get("/{report_date}", response_model=DailyReportDetail)
async def get_report_by_date(
    report_date: date,
    session: AsyncSession = Depends(get_session),
):
    """Get report for a specific date."""
    result = await session.execute(
        select(DailyReport).where(DailyReport.report_date == report_date)
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(404, f"No report for {report_date}")

    return DailyReportDetail(
        id=report.id,
        report_date=report.report_date,
        traders_scanned=report.traders_scanned,
        traders_passing=report.traders_passing,
        summary=report.summary,
        top_10=report.top_10 or [],
        created_at=report.created_at,
    )
