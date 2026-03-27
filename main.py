"""Split + Dual Ask Bot — Entry Point.

Usage:
  python main.py                    # Run with markets from .env
  python main.py --add btc-100k    # Add a market slug at runtime
  python main.py --once             # Run one scan cycle then exit (dry run)

Flow:
  1. Scan configured markets (fetch orderbooks)
  2. Analyze for dual-ask opportunities (YES_ask + NO_ask > 1.0)
  3. Decide which to execute (risk limits, scoring)
  4. Execute: split USDC → place YES ask + NO ask
  5. Monitor fills via WebSocket, reprice stale orders
"""

import argparse
import asyncio
import logging
import sys

from limitless_sdk.api import HttpClient
from limitless_sdk.markets import MarketFetcher

from bot.config import Config
from bot.scanner import scan_all
from bot.analyzer import analyze_all
from bot.strategy import decide
from bot.executor import Executor
from bot.monitor import Monitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bot")


async def run_cycle(
    cfg: Config,
    fetcher: MarketFetcher,
    executor: Executor,
    monitor: Monitor,
) -> int:
    """Run one scan → analyze → decide → execute cycle. Returns # of trades."""

    # Step 1: Scan
    log.info("=== SCAN (%d markets) ===", len(cfg.market_slugs))
    snapshots = await scan_all(fetcher, cfg)
    if not snapshots:
        log.info("No orderbook data, skipping cycle")
        return 0

    # Step 2: Analyze
    opportunities = analyze_all(snapshots, cfg)
    log.info("Found %d opportunities from %d markets", len(opportunities), len(snapshots))
    if not opportunities:
        return 0

    # Step 3: Decide
    decisions = decide(
        opportunities,
        open_position_count=monitor.open_count,
        total_exposure=monitor.total_exposure,
        cfg=cfg,
    )
    log.info("Approved %d trades", len(decisions))
    if not decisions:
        return 0

    # Step 4: Execute
    for dec in decisions:
        result = await executor.execute(dec)
        if result.success:
            monitor.track(result, dec.split_amount, opportunity=dec.opportunity)
            await monitor.subscribe_market(dec.opportunity.slug)
            log.info("[%s] Trade live!", dec.opportunity.slug)
        else:
            log.error("[%s] Trade failed: %s", dec.opportunity.slug, result.error)

    return len(decisions)


async def main_loop(cfg: Config):
    """Main bot loop: scan → trade → monitor → repeat."""
    http_client = HttpClient(api_key=cfg.api_key)
    fetcher = MarketFetcher(http_client)
    executor = Executor(cfg, http_client, fetcher)
    monitor = Monitor(cfg, executor=executor)

    # Start WebSocket monitoring in background
    ws_task = asyncio.create_task(monitor.start())

    try:
        while True:
            try:
                n = await run_cycle(cfg, fetcher, executor, monitor)
                # Fetch fresh snapshots for stale order repricing
                fresh_snaps = await scan_all(fetcher, cfg)
                await monitor.check_stale_orders(snapshots=fresh_snaps)
                log.info(
                    "Cycle done: %d new trades, %d open positions, $%.2f exposure",
                    n, monitor.open_count, monitor.total_exposure,
                )
            except Exception as e:
                log.exception("Cycle error: %s", e)

            await asyncio.sleep(cfg.reprice_interval)
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        monitor.stop()
        ws_task.cancel()
        await http_client.close()


async def run_once(cfg: Config):
    """Single scan cycle — dry run for testing."""
    http_client = HttpClient(api_key=cfg.api_key)
    fetcher = MarketFetcher(http_client)
    executor = Executor(cfg, http_client, fetcher)
    monitor = Monitor(cfg)

    n = await run_cycle(cfg, fetcher, executor, monitor)
    log.info("Single cycle complete: %d trades", n)
    await http_client.close()


def parse_args():
    parser = argparse.ArgumentParser(description="Split + Dual Ask Bot")
    parser.add_argument(
        "--add", nargs="+", metavar="SLUG",
        help="Add market slugs to trade (in addition to .env)",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run one cycle then exit (for testing)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    cfg = Config()

    if args.add:
        for slug in args.add:
            if slug not in cfg.market_slugs:
                cfg.market_slugs.append(slug)

    if not cfg.market_slugs:
        log.error("No markets configured. Set MARKET_SLUGS in .env or use --add")
        sys.exit(1)

    if not cfg.api_key:
        log.error("LIMITLESS_API_KEY not set")
        sys.exit(1)

    if not cfg.private_key:
        log.error("PRIVATE_KEY not set")
        sys.exit(1)

    log.info("Markets: %s", cfg.market_slugs)
    log.info("Split amount: $%.2f | Min combined ask: %.4f", cfg.split_amount_usdc, cfg.min_combined_ask)

    if args.once:
        asyncio.run(run_once(cfg))
    else:
        asyncio.run(main_loop(cfg))
