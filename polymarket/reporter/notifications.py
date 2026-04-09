"""Discord notifications for daily reports.

Reuses the embed pattern from limitless/exchange_monitor.py.
"""

from __future__ import annotations

import datetime
import logging

import httpx

from polymarket.config import settings
from polymarket.models.daily_report import DailyReport

logger = logging.getLogger(__name__)

# Tier colors
TIER_COLORS = {
    "S": 0x00C853,  # Green
    "A": 0x2196F3,  # Blue
    "B": 0xFFC107,  # Amber
    "C": 0xFF9800,  # Orange
    "F": 0xF44336,  # Red
}


def _truncate_wallet(wallet: str) -> str:
    if len(wallet) > 12:
        return wallet[:6] + "..." + wallet[-4:]
    return wallet


async def send_daily_report_notification(report: DailyReport) -> None:
    """Send daily top-10 report to Discord via webhook."""
    if not settings.discord_webhook_url:
        logger.info("Discord webhook not configured, skipping notification")
        return

    top_10 = report.top_10 or []
    if not top_10:
        logger.info("No traders in top-10, skipping notification")
        return

    # Build embed fields for each trader
    fields = []
    for trader in top_10[:10]:
        tier = trader.get("tier", "?")
        score = trader.get("composite_score", 0)
        name = trader.get("name", "Unknown")
        wallet = trader.get("proxy_wallet", "")
        roi = trader.get("roi", 0)
        win_rate = trader.get("win_rate", 0)
        liq = trader.get("liquidity_score", 0)

        tier_emoji = {"S": "S", "A": "A", "B": "B", "C": "C", "F": "F"}.get(tier, "?")

        fields.append({
            "name": f"#{trader.get('rank', '?')} [{tier_emoji}] {name}",
            "value": (
                f"Score: **{score}** | ROI: {roi}% | WR: {win_rate}%\n"
                f"Liq: {liq}% | `{wallet}`"
            ),
            "inline": False,
        })

    # Primary color based on best tier
    best_tier = top_10[0].get("tier", "B") if top_10 else "B"
    color = TIER_COLORS.get(best_tier, 0x9E9E9E)

    embed = {
        "title": f"Daily Top Traders Report — {report.report_date}",
        "color": color,
        "description": (
            f"**{report.traders_scanned}** traders scored | "
            f"**{report.traders_passing}** pass checklist"
        ),
        "fields": fields,
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "footer": {
            "text": "Wallet addresses above are ready to paste into Polycop",
        },
    }

    payload = {"embeds": [embed]}

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                settings.discord_webhook_url,
                json=payload,
                timeout=15,
            )
            resp.raise_for_status()
            logger.info("Discord notification sent for %s", report.report_date)
    except httpx.HTTPError as e:
        logger.error("Discord notification failed: %s", e)
