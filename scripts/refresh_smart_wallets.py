#!/usr/bin/env python3
"""
Rolling re-selection of smart wallets (v2 — quality-first).

Previous version ranked wallets purely by in-sample PnL, which caused two
failure modes:
  1. Wallets with high PnL from OTHER strategies polluted the consensus
     (signal-time accuracy dropped to 36-48% for 9 specific wallets).
  2. Market makers providing two-sided liquidity looked profitable on PnL
     but had zero directional edge.

This v2:
  1. Splits markets chronologically into train / validate.
  2. Bootstraps candidate pool from positive-PnL wallets on TRAIN.
  3. Replays ensemble voting on train to compute per-wallet *signal-time
     accuracy* (how often a wallet's vote matched the correct outcome when
     the bucket fired a signal).
  4. Blacklists:
       - market makers (|PnL| / (trade_count * $100) < 0.01)
       - wallets with >= 100 signal participations and accuracy < 50%
  5. Keeps wallets with accuracy >= 52% and binomial p <= 0.10.
  6. Validates survivors OOS on the held-out validate set — drops any
     wallet whose OOS accuracy collapses below 52% (with >= 5 OOS signals).
  7. Ranks by signal_time_accuracy * log(participations+1).
  8. Saves top-N with full metrics + OOS performance + dropped-wallet log.

The cron cadence (every 6 hours) becomes a rolling re-selection: each run
reconsiders the pool from scratch on fresh data, so bad wallets are
automatically expelled next cycle if they keep underperforming.

Usage:
    python scripts/refresh_smart_wallets.py
    python scripts/refresh_smart_wallets.py --days 5 --top 50 --train-frac 0.6

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

from polymarket.analyzer.wallet_signal_accuracy import (
    WalletSignalMetrics,
    apply_blacklist_filters,
    compute_wallet_signal_metrics,
    rank_wallets,
    validate_oos,
)
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


def _market_end_ts(m: dict) -> float:
    end = m.get("end_date")
    if not end:
        return 0
    try:
        return datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0


def compute_raw_pnl(
    markets: list[dict],
    trades_by_market: dict[str, list[dict]],
) -> dict[str, dict]:
    """Per-wallet PnL + trade count + last_ts + unique markets (no quality filter)."""
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
            e = stats[w]
            e["trades"] += 1
            e["markets"].add(cid)
            ts = int(t.get("timestamp") or 0)
            if ts > e["last_ts"]:
                e["last_ts"] = ts
            size = float(t.get("size") or 0)
            price = float(t.get("price") or 0)
            side = (t.get("side") or "BUY").upper()
            outcome_idx = t.get("outcomeIndex") or 0
            is_winning = outcome_idx == winning_idx
            if side == "BUY":
                if is_winning:
                    e["pnl"] += size * (1.0 - price)
                else:
                    e["pnl"] -= size * price
            elif side == "SELL":
                if is_winning:
                    e["pnl"] -= size * (1.0 - price)
                else:
                    e["pnl"] += size * price
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
    parser.add_argument("--top", type=int, default=50, help="Max wallets to keep")
    parser.add_argument("--min-trades", type=int, default=30, help="Min 5-min trades per wallet")
    parser.add_argument("--min-markets", type=int, default=20, help="Min unique markets")
    parser.add_argument(
        "--freshness-hours",
        type=float,
        default=24.0,
        help="Must have traded within last N hours",
    )
    parser.add_argument(
        "--train-frac",
        type=float,
        default=0.60,
        help="Chronological train fraction (remainder = OOS validate)",
    )
    parser.add_argument(
        "--bootstrap-size",
        type=int,
        default=150,
        help="Initial candidate pool size for signal replay (by train PnL)",
    )
    parser.add_argument(
        "--min-signal-participations",
        type=int,
        default=30,
        help="Minimum signal buckets a wallet must have voted in to be ranked",
    )
    parser.add_argument(
        "--min-signal-accuracy",
        type=float,
        default=0.52,
        help="Minimum signal-time accuracy on train (above chance)",
    )
    parser.add_argument(
        "--min-oos-accuracy",
        type=float,
        default=0.52,
        help="Minimum OOS accuracy on validate set",
    )
    parser.add_argument(
        "--min-seconds-remaining",
        type=int,
        default=180,
        help="Only count signals fired with >= N seconds remaining (matches live bot)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Don't save output")
    args = parser.parse_args()

    print("=" * 90)
    print("  ROLLING SMART WALLET REFRESH — v2 (signal-time accuracy)")
    print("=" * 90)
    print(f"  Window:          last {args.days} days")
    print(f"  Train frac:      {args.train_frac:.0%} train / {1-args.train_frac:.0%} validate")
    print(f"  Bootstrap pool:  top {args.bootstrap_size} by train PnL")
    print(f"  Top N output:    {args.top}")
    print(f"  Min trades:      {args.min_trades}")
    print(f"  Min markets:     {args.min_markets}")
    print(f"  Time gate:       signals with >= {args.min_seconds_remaining}s remaining")
    print(f"  Min sig accur:   train {args.min_signal_accuracy:.0%}  OOS {args.min_oos_accuracy:.0%}")
    print(f"  Freshness:       <= {args.freshness_hours}h")
    print("=" * 90)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)

    n_markets = args.days * 288

    gamma = GammaClient()
    data_api = DataAPIClient()

    try:
        print(f"\n[1/6] Discovering {n_markets} markets...")
        t0 = time.time()
        markets = await discover_btc_5min_markets(gamma, n_markets=n_markets)
        print(f"  Found {len(markets)} resolved markets in {time.time()-t0:.0f}s")

        print(f"\n[2/6] Collecting trades...")
        t0 = time.time()
        trades_by_market = await collect_market_trades(data_api, markets, concurrency=10)
        total_trades = sum(len(v) for v in trades_by_market.values())
        print(f"  Fetched {total_trades:,} trades in {time.time()-t0:.0f}s")

        # Chronological split
        sorted_markets = sorted(markets, key=_market_end_ts)
        train_cutoff = int(len(sorted_markets) * args.train_frac)
        train_markets = sorted_markets[:train_cutoff]
        validate_markets = sorted_markets[train_cutoff:]

        def _fmt(m_list):
            if not m_list:
                return "empty"
            t0 = _market_end_ts(m_list[0])
            t1 = _market_end_ts(m_list[-1])
            return (
                f"{datetime.utcfromtimestamp(t0).strftime('%m-%d %H:%M')} → "
                f"{datetime.utcfromtimestamp(t1).strftime('%m-%d %H:%M')}"
            )

        print(f"\n[3/6] Chronological split:")
        print(f"  Train:    {len(train_markets)} markets  ({_fmt(train_markets)})")
        print(f"  Validate: {len(validate_markets)} markets  ({_fmt(validate_markets)})")

        # Bootstrap candidate pool by raw PnL on TRAIN
        print(f"\n[4/6] Bootstrap candidate pool from train PnL...")
        train_stats = compute_raw_pnl(train_markets, trades_by_market)
        bootstrap = [
            (w, s)
            for w, s in train_stats.items()
            if s["trades"] >= args.min_trades
            and s["unique_markets"] >= args.min_markets
            and s["pnl"] > 0
        ]
        bootstrap.sort(key=lambda x: x[1]["pnl"], reverse=True)
        bootstrap = bootstrap[: args.bootstrap_size]
        bootstrap_set = {w for w, _ in bootstrap}
        print(f"  Bootstrap pool: {len(bootstrap_set)} candidates")

        # Compute signal-time accuracy on train set using bootstrap pool as the voting set
        print(f"\n[5/6] Replaying ensemble voting on train (min_remaining={args.min_seconds_remaining}s)...")
        train_metrics = compute_wallet_signal_metrics(
            train_markets,
            trades_by_market,
            candidate_wallets=bootstrap_set,
            min_distinct_wallets=7,
            signal_dominance=2.0,
            bucket_sec=10,
            min_seconds_remaining=args.min_seconds_remaining,
        )
        n_with_signals = sum(
            1 for m in train_metrics.values() if m.signal_participations > 0
        )
        print(f"  Wallets with >=1 signal participation: {n_with_signals}")

        # Apply blacklists (MM + bad signal accuracy)
        dropped = apply_blacklist_filters(
            train_metrics,
            min_participations_for_accuracy_filter=100,
            max_bad_accuracy=0.50,
        )
        mm_dropped = [(w, r) for w, r in dropped if r.startswith("market_maker")]
        bad_dropped = [(w, r) for w, r in dropped if r.startswith("bad_signal")]
        print(f"  Market makers removed:       {len(mm_dropped)}")
        print(f"  Bad-accuracy wallets removed: {len(bad_dropped)}")
        for w, r in bad_dropped:
            print(f"    - {w[:12]}... {r}")

        # Rank by signal accuracy
        ranked = rank_wallets(
            train_metrics,
            min_participations=args.min_signal_participations,
            min_accuracy=args.min_signal_accuracy,
            max_p_value=None,
        )
        print(f"  After train filter ranking: {len(ranked)} eligible wallets")

        # OOS validation
        print(f"\n[6/6] OOS validation on held-out set...")
        survivors = validate_oos(
            ranked,
            validate_markets,
            trades_by_market,
            min_distinct_wallets=7,
            signal_dominance=2.0,
            bucket_sec=10,
            min_seconds_remaining=args.min_seconds_remaining,
            min_oos_accuracy=args.min_oos_accuracy,
            min_oos_participations=5,
        )
        print(f"  Survivors after OOS filter: {len(survivors)}")

        # Freshness + sample-size filters on the survivors
        now = int(time.time())
        freshness_cutoff = now - int(args.freshness_hours * 3600)
        final: list[WalletSignalMetrics] = []
        for c in survivors:
            raw = train_stats.get(c.wallet, {})
            if raw.get("trades", 0) < args.min_trades:
                continue
            if raw.get("unique_markets", 0) < args.min_markets:
                continue
            if raw.get("last_ts", 0) < freshness_cutoff:
                continue
            final.append(c)
        final = final[: args.top]
        print(f"  After freshness+sample filters: {len(final)}")

        if not final:
            print("\n  WARNING: no wallets survived all filters. Pool would be empty.")
            print("  Consider relaxing --min-signal-accuracy or --train-frac.")

        # Build output
        output = {
            "refreshed_at": datetime.utcnow().isoformat() + "Z",
            "version": 2,
            "source_markets_count": len(markets),
            "source_window_days": args.days,
            "total_trades_analyzed": total_trades,
            "train_markets": len(train_markets),
            "validate_markets": len(validate_markets),
            "selection_metadata": {
                "train_frac": args.train_frac,
                "bootstrap_pool_size": len(bootstrap_set),
                "min_distinct_wallets": 7,
                "min_signal_participations": args.min_signal_participations,
                "min_signal_accuracy_train": args.min_signal_accuracy,
                "min_signal_accuracy_oos": args.min_oos_accuracy,
                "min_seconds_remaining": args.min_seconds_remaining,
                "freshness_hours": args.freshness_hours,
            },
            "dropped": {
                "market_makers": [w for w, _ in mm_dropped],
                "bad_signal_accuracy": [{"wallet": w, "reason": r} for w, r in bad_dropped],
            },
            "wallets": [],
        }
        for c in final:
            raw = train_stats.get(c.wallet, {})
            output["wallets"].append(
                {
                    "wallet": c.wallet,
                    "pnl": round(c.pnl, 2),
                    "trade_count": c.trade_count,
                    "unique_markets": raw.get("unique_markets", 0),
                    "last_trade_ts": raw.get("last_ts", 0),
                    "hours_since_last_trade": round((now - raw.get("last_ts", now)) / 3600, 1),
                    "signal_participations": c.signal_participations,
                    "signal_wins": c.signal_wins,
                    "signal_time_accuracy": round(c.signal_time_accuracy, 4),
                    "p_value": round(c.p_value, 4),
                    "edge_per_trade": round(c.edge_per_trade, 4),
                    "oos_participations": c.oos_participations,
                    "oos_wins": c.oos_wins,
                    "oos_accuracy": round(c.oos_accuracy, 4),
                }
            )

        # Print summary
        print(f"\nTop 15 by signal-time accuracy:")
        print(
            f"  {'rank':<4} {'wallet':<14} {'train_acc':>9} {'n':>5} "
            f"{'oos_acc':>8} {'oos_n':>5} {'pnl':>10}"
        )
        for i, w in enumerate(output["wallets"][:15], 1):
            print(
                f"  {i:<4} {w['wallet'][:12]}.. "
                f"{w['signal_time_accuracy']*100:>7.1f}% "
                f"{w['signal_participations']:>5} "
                f"{w['oos_accuracy']*100:>6.1f}% "
                f"{w['oos_participations']:>5} "
                f"${w['pnl']:>+8,.0f}"
            )

        if args.dry_run:
            print("\n[DRY RUN] Would save to", LATEST_FILE)
            return

        # Archive previous latest if exists
        if LATEST_FILE.exists():
            try:
                with open(LATEST_FILE, "r") as f:
                    prev = json.load(f)
                prev_date = prev.get("refreshed_at", "unknown")[:19].replace(":", "-")
                archive_name = HISTORY_DIR / f"smart_wallets_{prev_date}.json"
                LATEST_FILE.rename(archive_name)
                print(f"\nArchived previous: {archive_name.name}")
            except Exception as e:
                logger.warning(f"Archive failed: {e}")

        # Write new latest
        with open(LATEST_FILE, "w") as f:
            json.dump(output, f, indent=2)

        print(f"Saved: {LATEST_FILE}")
        print(f"\n✓ Smart wallet pool refreshed: {len(final)} wallets")

    finally:
        await gamma.close()
        await data_api.close()


if __name__ == "__main__":
    asyncio.run(main())
