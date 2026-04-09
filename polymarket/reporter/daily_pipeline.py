"""Daily pipeline orchestrator — discovers, collects, enriches, scores, reports."""

from __future__ import annotations

import logging
from datetime import date, datetime

from sqlalchemy import distinct, select
from sqlalchemy.ext.asyncio import AsyncSession

from polymarket.analyzer.scoring import score_trader
from polymarket.clients.clob import CLOBClient
from polymarket.clients.data_api import DataAPIClient
from polymarket.clients.gamma import GammaClient
from polymarket.clients.leaderboard import LeaderboardClient
from polymarket.clients.polymarketscan import PolymarketScanClient
from polymarket.collector.market_liquidity import fetch_market_liquidity
from polymarket.collector.position_fetcher import fetch_and_store_positions
from polymarket.collector.trade_fetcher import fetch_trades_batch
from polymarket.collector.trader_discovery import discover_and_upsert_traders
from polymarket.config import settings
from polymarket.models.market import Market
from polymarket.models.score import TraderScore
from polymarket.models.trade import Trade
from polymarket.models.trader import Trader
from polymarket.reporter.notifications import send_daily_report_notification
from polymarket.reporter.report_generator import generate_daily_report

logger = logging.getLogger(__name__)


async def run_daily_pipeline(session: AsyncSession) -> dict:
    """Execute the full daily research pipeline. Returns summary dict."""
    start_time = datetime.utcnow()
    logger.info("=== Daily Pipeline Started at %s ===", start_time)

    # Initialize clients
    data_client = DataAPIClient()
    gamma_client = GammaClient()
    clob_client = CLOBClient()
    leaderboard = LeaderboardClient(data_client)
    scan_client = PolymarketScanClient()

    try:
        # ── Step 1: Discover ────────────────────────────────────────────
        logger.info("Step 1: Discovering traders...")
        traders = await discover_and_upsert_traders(
            session, leaderboard, scan_client,
            alltime_limit=settings.leaderboard_top_n,
            monthly_limit=200,
        )
        await session.commit()
        logger.info("Discovered %d traders", len(traders))

        # ── Step 2: Collect ─────────────────────────────────────────────
        logger.info("Step 2: Collecting trade data...")
        # Skip traders updated < 6 hours ago
        cutoff = datetime.utcnow().timestamp() - 6 * 3600
        stale_traders = [
            t for t in traders
            if not t.last_updated_at or t.last_updated_at.timestamp() < cutoff
        ]
        logger.info("Fetching trades for %d stale traders (skipping %d fresh)",
                     len(stale_traders), len(traders) - len(stale_traders))

        new_trades = await fetch_trades_batch(
            session, data_client, stale_traders,
            concurrency=settings.max_concurrent_fetches,
        )
        # Fetch positions for stale traders
        for trader in stale_traders[:100]:  # Limit to avoid rate limiting
            try:
                await fetch_and_store_positions(session, data_client, trader)
            except Exception as e:
                logger.warning("Failed positions for %s: %s", trader.proxy_wallet[:10], e)
        await session.commit()
        logger.info("Collected %d new trades", new_trades)

        # ── Step 3: Enrich with liquidity ───────────────────────────────
        logger.info("Step 3: Enriching market liquidity...")
        # Get all unique condition_ids from trades
        cid_result = await session.execute(
            select(distinct(Trade.condition_id))
        )
        all_cids = [row[0] for row in cid_result.all() if row[0]]
        await fetch_market_liquidity(session, gamma_client, clob_client, all_cids[:500])
        await session.commit()
        logger.info("Enriched liquidity for up to %d markets", min(len(all_cids), 500))

        # Build liquidity lookup
        mkt_result = await session.execute(select(Market))
        markets = mkt_result.scalars().all()
        market_liq = {m.condition_id: (m.liquidity or 0) for m in markets}

        # ── Step 4: Score ───────────────────────────────────────────────
        logger.info("Step 4: Scoring traders...")
        scored_count = 0
        for trader in traders:
            # Get trades from DB
            trade_result = await session.execute(
                select(Trade).where(Trade.trader_id == trader.id)
            )
            trader_trades = [_trade_to_dict(t) for t in trade_result.scalars().all()]

            if len(trader_trades) < settings.min_trades_for_scoring:
                continue

            # Get positions from DB
            from polymarket.models.position import Position
            pos_result = await session.execute(
                select(Position).where(Position.trader_id == trader.id, Position.is_closed.is_(False))
            )
            open_pos = [_pos_to_dict(p) for p in pos_result.scalars().all()]

            closed_result = await session.execute(
                select(Position).where(Position.trader_id == trader.id, Position.is_closed.is_(True))
            )
            closed_pos = [_pos_to_dict(p) for p in closed_result.scalars().all()]

            # Score
            score_data = score_trader(trader_trades, open_pos, closed_pos, market_liq)

            # Store score
            score_record = TraderScore(
                trader_id=trader.id,
                scored_at=datetime.utcnow(),
                **{k: v for k, v in score_data.items() if k not in ("tier", "red_flags", "passes_checklist", "composite_score")},
                composite_score=score_data["composite_score"],
                tier=score_data["tier"],
                red_flags=score_data["red_flags"],
                passes_checklist=score_data["passes_checklist"],
            )
            session.add(score_record)
            scored_count += 1

        await session.commit()
        logger.info("Scored %d traders", scored_count)

        # ── Step 5: Generate report ─────────────────────────────────────
        logger.info("Step 5: Generating daily report...")
        report = await generate_daily_report(session, scored_count)
        await session.commit()

        # ── Step 6: Notify ──────────────────────────────────────────────
        if settings.discord_webhook_url:
            logger.info("Step 6: Sending Discord notification...")
            await send_daily_report_notification(report)

        elapsed = (datetime.utcnow() - start_time).total_seconds()
        logger.info("=== Pipeline complete in %.0fs ===", elapsed)

        return {
            "traders_discovered": len(traders),
            "new_trades": new_trades,
            "traders_scored": scored_count,
            "report_date": str(date.today()),
            "elapsed_seconds": round(elapsed),
        }

    finally:
        await data_client.close()
        await gamma_client.close()
        await clob_client.close()
        await scan_client.close()


def _trade_to_dict(trade: Trade) -> dict:
    return {
        "side": trade.side,
        "size": trade.size,
        "price": trade.price,
        "timestamp": trade.timestamp,
        "condition_id": trade.condition_id,
        "event_slug": trade.event_slug,
        "outcome": trade.outcome,
        "outcome_index": trade.outcome_index,
        "title": trade.title,
        "transactionHash": trade.transaction_hash,
    }


def _pos_to_dict(pos) -> dict:
    return {
        "conditionId": pos.condition_id,
        "asset": pos.asset,
        "size": pos.size,
        "avgPrice": pos.avg_price,
        "initialValue": pos.initial_value,
        "currentValue": pos.current_value,
        "cashPnl": pos.cash_pnl,
        "percentPnl": pos.percent_pnl,
        "totalBought": pos.total_bought,
        "realizedPnl": pos.realized_pnl,
        "curPrice": pos.cur_price,
        "title": pos.title,
        "slug": pos.slug,
        "outcome": pos.outcome,
        "outcomeIndex": pos.outcome_index,
        "eventSlug": pos.slug,
        "endDate": pos.end_date,
    }
