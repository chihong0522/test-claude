#!/usr/bin/env python3
"""
Rolling re-selection of smart wallets.

Run this daily (via cron or manually). It:
1. Fetches the last N days of BTC 5-min markets (default 5)
2. Recomputes per-wallet P&L from that fresh window
3. Filters for freshness (must have traded within last 24 hours)
4. Applies minimum participation threshold (>= 30 markets)
5. Saves to data/smart_wallets_latest.json
6. Archives previous version to data/smart_wallets_history/

Usage:
    python scripts/refresh_smart_wallets.py
    python scripts/refresh_smart_wallets.py --days 3 --top 50 --freshness-hours 24

Run as cron (every 6 hours):
    0 */6 * * * cd /home/user/test-claude && python scripts/refresh_smart_wallets.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from polymarket.clients.data_api import DataAPIClient
from polymarket.clients.gamma import GammaClient
from polymarket.collector.btc_5min_discovery import (
    collect_market_trades,
    discover_btc_5min_markets,
)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
LATEST_FILE = DATA_DIR / "smart_wallets_latest.json"
HISTORY_DIR = DATA_DIR / "smart_wallets_history"


def compute_wallet_stats(
    markets: list[dict],
    trades_by_market: dict[str, list[dict]],
) -> dict[str, dict]:
    """Compute per-wallet P&L, trade count, last-trade timestamp."""
    stats: dict[str, dict] = defaultdict(
        lambda: {"pnl": 0.0, "trades": 0, "last_ts": 0, "markets": set()}
    )

    for m in markets:
        if not m.get("resolved") or m.get("winning_index") is None:
            continue
        winning_idx = m["winning_index"]
        cid = m["condition_id"]
        for t in trades_by_market.get(cid, []):
            w = t.get("proxyWallet")
            if not w:
                continue

            entry = stats[w]
            entry["trades"] += 1
            entry["markets"].add(cid)
            ts = int(t.get("timestamp") or 0)
            if ts > entry["last_ts"]:
                entry["last_ts"] = ts

            size = float(t.get("size") or 0)
            price = float(t.get("price") or 0)
            side = (t.get("side") or "BUY").upper()
            outcome_idx = t.get("outcomeIndex") or 0
            is_winning = outcome_idx == winning_idx

            if side == "BUY":
                if is_winning:
                    entry["pnl"] += size * (1.0 - price)
                else:
                    entry["pnl"] -= size * price
            elif side == "SELL":
                if is_winning:
                    entry["pnl"] -= size * (1.0 - price)
                else:
                    entry["pnl"] += size * price

    # Convert sets to counts (not JSON-serializable)
    return {
        w: {
            "pnl": round(s["pnl"], 2),
            "trades": s["trades"],
            "last_ts": s["last_ts"],
            "unique_markets": len(s["markets"]),
        }
        for w, s in stats.items()
    }


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=5, help="Days of history to analyze")
    parser.add_argument("--top", type=int, default=50, help="Top N wallets to keep")
    parser.add_argument("--min-trades", type=int, default=30, help="Minimum trades per wallet")
    parser.add_argument("--min-markets", type=int, default=20, help="Min unique markets")
    parser.add_argument(
        "--freshness-hours",
        type=float,
        default=24.0,
        help="Must have traded within last N hours",
    )
    parser.add_argument("--dry-run", action="store_true", help="Don't save output")
    args = parser.parse_args()

    print("=" * 80)
    print("  ROLLING SMART WALLET REFRESH")
    print("=" * 80)
    print(f"  Window:          last {args.days} days")
    print(f"  Top N:           {args.top}")
    print(f"  Min trades:      {args.min_trades}")
    print(f"  Min markets:     {args.min_markets}")
    print(f"  Freshness:       <= {args.freshness_hours}h")
    print("=" * 80)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)

    n_markets = args.days * 288

    gamma = GammaClient()
    data_api = DataAPIClient()

    try:
        print(f"\n[1/4] Discovering {n_markets} markets...")
        t0 = time.time()
        markets = await discover_btc_5min_markets(gamma, n_markets=n_markets)
        print(f"  Found {len(markets)} resolved markets in {time.time()-t0:.0f}s")

        print(f"\n[2/4] Collecting trades...")
        t0 = time.time()
        trades_by_market = await collect_market_trades(data_api, markets, concurrency=10)
        total_trades = sum(len(v) for v in trades_by_market.values())
        print(f"  Fetched {total_trades:,} trades in {time.time()-t0:.0f}s")

        print(f"\n[3/4] Computing per-wallet stats...")
        stats = compute_wallet_stats(markets, trades_by_market)
        print(f"  Total unique wallets: {len(stats):,}")

        print(f"\n[4/4] Applying filters...")
        now = int(time.time())
        freshness_cutoff = now - int(args.freshness_hours * 3600)

        # Apply filters: min trades, positive PnL, freshness, unique markets
        candidates = [
            (w, s)
            for w, s in stats.items()
            if s["trades"] >= args.min_trades
            and s["unique_markets"] >= args.min_markets
            and s["pnl"] > 0
            and s["last_ts"] >= freshness_cutoff
        ]
        candidates.sort(key=lambda x: x[1]["pnl"], reverse=True)
        top = candidates[: args.top]

        print(f"  After min_trades filter ({args.min_trades}): {sum(1 for s in stats.values() if s['trades'] >= args.min_trades)}")
        print(f"  After min_markets filter ({args.min_markets}): {sum(1 for s in stats.values() if s['trades'] >= args.min_trades and s['unique_markets'] >= args.min_markets)}")
        print(f"  After positive PnL filter: {sum(1 for s in stats.values() if s['trades'] >= args.min_trades and s['unique_markets'] >= args.min_markets and s['pnl'] > 0)}")
        print(f"  After freshness filter (<= {args.freshness_hours}h): {len(candidates)}")
        print(f"  Selected top: {len(top)}")

        # Build output
        output = {
            "refreshed_at": datetime.utcnow().isoformat() + "Z",
            "source_markets_count": len(markets),
            "source_window_days": args.days,
            "total_trades_analyzed": total_trades,
            "total_unique_wallets": len(stats),
            "selection_metadata": {
                "min_trades_threshold": args.min_trades,
                "min_markets_threshold": args.min_markets,
                "min_pnl": 0,
                "freshness_required_hours": args.freshness_hours,
                "top_n": args.top,
            },
            "wallets": [
                {
                    "wallet": w,
                    "pnl": s["pnl"],
                    "trade_count": s["trades"],
                    "unique_markets": s["unique_markets"],
                    "last_trade_ts": s["last_ts"],
                    "hours_since_last_trade": round((now - s["last_ts"]) / 3600, 1),
                }
                for w, s in top
            ],
        }

        # Print summary
        print(f"\nTop 10 smart wallets by PnL:")
        for i, w in enumerate(output["wallets"][:10], 1):
            print(
                f"  {i:>2}. {w['wallet'][:10]}...  "
                f"PnL: ${w['pnl']:>+10,.0f}  "
                f"trades: {w['trade_count']:>5}  "
                f"markets: {w['unique_markets']:>4}  "
                f"last: {w['hours_since_last_trade']:>5.1f}h ago"
            )

        if args.dry_run:
            print("\n[DRY RUN] Would save to", LATEST_FILE)
            return

        # Archive previous latest if exists
        if LATEST_FILE.exists():
            with open(LATEST_FILE, "r") as f:
                prev = json.load(f)
            prev_date = prev.get("refreshed_at", "unknown")[:19].replace(":", "-")
            archive_name = HISTORY_DIR / f"smart_wallets_{prev_date}.json"
            LATEST_FILE.rename(archive_name)
            print(f"\nArchived previous: {archive_name.name}")

        # Write new latest
        with open(LATEST_FILE, "w") as f:
            json.dump(output, f, indent=2)

        print(f"Saved: {LATEST_FILE}")
        print(f"\n✓ Smart wallet pool refreshed: {len(top)} wallets")

    finally:
        await gamma.close()
        await data_api.close()


if __name__ == "__main__":
    asyncio.run(main())
