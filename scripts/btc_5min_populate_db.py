#!/usr/bin/env python3
"""
Run the BTC 5-min scan and save top traders to the database so the
web dashboard can display them.
"""
import asyncio
import logging
from datetime import date, datetime

from sqlalchemy import delete, select

from polymarket.analyzer.btc_5min_scoring import (
    aggregate_by_wallet,
    compute_wallet_btc5m_metrics,
    passes_btc5m_checklist,
    score_btc5m_wallet,
)
from polymarket.clients.data_api import DataAPIClient
from polymarket.clients.gamma import GammaClient
from polymarket.collector.btc_5min_discovery import (
    collect_market_trades,
    discover_btc_5min_markets,
)
from polymarket.db import async_session, init_db
from polymarket.models.daily_report import DailyReport
from polymarket.models.score import TraderScore
from polymarket.models.trader import Trader

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _tier_from_score(s: float) -> str:
    if s >= 80:
        return "S"
    if s >= 65:
        return "A"
    if s >= 50:
        return "B"
    if s >= 35:
        return "C"
    return "F"


async def main():
    await init_db()

    gamma_client = GammaClient()
    data_client = DataAPIClient()

    try:
        print("Discovering BTC 5-min markets...")
        markets = await discover_btc_5min_markets(gamma_client, n_markets=500)
        print(f"  Found {len(markets)} resolved markets")

        print("Collecting trades per market...")
        market_trades = await collect_market_trades(data_client, markets, concurrency=10)
        total_trades = sum(len(v) for v in market_trades.values())
        print(f"  Fetched {total_trades:,} trades")

        print("Aggregating by wallet...")
        wallet_trades = aggregate_by_wallet(market_trades)
        print(f"  {len(wallet_trades):,} unique wallets")

        market_info = {m["condition_id"]: m for m in markets}
        scored = []
        for wallet, tbm in wallet_trades.items():
            metrics = compute_wallet_btc5m_metrics(wallet, tbm, market_info)
            if metrics["btc5m_trades"] < 20:
                continue
            metrics["score"] = score_btc5m_wallet(metrics)
            metrics["passes_checklist"] = passes_btc5m_checklist(metrics, 24.0)
            scored.append(metrics)

        # Filter to passing checklist and rank
        passing = [w for w in scored if w["passes_checklist"]]
        passing.sort(key=lambda w: w["score"], reverse=True)
        top_100 = passing[:100]
        print(f"  {len(passing)} wallets pass checklist, saving top {len(top_100)}")

        print("Saving to database...")
        async with async_session() as session:
            # Keep score history — only clear very old records (>14 days)
            # so we can track score trends over time
            from datetime import timedelta
            cutoff = datetime.utcnow() - timedelta(days=14)
            await session.execute(
                delete(TraderScore).where(TraderScore.scored_at < cutoff)
            )
            await session.commit()

            top_10_for_report = []

            for rank, m in enumerate(top_100, start=1):
                wallet_addr = m["wallet"]
                # Upsert trader — keep first_seen_at if already exists
                existing = await session.execute(
                    select(Trader).where(Trader.proxy_wallet == wallet_addr)
                )
                trader = existing.scalar_one_or_none()
                if trader is None:
                    trader = Trader(
                        proxy_wallet=wallet_addr,
                        name=m.get("name") or wallet_addr[:10],
                        pseudonym=m.get("name"),
                        first_seen_at=datetime.utcnow(),
                        last_updated_at=datetime.utcnow(),
                        is_active=True,
                    )
                    session.add(trader)
                else:
                    trader.name = m.get("name") or wallet_addr[:10]
                    trader.last_updated_at = datetime.utcnow()
                    trader.is_active = True
                await session.flush()

                # Compute derived metrics to fill the scoring schema
                wins = m["btc5m_wins"]
                losses = m["btc5m_losses"]
                profit_factor = 99.0 if losses == 0 else max(wins / max(losses, 1), 1.0)

                score = TraderScore(
                    trader_id=trader.id,
                    scored_at=datetime.utcnow(),
                    trade_count=m["btc5m_trades"],
                    active_days=m["btc5m_active_hours"] // 24 + 1,
                    time_span_days=1,
                    total_volume=m["btc5m_volume"],
                    unique_markets=m["btc5m_markets"],
                    days_since_last_trade=int(m["btc5m_hours_since_last"] // 24),
                    net_profit=m["btc5m_pnl"],
                    roi=m["btc5m_roi"],
                    win_rate=m["btc5m_win_rate"],
                    profit_factor=profit_factor,
                    sharpe_ratio=0.0,
                    max_drawdown=0.0,
                    recovery_factor=0.0,
                    calmar_ratio=0.0,
                    market_diversity=min(1.0, m["btc5m_markets"] / 200),
                    consistency_score=m["btc5m_win_rate"],
                    position_sizing_score=1.0,
                    liquidity_score=1.0,  # BTC 5-min is always liquid
                    composite_score=m["score"],
                    tier=_tier_from_score(m["score"]),
                    red_flags=[],
                    passes_checklist=m["passes_checklist"],
                )
                session.add(score)

                if rank <= 10:
                    top_10_for_report.append({
                        "rank": rank,
                        "trader_id": trader.id,
                        "proxy_wallet": wallet_addr,
                        "name": m.get("name") or wallet_addr[:10],
                        "composite_score": round(m["score"], 1),
                        "tier": _tier_from_score(m["score"]),
                        "roi": round(m["btc5m_roi"] * 100, 1),
                        "win_rate": round(m["btc5m_win_rate"] * 100, 1),
                        "profit_factor": round(profit_factor, 2),
                        "sharpe_ratio": 0.0,
                        "trade_count": m["btc5m_trades"],
                        "liquidity_score": 100.0,
                        "red_flags": [],
                        "btc5m_pnl": round(m["btc5m_pnl"], 2),
                        "btc5m_volume": round(m["btc5m_volume"], 2),
                        "minutes_since_last": m["btc5m_minutes_since_last"],
                    })

            # Replace any existing report for today
            await session.execute(delete(DailyReport).where(DailyReport.report_date == date.today()))

            report = DailyReport(
                report_date=date.today(),
                created_at=datetime.utcnow(),
                traders_scanned=len(scored),
                traders_passing=len(passing),
                top_10=top_10_for_report,
                summary=(
                    f"BTC 5-Minute Up/Down — Scan of last 500 markets\n"
                    f"Traders analyzed: {len(scored)}\n"
                    f"Passing checklist: {len(passing)}\n"
                    f"Top trader: {top_10_for_report[0]['name']} "
                    f"(${top_10_for_report[0]['btc5m_pnl']:+,.0f} P&L, "
                    f"{top_10_for_report[0]['trade_count']} trades)"
                ),
            )
            session.add(report)
            await session.commit()

        print(f"  Saved {len(top_100)} traders + 1 daily report")
        print("\nDashboard ready. Start it with:")
        print("  PYTHONPATH=. uvicorn polymarket.api.app:app --host 0.0.0.0 --port 8000")

    finally:
        await gamma_client.close()
        await data_client.close()


if __name__ == "__main__":
    asyncio.run(main())
