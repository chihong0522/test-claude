#!/usr/bin/env python3
"""Backtest split-capital direct copy-trading of selected smart wallets."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from polymarket.backtester.portfolio import (
    allocate_capital,
    filter_trades_to_market_window,
    select_wallet_rows,
    summarize_portfolio_results,
)
from polymarket.backtester.simulator import BacktestConfig, run_backtest
from polymarket.clients.data_api import DataAPIClient
from polymarket.clients.gamma import GammaClient
from polymarket.collector.btc_5min_discovery import discover_btc_5min_markets
from scripts.ensemble_backtest import _parse_end_ts

REPO_ROOT = Path(__file__).resolve().parent.parent
SMART_WALLETS_FILE = REPO_ROOT / "data" / "smart_wallets_latest.json"


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest split-capital direct wallet copy on BTC 5-minute markets")
    parser.add_argument("--wallet-set", choices=["elite", "top"], default="elite")
    parser.add_argument("--top-n", type=int, default=5, help="How many wallets to include (0 = all matches)")
    parser.add_argument("--weighting", choices=["equal", "tiered"], default="equal")
    parser.add_argument("--capital", type=float, default=3000.0)
    parser.add_argument("--markets", type=int, default=30, help="How many recent BTC 5-minute markets to evaluate")
    parser.add_argument("--position-pct", type=float, default=0.02)
    parser.add_argument("--slippage-bps", type=int, default=30)
    parser.add_argument("--concurrency", type=int, default=2)
    args = parser.parse_args()

    if not SMART_WALLETS_FILE.exists():
        raise RuntimeError(f"Missing smart-wallet pool: {SMART_WALLETS_FILE}")

    pool_data = json.loads(SMART_WALLETS_FILE.read_text())
    selected_wallets = select_wallet_rows(pool_data, wallet_set=args.wallet_set, top_n=args.top_n)
    allocations = allocate_capital(selected_wallets, total_capital=args.capital, weighting=args.weighting)

    gamma = GammaClient()
    data_api = DataAPIClient()
    try:
        markets = await discover_btc_5min_markets(gamma, n_markets=args.markets)
        markets = [m for m in markets if m.get("resolved") and m.get("winning_index") is not None]
        if not markets:
            raise RuntimeError("No resolved BTC 5-minute markets found for the requested sample")

        condition_ids = {m["condition_id"] for m in markets}
        market_outcomes = {m["condition_id"]: m["winning_index"] for m in markets}
        start_ts = int(min(_parse_end_ts(m) for m in markets) - 300)
        end_ts = int(max(_parse_end_ts(m) for m in markets))

        print("=" * 90)
        print("  DIRECT WALLET COPY BACKTEST")
        print("=" * 90)
        print(f"  Pool refreshed: {pool_data.get('refreshed_at', 'unknown')}")
        print(f"  Wallet set:     {args.wallet_set}")
        print(f"  Wallet count:   {len(selected_wallets)}")
        print(f"  Weighting:      {args.weighting}")
        print(f"  Capital:        ${args.capital:,.2f}")
        print(f"  Markets:        {len(markets)}")
        print(f"  Window:         {_fmt_ts(start_ts)} -> {_fmt_ts(end_ts)}")
        print(f"  Position pct:   {args.position_pct:.2%}")
        print(f"  Slippage:       {args.slippage_bps} bps")
        print("=" * 90)
        print("\n  Selected wallets:")
        for row in selected_wallets:
            wallet = row["wallet"]
            print(
                f"    #{row['rank']:>2} {wallet[:12]}..  "
                f"tier={row['derived_weight']:.1f}x  "
                f"oos={row.get('oos_accuracy', 0.0):.1%}  "
                f"sleeve=${allocations[wallet]:.2f}"
            )

        sem = asyncio.Semaphore(max(1, args.concurrency))

        async def evaluate_wallet(row: dict) -> dict:
            wallet = row["wallet"]
            async with sem:
                trades = await data_api.get_all_trades(wallet)
            filtered_trades = filter_trades_to_market_window(
                trades,
                condition_ids=condition_ids,
                start_ts=start_ts,
                end_ts=end_ts,
            )
            result = run_backtest(
                filtered_trades,
                BacktestConfig(
                    initial_capital=allocations[wallet],
                    position_pct=args.position_pct,
                    max_position_pct=max(args.position_pct, 0.10),
                    slippage_bps=args.slippage_bps,
                ),
                market_outcomes=market_outcomes,
            )
            return {
                "wallet": wallet,
                "rank": row["rank"],
                "derived_weight": row["derived_weight"],
                "oos_accuracy": row.get("oos_accuracy", 0.0),
                "allocation": allocations[wallet],
                "all_trades": len(trades),
                "filtered_trades": len(filtered_trades),
                "copied_events": result.total_trades_copied,
                "final_capital": result.final_capital,
                "return_pct": result.total_return,
                "win_rate_pct": result.win_rate,
                "max_drawdown_pct": result.max_drawdown,
            }

        wallet_results = await asyncio.gather(*[evaluate_wallet(row) for row in selected_wallets])
        wallet_results.sort(key=lambda r: r["return_pct"], reverse=True)
        summary = summarize_portfolio_results(wallet_results, total_capital=args.capital)
        summary.update(
            {
                "wallet_set": args.wallet_set,
                "weighting": args.weighting,
                "capital": args.capital,
                "markets": len(markets),
                "window_start_ts": start_ts,
                "window_end_ts": end_ts,
                "wallet_results": wallet_results,
            }
        )

        print("\n" + "=" * 90)
        print("  PORTFOLIO SUMMARY")
        print("=" * 90)
        print(f"  Final capital:       ${summary['portfolio_final_capital']:,.2f}")
        print(f"  Portfolio P&L:       ${summary['portfolio_pnl']:+,.2f}")
        print(f"  Portfolio return:    {summary['portfolio_return_pct']:+.2f}%")
        print(f"  Wallets with trades: {summary['wallets_with_trades']}/{summary['wallet_count']}")
        print(f"  Filtered trades:     {summary['total_filtered_trades']}")
        print(f"  Copied trade events: {summary['total_copied_events']}")

        print("\n  Per-wallet breakdown:")
        for row in wallet_results:
            print(
                f"    #{row['rank']:>2} {row['wallet'][:12]}..  "
                f"ret={row['return_pct']:+.2f}%  final=${row['final_capital']:.2f}  "
                f"btc5m_trades={row['filtered_trades']}  copied={row['copied_events']}  "
                f"dd={row['max_drawdown_pct']:.2f}%"
            )

        print("\n  JSON summary:")
        print(json.dumps(summary, indent=2))

    finally:
        await gamma.close()
        await data_api.close()


if __name__ == "__main__":
    asyncio.run(main())
