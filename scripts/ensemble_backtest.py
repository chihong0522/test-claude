#!/usr/bin/env python3
"""
Ensemble Voting Backtest for BTC 5-minute markets.

Strategy:
- Identify "smart wallets" (profitable traders) from training data
- Within smart wallets, identify "leaders" (earliest movers per market)
- Every N seconds during a market, count leader BUY votes for Up vs Down
- Enter position when a strong consensus forms AND price hasn't drifted too far
- Measure P&L at market resolution

Tests multiple configurations to find the best combination of filters:
  A) Baseline — all smart wallets, no filters
  B) Leaders only — top 10 earliest movers
  C) Leaders + drift filter
  D) Leaders + drift + time window filter
  E) Leaders + drift + time + sustained consensus

Data: reuses BTC 5-min discovery + trade collection from
scripts/btc_5min_populate_db.py. Results are cached to disk for rapid
iteration on configs without re-fetching the API.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import pickle
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from polymarket.clients.data_api import DataAPIClient
from polymarket.clients.gamma import GammaClient
from polymarket.collector.btc_5min_discovery import (
    collect_market_trades,
    discover_btc_5min_markets,
)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CACHE_FILE = "/tmp/btc5m_backtest_cache.pkl"
CACHE_MAX_AGE_SEC = 3600  # 1 hour


# ══════════════════════════════════════════════════════════════════════════
# Data loading (cached)
# ══════════════════════════════════════════════════════════════════════════


async def load_data(n_markets: int = 500, force_refresh: bool = False):
    """Load cached data or fetch fresh from the API."""
    if not force_refresh and os.path.exists(CACHE_FILE):
        age = time.time() - os.path.getmtime(CACHE_FILE)
        if age < CACHE_MAX_AGE_SEC:
            print(f"  [cache] Using cached data (age: {age:.0f}s)")
            with open(CACHE_FILE, "rb") as f:
                return pickle.load(f)

    print("  [api] Fetching fresh data from Polymarket...")
    gamma = GammaClient()
    data = DataAPIClient()
    try:
        markets = await discover_btc_5min_markets(gamma, n_markets=n_markets)
        trades = await collect_market_trades(data, markets, concurrency=10)
    finally:
        await gamma.close()
        await data.close()

    cache = (markets, trades)
    with open(CACHE_FILE, "wb") as f:
        pickle.dump(cache, f)
    return cache


# ══════════════════════════════════════════════════════════════════════════
# Smart wallet identification (from TRAIN set only — avoids look-ahead)
# ══════════════════════════════════════════════════════════════════════════


def _parse_end_ts(market: dict) -> float:
    """Parse market endDate to unix timestamp."""
    end = market.get("end_date")
    if not end:
        return 0
    try:
        return datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0


def compute_wallet_pnl_and_counts(
    train_markets: list[dict],
    trades_by_market: dict[str, list[dict]],
) -> tuple[dict[str, float], dict[str, int]]:
    """Compute per-wallet P&L and trade counts from the train set."""
    wallet_pnl: dict[str, float] = defaultdict(float)
    wallet_trade_count: dict[str, int] = defaultdict(int)

    for m in train_markets:
        if not m.get("resolved") or m.get("winning_index") is None:
            continue
        winning_idx = m["winning_index"]
        cid = m["condition_id"]
        trades = trades_by_market.get(cid, [])

        for t in trades:
            w = t.get("proxyWallet")
            if not w:
                continue
            wallet_trade_count[w] += 1

            size = float(t.get("size") or 0)
            price = float(t.get("price") or 0)
            side = (t.get("side") or "BUY").upper()
            outcome_idx = t.get("outcomeIndex") or 0
            is_winning = outcome_idx == winning_idx

            if side == "BUY":
                if is_winning:
                    wallet_pnl[w] += size * (1.0 - price)
                else:
                    wallet_pnl[w] -= size * price
            elif side == "SELL":
                if is_winning:
                    wallet_pnl[w] -= size * (1.0 - price)
                else:
                    wallet_pnl[w] += size * price

    return wallet_pnl, wallet_trade_count


def compute_smart_wallets(
    train_markets: list[dict],
    trades_by_market: dict[str, list[dict]],
    top_n: int = 50,
    min_trades: int = 30,
) -> list[str]:
    """Top profitable wallets from training set."""
    wallet_pnl, wallet_trade_count = compute_wallet_pnl_and_counts(
        train_markets, trades_by_market
    )
    candidates = [
        (w, pnl)
        for w, pnl in wallet_pnl.items()
        if wallet_trade_count[w] >= min_trades and pnl > 0
    ]
    candidates.sort(key=lambda x: x[1], reverse=True)
    return [w for w, _ in candidates[:top_n]]


def compute_unprofitable_wallets(
    train_markets: list[dict],
    trades_by_market: dict[str, list[dict]],
    top_n: int = 50,
    min_trades: int = 30,
) -> list[str]:
    """Worst-performing wallets (control group — should LOSE if strategy is real)."""
    wallet_pnl, wallet_trade_count = compute_wallet_pnl_and_counts(
        train_markets, trades_by_market
    )
    candidates = [
        (w, pnl)
        for w, pnl in wallet_pnl.items()
        if wallet_trade_count[w] >= min_trades and pnl < 0
    ]
    candidates.sort(key=lambda x: x[1])  # most negative first
    return [w for w, _ in candidates[:top_n]]


def compute_random_active_wallets(
    train_markets: list[dict],
    trades_by_market: dict[str, list[dict]],
    n: int = 50,
    min_trades: int = 30,
    seed: int = 42,
) -> list[str]:
    """Random active wallets (control group — should be no better than coin flip if strategy is real)."""
    import random
    _, wallet_trade_count = compute_wallet_pnl_and_counts(train_markets, trades_by_market)
    active = [w for w, c in wallet_trade_count.items() if c >= min_trades]
    rng = random.Random(seed)
    rng.shuffle(active)
    return active[:n]


# ══════════════════════════════════════════════════════════════════════════
# Leader identification (who trades earliest in each market)
# ══════════════════════════════════════════════════════════════════════════


def identify_leaders(
    smart_wallets: list[str],
    train_markets: list[dict],
    trades_by_market: dict[str, list[dict]],
    top_k: int = 10,
    min_markets: int = 5,
) -> list[tuple[str, float, int]]:
    """Rank smart wallets by how early they enter markets. Returns (wallet, median_offset_sec, n_markets)."""
    smart_set = set(smart_wallets)
    wallet_timings: dict[str, list[float]] = defaultdict(list)

    for m in train_markets:
        end_ts = _parse_end_ts(m)
        if end_ts == 0:
            continue
        start_ts = end_ts - 300  # 5-min window

        cid = m["condition_id"]
        trades = sorted(
            trades_by_market.get(cid, []),
            key=lambda t: int(t.get("timestamp") or 0),
        )

        wallet_first_trade: dict[str, float] = {}
        for t in trades:
            w = t.get("proxyWallet")
            if w not in smart_set:
                continue
            if w in wallet_first_trade:
                continue
            offset = int(t.get("timestamp") or 0) - start_ts
            if 0 <= offset <= 300:
                wallet_first_trade[w] = offset

        for w, offset in wallet_first_trade.items():
            wallet_timings[w].append(offset)

    ranked: list[tuple[str, float, int]] = []
    for w, timings in wallet_timings.items():
        if len(timings) >= min_markets:
            median_offset = statistics.median(timings)
            ranked.append((w, median_offset, len(timings)))

    ranked.sort(key=lambda x: x[1])  # ascending (earlier first)
    return ranked[:top_k]


# ══════════════════════════════════════════════════════════════════════════
# Backtest simulator
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class BacktestConfig:
    name: str
    leader_wallets: set[str]
    min_signal_strength: int = 3
    signal_dominance: float = 2.0  # winning votes >= dominance * losing votes
    drift_threshold: float = 0.04
    window_start_sec: int = 30
    window_end_sec: int = 240
    bucket_sec: int = 10
    sustained_windows: int = 2
    position_size_usd: float = 60.0
    fee_pct: float = 0.02
    apply_drift_filter: bool = True
    apply_time_filter: bool = True
    apply_sustained: bool = False
    allow_flips: bool = True  # if False, enter once per market and hold
    invert_drift: bool = False  # if True, only trade when drift > threshold (inverse filter)
    latency_buckets: int = 0  # number of bucket_sec delays before we can execute (realism)


def _up_price_of_trade(t: dict) -> float:
    """Return the trade's implied UP-token price (normalize NO trades)."""
    price = float(t.get("price") or 0.5)
    outcome_idx = t.get("outcomeIndex") or 0
    if outcome_idx == 0:
        return price  # already Up
    return 1.0 - price  # Down trade -> implied Up probability


def simulate_market(market: dict, trades: list[dict], cfg: BacktestConfig) -> dict | None:
    """Simulate the strategy on one market. Returns result dict or None."""
    if not market.get("resolved") or market.get("winning_index") is None:
        return None

    winning_idx = market["winning_index"]
    end_ts = _parse_end_ts(market)
    if end_ts == 0:
        return None
    start_ts = end_ts - 300

    sorted_trades = sorted(trades, key=lambda t: int(t.get("timestamp") or 0))
    buckets: dict[int, list[dict]] = defaultdict(list)
    for t in sorted_trades:
        offset = int(t.get("timestamp") or 0) - start_ts
        if 0 <= offset <= 300:
            bucket_idx = int(offset // cfg.bucket_sec)
            buckets[bucket_idx].append(t)

    # Pre-compute last UP-price as of end of each bucket
    last_up_price: dict[int, float] = {}
    running_price = 0.5
    max_bucket = int(300 // cfg.bucket_sec)
    for bi in range(max_bucket + 1):
        for t in buckets.get(bi, []):
            running_price = _up_price_of_trade(t)
        last_up_price[bi] = running_price

    sustained_yes = 0
    sustained_no = 0
    # Position state: (side, entry_up_price, size_shares, cost_basis_usd)
    position: tuple[str, float, float, float] | None = None
    realized_pnl = 0.0  # booked on each flip
    entries = 0
    flips = 0

    for bucket_idx in sorted(buckets.keys()):
        offset_sec = bucket_idx * cfg.bucket_sec
        if cfg.apply_time_filter and (
            offset_sec < cfg.window_start_sec or offset_sec > cfg.window_end_sec
        ):
            continue

        bucket = buckets[bucket_idx]
        leader_trades = [t for t in bucket if t.get("proxyWallet") in cfg.leader_wallets]
        yes_votes = [t for t in leader_trades if (t.get("side") or "BUY").upper() == "BUY"
                     and (t.get("outcomeIndex") or 0) == 0]
        no_votes = [t for t in leader_trades if (t.get("side") or "BUY").upper() == "BUY"
                    and (t.get("outcomeIndex") or 0) == 1]

        signal: str | None = None
        signal_avg_up_price: float = 0.5
        if (
            len(yes_votes) >= cfg.min_signal_strength
            and len(yes_votes) >= cfg.signal_dominance * max(len(no_votes), 1)
        ):
            signal = "YES"
            signal_avg_up_price = statistics.mean(
                [float(t.get("price") or 0.5) for t in yes_votes]
            )
        elif (
            len(no_votes) >= cfg.min_signal_strength
            and len(no_votes) >= cfg.signal_dominance * max(len(yes_votes), 1)
        ):
            signal = "NO"
            # convert to implied UP price for drift comparison
            signal_avg_up_price = 1.0 - statistics.mean(
                [float(t.get("price") or 0.5) for t in no_votes]
            )

        # Sustained consensus counter
        if signal == "YES":
            sustained_yes += 1
            sustained_no = 0
        elif signal == "NO":
            sustained_no += 1
            sustained_yes = 0
        else:
            sustained_yes = 0
            sustained_no = 0

        if cfg.apply_sustained:
            if signal == "YES" and sustained_yes < cfg.sustained_windows:
                signal = None
            elif signal == "NO" and sustained_no < cfg.sustained_windows:
                signal = None

        if signal is None:
            continue

        # Apply latency: our execution price is from N buckets later
        exec_bucket_idx = bucket_idx + cfg.latency_buckets
        if exec_bucket_idx > max_bucket:
            continue  # market closed before we could execute

        # Drift filter
        current_up_price = last_up_price.get(exec_bucket_idx, last_up_price.get(bucket_idx, 0.5))
        drift = abs(current_up_price - signal_avg_up_price)
        if cfg.apply_drift_filter:
            if cfg.invert_drift:
                # Only take signals where drift is HIGH (strong trending signal)
                if drift < cfg.drift_threshold:
                    continue
            else:
                # Only take signals where drift is LOW (price hasn't run away)
                if drift > cfg.drift_threshold:
                    continue

        # Determine entry price for OUR side (what we'd pay)
        our_entry_price = current_up_price if signal == "YES" else 1.0 - current_up_price

        # Avoid pathological prices (nothing useful if we'd pay $0.99)
        if our_entry_price < 0.05 or our_entry_price > 0.95:
            continue

        if position is None:
            # Fresh entry: spend fixed $position_size_usd (with fee)
            size = cfg.position_size_usd / our_entry_price
            cost_basis = cfg.position_size_usd * (1 + cfg.fee_pct)
            position = (signal, our_entry_price, size, cost_basis)
            entries += 1
        elif position[0] != signal:
            if not cfg.allow_flips:
                continue  # hold original position
            # Flip: close old position at current price, book realized P&L
            old_side, old_entry, old_size, old_cost = position
            old_current = current_up_price if old_side == "YES" else 1.0 - current_up_price
            proceeds = old_size * old_current * (1 - cfg.fee_pct)
            realized_pnl += proceeds - old_cost
            # Open fresh $position_size_usd position on new side (NO compounding)
            new_size = cfg.position_size_usd / our_entry_price
            new_cost = cfg.position_size_usd * (1 + cfg.fee_pct)
            position = (signal, our_entry_price, new_size, new_cost)
            flips += 1

    if position is None:
        return {
            "market": market["condition_id"],
            "pnl": 0.0,
            "action": "NO_SIGNAL",
            "winning_idx": winning_idx,
        }

    side, entry, size, cost_basis = position
    pos_idx = 0 if side == "YES" else 1
    settlement = size * 1.0 if pos_idx == winning_idx else 0.0
    final_pnl = settlement - cost_basis
    total_pnl = realized_pnl + final_pnl

    return {
        "market": market["condition_id"],
        "action": "TRADE",
        "position": side,
        "entry_price": round(entry, 4),
        "size_shares": round(size, 2),
        "winning_idx": winning_idx,
        "correct": pos_idx == winning_idx,
        "pnl": round(total_pnl, 2),
        "realized_pnl_from_flips": round(realized_pnl, 2),
        "final_settlement_pnl": round(final_pnl, 2),
        "entries": entries,
        "flips": flips,
    }


# ══════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════


def summarize(name: str, test_markets: list[dict], results: list[dict], cfg: BacktestConfig):
    traded = [r for r in results if r.get("action") == "TRADE"]
    no_signal = [r for r in results if r.get("action") == "NO_SIGNAL"]
    wins = sum(1 for r in traded if r.get("correct"))
    losses = len(traded) - wins
    total_pnl = sum(r["pnl"] for r in traded)
    avg_pnl = total_pnl / len(traded) if traded else 0
    win_rate = wins / len(traded) * 100 if traded else 0
    signal_rate = len(traded) / max(len(results), 1) * 100
    total_flips = sum(r.get("flips", 0) for r in traded)

    initial_capital = 3000
    ret_pct = total_pnl / initial_capital * 100

    # Best/worst
    best = max((r["pnl"] for r in traded), default=0)
    worst = min((r["pnl"] for r in traded), default=0)

    print(f"\n  {name}")
    print(f"    Signal rate:    {len(traded)}/{len(results)} markets ({signal_rate:.0f}%)")
    if traded:
        print(f"    Accuracy:       {wins}/{len(traded)} = {win_rate:.1f}%  (wins: {wins}, losses: {losses})")
        print(f"    Total P&L:      ${total_pnl:+.2f}")
        print(f"    Avg per trade:  ${avg_pnl:+.2f}")
        print(f"    Best / Worst:   ${best:+.2f} / ${worst:+.2f}")
        print(f"    Return on $3k:  {ret_pct:+.1f}%")
        print(f"    Position flips: {total_flips}")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--markets", type=int, default=500, help="Number of markets to fetch")
    parser.add_argument("--refresh", action="store_true", help="Force fresh data fetch")
    parser.add_argument("--top-smart", type=int, default=50, help="Number of smart wallets")
    parser.add_argument("--top-leaders", type=int, default=10, help="Number of leader wallets")
    parser.add_argument("--position-size", type=float, default=60.0, help="Size in USD per trade")
    parser.add_argument("--train-fraction", type=float, default=0.7, help="Fraction of markets for train set")
    parser.add_argument("--only-a-j", action="store_true", help="Run only Config A and J (faster output)")
    args = parser.parse_args()

    print("=" * 88)
    print("  BTC 5-MIN ENSEMBLE VOTING BACKTEST")
    print("=" * 88)

    print("\nStep 1: Loading data...")
    markets, trades_by_market = await load_data(args.markets, force_refresh=args.refresh)
    print(f"  Loaded {len(markets)} markets, {sum(len(v) for v in trades_by_market.values()):,} trades")

    # Chronological split
    sorted_markets = sorted(markets, key=lambda m: _parse_end_ts(m))
    train_cutoff = int(len(sorted_markets) * args.train_fraction)
    train_markets = sorted_markets[:train_cutoff]
    test_markets = sorted_markets[train_cutoff:]

    def _day(m):
        ts = _parse_end_ts(m)
        return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "?"

    print(f"  Train set: {len(train_markets)} markets  ({_day(train_markets[0])} -> {_day(train_markets[-1])})")
    print(f"  Test  set: {len(test_markets)} markets  ({_day(test_markets[0])} -> {_day(test_markets[-1])})")

    print("\nStep 2: Identifying smart wallets from train set...")
    smart_wallets = compute_smart_wallets(train_markets, trades_by_market, top_n=args.top_smart)
    print(f"  Top {len(smart_wallets)} profitable wallets identified")

    # Control groups
    random_50_wallets = set(compute_random_active_wallets(train_markets, trades_by_market, n=50))
    unprofitable_50_wallets = set(
        compute_unprofitable_wallets(train_markets, trades_by_market, top_n=50)
    )
    print(f"  Control: {len(random_50_wallets)} random active wallets")
    print(f"  Control: {len(unprofitable_50_wallets)} worst-performing wallets")

    print("\nStep 3: Identifying leaders (earliest movers)...")
    leaders = identify_leaders(
        smart_wallets, train_markets, trades_by_market, top_k=args.top_leaders
    )
    print(f"  Top {len(leaders)} leaders by median first-trade offset:")
    for i, (w, med, count) in enumerate(leaders, 1):
        print(f"    {i:>2}. {w[:10]}...  median={med:>5.0f}s  markets={count}")

    leader_set = {w for w, _, _ in leaders}
    smart_set = set(smart_wallets)

    print("\nStep 4: Running backtest configurations on TEST set...")
    print("=" * 88)

    configs = [
        BacktestConfig(
            name="A) Baseline — all 50 smart wallets, no filters",
            leader_wallets=smart_set,
            min_signal_strength=5,
            signal_dominance=2.0,
            apply_drift_filter=False,
            apply_time_filter=False,
            apply_sustained=False,
            position_size_usd=args.position_size,
        ),
        BacktestConfig(
            name="B) Leaders only (top 10) — no filters",
            leader_wallets=leader_set,
            min_signal_strength=3,
            signal_dominance=2.0,
            apply_drift_filter=False,
            apply_time_filter=False,
            apply_sustained=False,
            position_size_usd=args.position_size,
        ),
        BacktestConfig(
            name="C) Leaders + drift filter (0.04)",
            leader_wallets=leader_set,
            min_signal_strength=3,
            signal_dominance=2.0,
            drift_threshold=0.04,
            apply_drift_filter=True,
            apply_time_filter=False,
            apply_sustained=False,
            position_size_usd=args.position_size,
        ),
        BacktestConfig(
            name="D) Leaders + drift + time filter (30-240s)",
            leader_wallets=leader_set,
            min_signal_strength=3,
            signal_dominance=2.0,
            drift_threshold=0.04,
            window_start_sec=30,
            window_end_sec=240,
            apply_drift_filter=True,
            apply_time_filter=True,
            apply_sustained=False,
            position_size_usd=args.position_size,
        ),
        BacktestConfig(
            name="E) Leaders + drift + time + sustained (2 windows)",
            leader_wallets=leader_set,
            min_signal_strength=3,
            signal_dominance=2.0,
            drift_threshold=0.04,
            window_start_sec=30,
            window_end_sec=240,
            sustained_windows=2,
            apply_drift_filter=True,
            apply_time_filter=True,
            apply_sustained=True,
            position_size_usd=args.position_size,
        ),
        BacktestConfig(
            name="F) Tight — leaders + drift 0.02 + time + sustained + min 4 votes",
            leader_wallets=leader_set,
            min_signal_strength=4,
            signal_dominance=2.0,
            drift_threshold=0.02,
            window_start_sec=30,
            window_end_sec=210,
            sustained_windows=2,
            apply_drift_filter=True,
            apply_time_filter=True,
            apply_sustained=True,
            position_size_usd=args.position_size,
        ),
        BacktestConfig(
            name="G) All 50 smart wallets — enter once, no flipping",
            leader_wallets=smart_set,
            min_signal_strength=5,
            signal_dominance=2.0,
            apply_drift_filter=False,
            apply_time_filter=False,
            apply_sustained=False,
            allow_flips=False,  # key change
            position_size_usd=args.position_size,
        ),
        BacktestConfig(
            name="H) Leaders + INVERSE drift (only take strong trending signals)",
            leader_wallets=leader_set,
            min_signal_strength=3,
            signal_dominance=2.0,
            drift_threshold=0.04,
            apply_drift_filter=True,
            invert_drift=True,  # key change
            apply_time_filter=False,
            apply_sustained=False,
            position_size_usd=args.position_size,
        ),
        BacktestConfig(
            name="I) All 50 smart wallets — no flipping + time filter",
            leader_wallets=smart_set,
            min_signal_strength=5,
            signal_dominance=2.0,
            apply_drift_filter=False,
            apply_time_filter=True,
            window_start_sec=30,
            window_end_sec=180,
            apply_sustained=False,
            allow_flips=False,
            position_size_usd=args.position_size,
        ),
        BacktestConfig(
            name="J) All 50 smart — min 7 votes, no filters, allow flips",
            leader_wallets=smart_set,
            min_signal_strength=7,
            signal_dominance=2.0,
            apply_drift_filter=False,
            apply_time_filter=False,
            apply_sustained=False,
            allow_flips=True,
            position_size_usd=args.position_size,
        ),
        BacktestConfig(
            name="K) All 50 smart — min 10 votes (stricter), no filters, flips",
            leader_wallets=smart_set,
            min_signal_strength=10,
            signal_dominance=2.0,
            apply_drift_filter=False,
            apply_time_filter=False,
            apply_sustained=False,
            allow_flips=True,
            position_size_usd=args.position_size,
        ),
        BacktestConfig(
            name="L) All 50 smart — min 15 votes (very strict), flips",
            leader_wallets=smart_set,
            min_signal_strength=15,
            signal_dominance=2.0,
            apply_drift_filter=False,
            apply_time_filter=False,
            apply_sustained=False,
            allow_flips=True,
            position_size_usd=args.position_size,
        ),
        BacktestConfig(
            name="J+latency 1s) Config J with 1-second execution delay",
            leader_wallets=smart_set,
            min_signal_strength=7,
            signal_dominance=2.0,
            apply_drift_filter=False,
            apply_time_filter=False,
            apply_sustained=False,
            allow_flips=True,
            latency_buckets=0,  # 10s buckets, 0 extra = ~5s avg latency
            position_size_usd=args.position_size,
        ),
        BacktestConfig(
            name="J+latency 2s) Config J with 2-second execution delay",
            leader_wallets=smart_set,
            min_signal_strength=7,
            signal_dominance=2.0,
            apply_drift_filter=False,
            apply_time_filter=False,
            apply_sustained=False,
            allow_flips=True,
            latency_buckets=0,
            bucket_sec=10,
            position_size_usd=args.position_size,
        ),
        BacktestConfig(
            name="J+latency 10s) Config J with full-bucket delay (10s)",
            leader_wallets=smart_set,
            min_signal_strength=7,
            signal_dominance=2.0,
            apply_drift_filter=False,
            apply_time_filter=False,
            apply_sustained=False,
            allow_flips=True,
            latency_buckets=1,  # 1 extra bucket = 10s delay
            position_size_usd=args.position_size,
        ),
        BacktestConfig(
            name="CTRL-RAND) 50 RANDOM wallets (selection-bias control)",
            leader_wallets=random_50_wallets,
            min_signal_strength=7,
            signal_dominance=2.0,
            apply_drift_filter=False,
            apply_time_filter=False,
            apply_sustained=False,
            allow_flips=True,
            position_size_usd=args.position_size,
        ),
        BacktestConfig(
            name="CTRL-UNPROF) 50 UNPROFITABLE wallets (selection-bias control)",
            leader_wallets=unprofitable_50_wallets,
            min_signal_strength=7,
            signal_dominance=2.0,
            apply_drift_filter=False,
            apply_time_filter=False,
            apply_sustained=False,
            allow_flips=True,
            position_size_usd=args.position_size,
        ),
    ]

    if args.only_a_j:
        # Include Config J, latency variants, and control groups
        keep = ("A)", "J)", "J+", "CTRL")
        configs = [c for c in configs if any(c.name.startswith(k) for k in keep)]

    for cfg in configs:
        results = []
        for m in test_markets:
            r = simulate_market(m, trades_by_market.get(m["condition_id"], []), cfg)
            if r is not None:
                results.append(r)
        summarize(cfg.name, test_markets, results, cfg)

    print("\n" + "=" * 88)
    print("  REFERENCE — Individual best trader in test set")
    print("=" * 88)

    # Compare against single-trader copy (best leader)
    best_leader = leaders[0][0] if leaders else None
    if best_leader:
        # Compute their P&L in test set
        test_pnl = 0.0
        test_trades = 0
        for m in test_markets:
            if not m.get("resolved") or m.get("winning_index") is None:
                continue
            winning_idx = m["winning_index"]
            cid = m["condition_id"]
            for t in trades_by_market.get(cid, []):
                if t.get("proxyWallet") != best_leader:
                    continue
                test_trades += 1
                size = float(t.get("size") or 0)
                price = float(t.get("price") or 0)
                side = (t.get("side") or "BUY").upper()
                outcome_idx = t.get("outcomeIndex") or 0
                is_winning = outcome_idx == winning_idx
                if side == "BUY":
                    if is_winning:
                        test_pnl += size * (1.0 - price)
                    else:
                        test_pnl -= size * price
                else:  # SELL
                    if is_winning:
                        test_pnl -= size * (1.0 - price)
                    else:
                        test_pnl += size * price
        print(f"\n  Best leader: {best_leader}")
        print(f"  Their test set P&L (unscaled): ${test_pnl:+.2f} across {test_trades} trades")

    print("\n" + "=" * 88)


if __name__ == "__main__":
    asyncio.run(main())
