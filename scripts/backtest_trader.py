#!/usr/bin/env python3
"""Backtest a specific trader wallet."""
import argparse
import asyncio
import json
import logging

from polymarket.backtester.report import format_backtest_summary
from polymarket.backtester.simulator import BacktestConfig, run_backtest
from polymarket.clients.data_api import DataAPIClient
from polymarket.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


async def main():
    parser = argparse.ArgumentParser(description="Backtest copy-trading a Polymarket wallet")
    parser.add_argument("wallet", help="Polymarket proxy wallet address")
    parser.add_argument("--capital", type=float, default=settings.default_initial_capital)
    parser.add_argument("--position-pct", type=float, default=settings.default_position_pct)
    parser.add_argument("--slippage-bps", type=int, default=settings.default_slippage_bps)
    args = parser.parse_args()

    client = DataAPIClient()
    try:
        print(f"Fetching trades for {args.wallet}...")
        trades = await client.get_all_trades(args.wallet)
        print(f"Found {len(trades)} trades")

        if not trades:
            print("No trades found. Check the wallet address.")
            return

        config = BacktestConfig(
            initial_capital=args.capital,
            position_pct=args.position_pct,
            slippage_bps=args.slippage_bps,
        )

        print(f"Running backtest (capital=${args.capital}, position={args.position_pct*100}%, slippage={args.slippage_bps}bps)...")
        result = run_backtest(trades, config)
        summary = format_backtest_summary(result)

        print("\n=== Backtest Results ===")
        print(json.dumps(summary, indent=2))

    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
