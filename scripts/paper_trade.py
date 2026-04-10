#!/usr/bin/env python3
"""
Paper Trading — Run Config J strategy on live BTC 5-min markets.

IMPORTANT CAVEAT: The backtest showed that a 10-second execution delay turns
the strategy from +165% to -170%. This paper trader polls via HTTP which has
real-world latency of 5-15 seconds per poll cycle. Expected behavior:
the strategy will likely LOSE MONEY in this paper trading test, which
validates the latency-sensitive nature of the approach.

Purpose: Verify infrastructure works + measure real-world latency + show
the gap between theoretical edge and practical execution.

Usage:
    python scripts/paper_trade.py --duration-min 30
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
    discover_btc_5min_markets,
    collect_market_trades,
    _extract_market_info,
)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CACHE_FILE = "/tmp/btc5m_backtest_cache.pkl"
FIVE_MIN = 300
POLL_INTERVAL_SEC = 10  # Poll every 10 seconds


# ══════════════════════════════════════════════════════════════════════════
# Load smart wallets from cached backtest data
# ══════════════════════════════════════════════════════════════════════════


def load_smart_wallets_from_cache(top_n: int = 50) -> list[str]:
    """Reuse the cached 2016-market dataset to identify smart wallets."""
    if not os.path.exists(CACHE_FILE):
        raise RuntimeError(
            "No cached data found. Run ensemble_backtest.py first to populate cache."
        )

    with open(CACHE_FILE, "rb") as f:
        markets, trades_by_market = pickle.load(f)

    # Use ALL markets in the cache as training (we're not validating, just selecting)
    wallet_pnl: dict[str, float] = defaultdict(float)
    wallet_trades: dict[str, int] = defaultdict(int)

    for m in markets:
        if not m.get("resolved") or m.get("winning_index") is None:
            continue
        winning_idx = m["winning_index"]
        for t in trades_by_market.get(m["condition_id"], []):
            w = t.get("proxyWallet")
            if not w:
                continue
            wallet_trades[w] += 1
            size = float(t.get("size") or 0)
            price = float(t.get("price") or 0)
            side = (t.get("side") or "BUY").upper()
            idx = t.get("outcomeIndex") or 0
            is_winning = idx == winning_idx
            if side == "BUY":
                wallet_pnl[w] += size * (1.0 - price) if is_winning else -size * price
            else:  # SELL
                wallet_pnl[w] += -size * (1.0 - price) if is_winning else size * price

    candidates = [
        (w, pnl) for w, pnl in wallet_pnl.items() if wallet_trades[w] >= 30 and pnl > 0
    ]
    candidates.sort(key=lambda x: x[1], reverse=True)
    return [w for w, _ in candidates[:top_n]]


# ══════════════════════════════════════════════════════════════════════════
# Paper trading state
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class PaperTradeState:
    market_info: dict
    smart_wallets: set[str]
    start_ts: int  # market start (5 min before close)
    end_ts: int  # market close
    seen_tx_hashes: set[str] = field(default_factory=set)
    all_trades: list[dict] = field(default_factory=list)
    position: tuple[str, float, float, float] | None = None  # side, entry, size, cost_basis
    realized_pnl: float = 0.0
    action_log: list[dict] = field(default_factory=list)
    last_poll_latency_ms: float = 0.0
    poll_count: int = 0

    # Strategy params (Config J)
    min_signal_strength: int = 7
    signal_dominance: float = 2.0
    position_size_usd: float = 60.0
    fee_pct: float = 0.02


async def find_next_market(gamma: GammaClient, min_remaining_sec: int = 60) -> dict | None:
    """Find the best BTC 5-min market to trade right now.

    Prefer the CURRENT window if at least `min_remaining_sec` seconds remain.
    Otherwise, wait for the NEXT window.
    """
    now = int(time.time())
    current_boundary = (now // FIVE_MIN) * FIVE_MIN
    current_end = current_boundary + FIVE_MIN

    # If current window still has enough time, use it
    if current_end - now >= min_remaining_sec:
        target_boundary = current_boundary
    else:
        target_boundary = current_boundary + FIVE_MIN

    slug = f"btc-updown-5m-{target_boundary}"

    try:
        data = await gamma.get("/events", {"slug": slug})
        if isinstance(data, list) and data:
            info = _extract_market_info(data[0])
            if info:
                info["_slug_ts"] = target_boundary
                return info
    except Exception as e:
        logger.warning(f"find_next_market failed: {e}")
    return None


def _up_price(t: dict) -> float:
    price = float(t.get("price") or 0.5)
    idx = t.get("outcomeIndex") or 0
    return price if idx == 0 else 1.0 - price


def _print_status(state: PaperTradeState, event: str):
    now = int(time.time())
    elapsed = now - state.start_ts
    remaining = max(0, state.end_ts - now)
    pos_str = "FLAT"
    if state.position:
        side, entry, size, cost = state.position
        pos_str = f"{side} @ {entry:.3f} size={size:.1f}"
    print(
        f"  [{datetime.utcnow().strftime('%H:%M:%S')}] "
        f"t+{elapsed:3d}s/{state.end_ts-state.start_ts}s "
        f"trades={len(state.all_trades):4d} smart={sum(1 for t in state.all_trades if t.get('proxyWallet') in state.smart_wallets):3d} "
        f"pos={pos_str} rPnL=${state.realized_pnl:+.2f} | {event}"
    )


def process_new_buckets(state: PaperTradeState, now_ts: int) -> list[str]:
    """Apply voting logic to trades up to now_ts. Return list of events triggered."""
    events: list[str] = []

    # Bucket trades by 10-second offsets from market start
    buckets: dict[int, list[dict]] = defaultdict(list)
    for t in state.all_trades:
        ts = int(t.get("timestamp") or 0)
        offset = ts - state.start_ts
        if 0 <= offset <= 300:
            bucket_idx = int(offset // 10)
            buckets[bucket_idx].append(t)

    # Pre-compute last up-price per bucket
    last_up: dict[int, float] = {}
    running = 0.5
    max_bi = int(300 // 10)
    for bi in range(max_bi + 1):
        for t in buckets.get(bi, []):
            running = _up_price(t)
        last_up[bi] = running

    # The current bucket index based on real time
    current_real_bucket = (now_ts - state.start_ts) // 10

    # Reset position state so we can replay from scratch each poll
    state.position = None
    state.realized_pnl = 0.0
    state.action_log = []

    for bi in sorted(buckets.keys()):
        if bi > current_real_bucket:
            break  # can't act on future buckets
        bucket = buckets[bi]
        smart_trades = [t for t in bucket if t.get("proxyWallet") in state.smart_wallets]
        yes_votes = [
            t for t in smart_trades
            if (t.get("side") or "BUY").upper() == "BUY" and (t.get("outcomeIndex") or 0) == 0
        ]
        no_votes = [
            t for t in smart_trades
            if (t.get("side") or "BUY").upper() == "BUY" and (t.get("outcomeIndex") or 0) == 1
        ]

        signal = None
        if (
            len(yes_votes) >= state.min_signal_strength
            and len(yes_votes) >= state.signal_dominance * max(len(no_votes), 1)
        ):
            signal = "YES"
        elif (
            len(no_votes) >= state.min_signal_strength
            and len(no_votes) >= state.signal_dominance * max(len(yes_votes), 1)
        ):
            signal = "NO"

        if signal is None:
            continue

        up_price = last_up.get(bi, 0.5)
        our_entry_price = up_price if signal == "YES" else 1.0 - up_price
        if our_entry_price < 0.05 or our_entry_price > 0.95:
            continue

        if state.position is None:
            size = state.position_size_usd / our_entry_price
            cost = state.position_size_usd * (1 + state.fee_pct)
            state.position = (signal, our_entry_price, size, cost)
            state.action_log.append({"bucket": bi, "action": "ENTER", "side": signal, "price": our_entry_price})
        elif state.position[0] != signal:
            # Flip
            old_side, old_entry, old_size, old_cost = state.position
            old_cur = up_price if old_side == "YES" else 1.0 - up_price
            proceeds = old_size * old_cur * (1 - state.fee_pct)
            state.realized_pnl += proceeds - old_cost
            new_size = state.position_size_usd / our_entry_price
            new_cost = state.position_size_usd * (1 + state.fee_pct)
            state.position = (signal, our_entry_price, new_size, new_cost)
            state.action_log.append({"bucket": bi, "action": "FLIP", "side": signal, "price": our_entry_price})

    return events


def settle_market(state: PaperTradeState, winning_index: int | None) -> float:
    """Compute final P&L given the market's resolution."""
    if winning_index is None:
        return 0.0
    total = state.realized_pnl
    if state.position is not None:
        side, entry, size, cost = state.position
        pos_idx = 0 if side == "YES" else 1
        settlement = size if pos_idx == winning_index else 0.0
        total += settlement - cost
    return total


async def paper_trade_one_market(
    gamma: GammaClient,
    data_api: DataAPIClient,
    market_info: dict,
    smart_wallets: set[str],
) -> dict:
    """Run paper trading on one BTC 5-min market from now to close."""
    slug_ts = market_info["_slug_ts"]
    condition_id = market_info["condition_id"]
    state = PaperTradeState(
        market_info=market_info,
        smart_wallets=smart_wallets,
        start_ts=slug_ts,  # the 5-min window starts at slug timestamp
        end_ts=slug_ts + FIVE_MIN,
    )

    print(f"\n  --- Paper trading market {market_info.get('slug')} ---")
    print(f"  Condition: {condition_id[:16]}...")
    print(f"  Window: {datetime.utcfromtimestamp(state.start_ts).strftime('%H:%M:%S')} -> {datetime.utcfromtimestamp(state.end_ts).strftime('%H:%M:%S')} UTC")

    poll_latencies = []

    while True:
        now = int(time.time())
        if now >= state.end_ts + 5:
            break  # Market closed, exit

        # Skip polling before market start
        if now < state.start_ts:
            await asyncio.sleep(1)
            continue

        t0 = time.time()
        try:
            batch = await data_api.get_trades(market=condition_id, limit=500)
        except Exception as e:
            print(f"    poll error: {e}")
            await asyncio.sleep(POLL_INTERVAL_SEC)
            continue

        latency_ms = (time.time() - t0) * 1000
        poll_latencies.append(latency_ms)
        state.last_poll_latency_ms = latency_ms
        state.poll_count += 1

        # Dedupe and append
        new_count = 0
        for t in batch:
            tx = t.get("transactionHash")
            if tx and tx not in state.seen_tx_hashes:
                state.seen_tx_hashes.add(tx)
                state.all_trades.append(t)
                new_count += 1

        process_new_buckets(state, now)
        _print_status(state, f"poll +{new_count} new trades, {latency_ms:.0f}ms")

        # Sleep until next poll, but stop if market is about to close
        await asyncio.sleep(POLL_INTERVAL_SEC)

    # Market closed — fetch final resolution
    print("  Market closed, fetching resolution...")
    try:
        data = await gamma.get("/events", {"slug": market_info.get("slug")})
        if isinstance(data, list) and data:
            final_info = _extract_market_info(data[0])
            winning_idx = final_info.get("winning_index") if final_info else None
        else:
            winning_idx = None
    except Exception as e:
        print(f"    resolution fetch error: {e}")
        winning_idx = None

    final_pnl = settle_market(state, winning_idx)

    print(f"  Resolution: winning_index={winning_idx}  final P&L: ${final_pnl:+.2f}")
    print(f"  Poll count: {state.poll_count}  Avg poll latency: {statistics.mean(poll_latencies):.0f}ms" if poll_latencies else "  No polls completed")
    print(f"  Actions: {len(state.action_log)}")
    for a in state.action_log:
        print(f"    b{a['bucket']:3d}: {a['action']:5s} {a['side']} @ {a['price']:.3f}")

    return {
        "market": condition_id,
        "slug": market_info.get("slug"),
        "winning_idx": winning_idx,
        "pnl": round(final_pnl, 2),
        "actions": len(state.action_log),
        "polls": state.poll_count,
        "avg_latency_ms": statistics.mean(poll_latencies) if poll_latencies else 0,
    }


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration-min", type=float, default=30)
    parser.add_argument("--top-smart", type=int, default=50)
    args = parser.parse_args()

    print("=" * 88)
    print("  BTC 5-MIN PAPER TRADING (Config J strategy)")
    print("=" * 88)
    print(f"  Duration:         {args.duration_min} minutes")
    print(f"  Strategy:         Config J (7+ votes from top 50 smart wallets, flips on)")
    print(f"  Position size:    $60 per entry")
    print(f"  ⚠️  Expected:     LOSS (HTTP polling has ~5-10s latency)")
    print(f"  ⚠️  Backtest:     J+latency 10s showed -170% return")
    print("=" * 88)

    print("\nLoading smart wallets from cached backtest data...")
    smart_wallets_list = load_smart_wallets_from_cache(top_n=args.top_smart)
    smart_wallets = set(smart_wallets_list)
    print(f"  Loaded {len(smart_wallets)} smart wallets")

    gamma = GammaClient()
    data_api = DataAPIClient()

    try:
        session_start = time.time()
        session_end = session_start + args.duration_min * 60
        results = []
        market_count = 0

        while time.time() < session_end:
            # Find next market to trade
            market = await find_next_market(gamma)
            if not market:
                print("  No upcoming market found, retrying in 5s...")
                await asyncio.sleep(5)
                continue

            # Make sure this is one we haven't traded yet
            slug_ts = market["_slug_ts"]
            remaining = slug_ts + FIVE_MIN - time.time()
            if remaining < 60:
                print(f"  Skipping market {market.get('slug')} (only {remaining:.0f}s left)")
                await asyncio.sleep(5)
                continue

            market_count += 1
            print(f"\n[Market {market_count}] ==================================================")
            result = await paper_trade_one_market(gamma, data_api, market, smart_wallets)
            results.append(result)

            if time.time() >= session_end:
                break

        # Summary
        print("\n" + "=" * 88)
        print("  PAPER TRADING SESSION COMPLETE")
        print("=" * 88)
        duration = time.time() - session_start
        print(f"  Duration: {duration/60:.1f} minutes")
        print(f"  Markets traded: {len(results)}")

        total_pnl = sum(r["pnl"] for r in results)
        wins = sum(1 for r in results if r["pnl"] > 0)
        losses = sum(1 for r in results if r["pnl"] < 0)
        flat = sum(1 for r in results if r["pnl"] == 0)

        print(f"  Total P&L: ${total_pnl:+.2f}")
        print(f"  Wins / Losses / Flat: {wins} / {losses} / {flat}")
        if results:
            print(f"  Avg per market: ${total_pnl/len(results):+.2f}")
            all_latencies = [r["avg_latency_ms"] for r in results if r["avg_latency_ms"] > 0]
            if all_latencies:
                print(f"  Avg poll latency: {statistics.mean(all_latencies):.0f}ms")
            print(f"  Total actions: {sum(r['actions'] for r in results)}")

        print("\n  Per-market breakdown:")
        for r in results:
            w = "WIN " if r["pnl"] > 0 else ("LOSS" if r["pnl"] < 0 else "FLAT")
            print(f"    {w} {r['slug']}: ${r['pnl']:+.2f}  actions={r['actions']}  polls={r['polls']}")

        print("=" * 88)

    finally:
        await gamma.close()
        await data_api.close()


if __name__ == "__main__":
    asyncio.run(main())
