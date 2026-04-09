"""Generate structured daily top-10 reports."""

from __future__ import annotations

import logging
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from polymarket.analyzer.rankings import get_top_traders
from polymarket.models.daily_report import DailyReport

logger = logging.getLogger(__name__)


async def generate_daily_report(
    session: AsyncSession,
    traders_scored: int,
) -> DailyReport:
    """Generate and store today's daily report with top-10 traders."""
    today = date.today()

    # Get top 10 passing traders
    top_10 = await get_top_traders(session, limit=10, passing_only=True)

    # Build summary
    tiers = [t["tier"] for t in top_10]
    s_count = tiers.count("S")
    a_count = tiers.count("A")

    summary_lines = [
        f"Daily Report for {today}",
        f"Traders scored: {traders_scored}",
        f"Traders passing checklist: {len(top_10)}",
        f"S-tier: {s_count}, A-tier: {a_count}",
    ]
    if top_10:
        best = top_10[0]
        summary_lines.append(
            f"#1: {best['name']} — Score: {best['composite_score']}, "
            f"ROI: {best['roi']}%, Win Rate: {best['win_rate']}%"
        )

    report = DailyReport(
        report_date=today,
        created_at=datetime.utcnow(),
        traders_scanned=traders_scored,
        traders_passing=len(top_10),
        top_10=top_10,
        summary="\n".join(summary_lines),
    )
    session.add(report)

    logger.info("Generated daily report: %d passing traders", len(top_10))
    return report
