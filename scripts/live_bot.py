#!/usr/bin/env python3
"""
Live trading bot (paper mode) — WebSocket + Confusion Detector + Smart Wallets.

Architecture:
- HYBRID approach: WebSocket for fast price signals + HTTP polling every 5s
  for wallet enrichment
- Loads smart wallets from data/smart_wallets_latest.json (refreshed daily)
- Uses ConfusionDetector to pause during regime changes
- Runs Config J voting logic on smart wallet trades

Usage:
    # First refresh the smart wallet pool:
    python scripts/refresh_smart_wallets.py

    # Then run the live bot:
    python scripts/live_bot.py --duration-min 30

NO REAL MONEY is traded. This logs what the strategy WOULD have done.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from polymarket.analyzer.confusion_detector import (
    ConfusionDetector,
    MarketOutcome,
)
from polymarket.clients.clob_websocket import MarketWebSocketClient
from polymarket.clients.data_api import DataAPIClient
from polymarket.clients.gamma import GammaClient
from polymarket.collector.btc_5min_discovery import _extract_market_info

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
SMART_WALLETS_FILE = REPO_ROOT / "data" / "smart_wallets_latest.json"
FIVE_MIN = 300
POLL_INTERVAL = 5  # HTTP polling interval for wallet enrichment


def load_smart_wallets() -> set[str]:
    """Load smart wallets from the latest refresh file."""
    if not SMART_WALLETS_FILE.exists():
        raise RuntimeError(
            f"Smart wallets file not found: {SMART_WALLETS_FILE}\n"
            f"Run: python scripts/refresh_smart_wallets.py"
        )
    with open(SMART_WALLETS_FILE, "r") as f:
        data = json.load(f)
    refreshed = data.get("refreshed_at", "unknown")
    wallets = {w["wallet"] for w in data.get("wallets", [])}
    print(f"Loaded {len(wallets)} smart wallets (refreshed {refreshed[:19]})")
    return wallets


def _fmt_ts(ts: int | None) -> str:
    if not ts:
        return "?"
    return datetime.utcfromtimestamp(ts).strftime("%H:%M:%S")


@dataclass
class MarketTradingState:
    """State for paper-trading one BTC 5-min market."""

    slug: str
    condition_id: str
    token_ids: list[str]  # [Up_token, Down_token]
    start_ts: int
    end_ts: int
    smart_wallets: set[str]

    # Strategy params (Config J)
    min_signal_strength: int = 7
    signal_dominance: float = 2.0
    position_size_usd: float = 60.0
    fee_pct: float = 0.02

    # WebSocket state
    ws_trade_events: list[dict] = field(default_factory=list)
    ws_latest_price_up: float = 0.5
    ws_events_count: int = 0

    # HTTP poll state
    seen_tx_hashes: set[str] = field(default_factory=set)
    http_trades: list[dict] = field(default_factory=list)
    last_poll_ts: float = 0.0

    # Position state
    position: tuple[str, float, float, float] | None = None  # side, entry_up_price, size, cost
    realized_pnl: float = 0.0
    actions: list[dict] = field(default_factory=list)

    # Voting state
    buckets_processed: set[int] = field(default_factory=set)

    # Resolution
    winning_index: int | None = None
    final_pnl: float = 0.0


def up_price_from_trade(t: dict) -> float:
    """Return implied UP-token price from a trade dict (HTTP or WS format)."""
    try:
        price = float(t.get("price", 0.5) or 0.5)
    except (TypeError, ValueError):
        return 0.5
    outcome_idx = t.get("outcomeIndex")
    if outcome_idx is None:
        # WebSocket format doesn't have outcomeIndex; infer from asset_id
        return price
    return price if outcome_idx == 0 else 1.0 - price


async def fetch_current_market(gamma: GammaClient, min_remaining: int = 60) -> dict | None:
    """Get the current BTC 5-min market (prefer current window if it has time left)."""
    now = int(time.time())
    current_boundary = (now // FIVE_MIN) * FIVE_MIN
    current_end = current_boundary + FIVE_MIN

    target_boundary = (
        current_boundary
        if current_end - now >= min_remaining
        else current_boundary + FIVE_MIN
    )
    slug = f"btc-updown-5m-{target_boundary}"

    try:
        data = await gamma.get("/events", {"slug": slug})
        if isinstance(data, list) and data:
            info = _extract_market_info(data[0])
            if not info:
                return None
            info["_slug_ts"] = target_boundary

            # Extract token IDs from the market object
            market = data[0].get("markets", [{}])[0]
            tok_str = market.get("clobTokenIds", "[]")
            if isinstance(tok_str, str):
                try:
                    info["token_ids"] = json.loads(tok_str)
                except json.JSONDecodeError:
                    info["token_ids"] = []
            else:
                info["token_ids"] = tok_str or []
            return info
    except Exception as e:
        logger.warning(f"fetch_current_market failed: {e}")
    return None


async def poll_http_trades(
    data_api: DataAPIClient,
    state: MarketTradingState,
) -> int:
    """Fetch trades via HTTP and merge into state. Returns count of new trades."""
    new_count = 0
    try:
        # Paginate to get history
        for page in range(5):
            batch = await data_api.get_trades(
                market=state.condition_id, limit=500, offset=page * 500
            )
            if not batch:
                break
            for t in batch:
                tx = t.get("transactionHash")
                if tx and tx not in state.seen_tx_hashes:
                    state.seen_tx_hashes.add(tx)
                    state.http_trades.append(t)
                    new_count += 1
            if len(batch) < 500:
                break
    except Exception as e:
        logger.warning(f"HTTP poll failed: {e}")
    state.last_poll_ts = time.time()
    return new_count


def process_voting(state: MarketTradingState, now_ts: int):
    """Run Config J voting on NEW buckets only, using CURRENT market price.

    Critical fix: only processes buckets that are NEW (not already seen).
    Entry price always uses the most recent trade's up-price across ALL
    trades (what we'd realistically fill at right now), not the historical
    bucket's avg price.

    This is the correct semantics for real-time operation: we see a bucket
    once, decide based on its vote count, enter at the CURRENT price (not
    the bucket's historical price), and never re-process it.
    """
    buckets: dict[int, list[dict]] = defaultdict(list)
    for t in state.http_trades:
        ts = int(t.get("timestamp") or 0)
        offset = ts - state.start_ts
        if 0 <= offset <= 300:
            bucket_idx = int(offset // 10)
            buckets[bucket_idx].append(t)

    # Update the "current market price" = most recent trade's up-price
    # (from any bucket). This is what we'd pay if filling right now.
    all_trades_sorted = sorted(
        state.http_trades, key=lambda t: int(t.get("timestamp") or 0)
    )
    if all_trades_sorted:
        state.ws_latest_price_up = up_price_from_trade(all_trades_sorted[-1])

    current_real_bucket = max(0, (now_ts - state.start_ts) // 10)

    for bi in sorted(buckets.keys()):
        # Skip buckets already processed (avoid re-firing signals)
        if bi in state.buckets_processed:
            continue

        # Skip future buckets (shouldn't happen but safety)
        if bi > current_real_bucket:
            break

        state.buckets_processed.add(bi)

        bucket = buckets[bi]
        smart_trades = [
            t for t in bucket if t.get("proxyWallet") in state.smart_wallets
        ]
        yes_votes = [
            t
            for t in smart_trades
            if (t.get("side") or "BUY").upper() == "BUY"
            and (t.get("outcomeIndex") or 0) == 0
        ]
        no_votes = [
            t
            for t in smart_trades
            if (t.get("side") or "BUY").upper() == "BUY"
            and (t.get("outcomeIndex") or 0) == 1
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

        # Use CURRENT market price for entry, not historical bucket price
        current_up = state.ws_latest_price_up
        our_entry_price = current_up if signal == "YES" else 1.0 - current_up
        if our_entry_price < 0.05 or our_entry_price > 0.95:
            continue

        if state.position is None:
            size = state.position_size_usd / our_entry_price
            cost = state.position_size_usd * (1 + state.fee_pct)
            state.position = (signal, our_entry_price, size, cost)
            state.actions.append(
                {
                    "bucket": bi,
                    "action": "ENTER",
                    "side": signal,
                    "price": round(our_entry_price, 4),
                    "yes_votes": len(yes_votes),
                    "no_votes": len(no_votes),
                }
            )
        elif state.position[0] != signal:
            old_side, old_entry, old_size, old_cost = state.position
            old_current = current_up if old_side == "YES" else 1.0 - current_up
            proceeds = old_size * old_current * (1 - state.fee_pct)
            state.realized_pnl += proceeds - old_cost
            new_size = state.position_size_usd / our_entry_price
            new_cost = state.position_size_usd * (1 + state.fee_pct)
            state.position = (signal, our_entry_price, new_size, new_cost)
            state.actions.append(
                {
                    "bucket": bi,
                    "action": "FLIP",
                    "side": signal,
                    "price": round(our_entry_price, 4),
                    "yes_votes": len(yes_votes),
                    "no_votes": len(no_votes),
                }
            )


def summarize_market(state: MarketTradingState) -> dict:
    """Compute final P&L given resolution and return summary dict.

    Always returns the position state so retroactive resolution fetches can
    recompute P&L once the market resolves.
    """
    result = {
        "slug": state.slug,
        "condition_id": state.condition_id,
        "actions": len(state.actions),
        "action_log": list(state.actions),
        "realized_pnl_flips": round(state.realized_pnl, 2),
    }

    if state.position is None:
        # No signal fired — flat
        result.update({
            "pnl": 0.0,
            "position": None,
            "winning_idx": state.winning_index,
        })
        return result

    side, entry, size, cost = state.position
    pos_idx = 0 if side == "YES" else 1

    result.update({
        "position": side,
        "entry_price": round(entry, 4),
        "size": round(size, 2),
        "cost_basis": round(cost, 2),
    })

    if state.winning_index is None:
        # Resolution not yet known — P&L is unrealized (use realized flips for now)
        result["pnl"] = round(state.realized_pnl, 2)
        result["winning_idx"] = None
    else:
        settlement = size if pos_idx == state.winning_index else 0.0
        final = state.realized_pnl + (settlement - cost)
        result["pnl"] = round(final, 2)
        result["winning_idx"] = state.winning_index

    return result


async def trade_one_market(
    gamma: GammaClient,
    data_api: DataAPIClient,
    ws: MarketWebSocketClient,
    market_info: dict,
    smart_wallets: set[str],
    confusion_detector: ConfusionDetector,
) -> dict:
    """Paper-trade one BTC 5-min market via hybrid WebSocket + HTTP."""
    slug_ts = market_info["_slug_ts"]
    condition_id = market_info["condition_id"]
    token_ids = market_info.get("token_ids") or []

    state = MarketTradingState(
        slug=market_info.get("slug", ""),
        condition_id=condition_id,
        token_ids=token_ids,
        start_ts=slug_ts,
        end_ts=slug_ts + FIVE_MIN,
        smart_wallets=smart_wallets,
    )

    # Check confusion detector
    should_pause, pause_reason = confusion_detector.should_pause()
    if should_pause:
        print(f"\n  [SKIP] {state.slug}: {pause_reason}")
        return {"slug": state.slug, "pnl": 0.0, "action": "PAUSED", "reason": pause_reason}

    print(f"\n  [Market] {state.slug}")
    print(f"  Window: {_fmt_ts(state.start_ts)} -> {_fmt_ts(state.end_ts)} UTC")
    print(f"  Confusion status: {confusion_detector.status()}")

    # Resubscribe to only this market's tokens (flush old subs)
    if token_ids:
        try:
            await ws.resubscribe(token_ids)
        except Exception as e:
            logger.warning(f"WS resubscribe failed: {e}")

    # Main trading loop
    btc_prices: deque[float] = deque(maxlen=20)
    last_http_poll = 0.0
    last_status_print = 0

    while True:
        now = int(time.time())
        if now >= state.end_ts + 3:
            break
        if now < state.start_ts:
            await asyncio.sleep(0.5)
            continue

        # HTTP poll every POLL_INTERVAL seconds for wallet enrichment
        if now - last_http_poll >= POLL_INTERVAL:
            new_trades = await poll_http_trades(data_api, state)
            last_http_poll = now
            if new_trades > 0:
                process_voting(state, now)

        # Print status every 10 seconds
        if now - last_status_print >= 10:
            last_status_print = now
            smart_count = sum(
                1 for t in state.http_trades if t.get("proxyWallet") in state.smart_wallets
            )
            pos_str = "FLAT"
            if state.position:
                side, entry, size, _ = state.position
                pos_str = f"{side}@{entry:.3f}"
            offset = now - state.start_ts
            print(
                f"    [{_fmt_ts(now)}] t+{offset:3d}s "
                f"ws_events={state.ws_events_count:4d}  "
                f"http_trades={len(state.http_trades):4d}  "
                f"smart={smart_count:3d}  "
                f"up_price={state.ws_latest_price_up:.3f}  "
                f"pos={pos_str}  "
                f"rPnL=${state.realized_pnl:+.2f}"
            )

        await asyncio.sleep(0.5)

    # Market closed — try to fetch resolution
    # UMA resolution typically takes 20-60 seconds after close
    # Do a background retry so the main loop can proceed to next market
    print(f"  Market closed. Resolution will be fetched in background...")
    # Return immediately with winning_index=None; the final report
    # will do a retroactive resolution fetch for all markets.

    summary = summarize_market(state)
    print(
        f"  Result: winning_idx={summary.get('winning_idx')}  "
        f"P&L=${summary['pnl']:+.2f}  "
        f"actions={summary['actions']}"
    )
    for a in state.actions:
        print(f"    b{a['bucket']:3d}: {a['action']:5s} {a['side']} @ {a['price']}")

    # Record outcome for confusion detector
    had_signal = state.position is not None
    was_correct: bool | None = None
    if had_signal and state.winning_index is not None:
        pos_idx = 0 if state.position[0] == "YES" else 1
        was_correct = pos_idx == state.winning_index

    # Compute vote metadata from last bucket activity
    yes_count = sum(
        1 for t in state.http_trades
        if t.get("proxyWallet") in state.smart_wallets
        and (t.get("side") or "BUY").upper() == "BUY"
        and (t.get("outcomeIndex") or 0) == 0
    )
    no_count = sum(
        1 for t in state.http_trades
        if t.get("proxyWallet") in state.smart_wallets
        and (t.get("side") or "BUY").upper() == "BUY"
        and (t.get("outcomeIndex") or 0) == 1
    )
    was_tied = had_signal is False and yes_count >= 3 and no_count >= 3 and abs(yes_count - no_count) <= 2

    confusion_detector.record_market(
        MarketOutcome(
            timestamp=state.start_ts,
            had_signal=had_signal,
            was_correct=was_correct,
            signal_strength=max(yes_count, no_count),
            was_tied=was_tied,
            yes_votes=yes_count,
            no_votes=no_count,
            btc_vol_estimate=0.0,
        )
    )

    return summary


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration-min", type=float, default=30)
    args = parser.parse_args()

    print("=" * 90)
    print("  LIVE TRADING BOT — Config J + WebSocket + Confusion Detector")
    print("=" * 90)
    print(f"  Duration:     {args.duration_min} minutes")
    print(f"  Strategy:     Config J (7+ votes, flips on)")
    print(f"  Mode:         PAPER (no real money)")
    print(f"  Data path:    WebSocket + HTTP polling hybrid")
    print("=" * 90)

    # Load smart wallets from refreshed pool
    print("\n[1/3] Loading smart wallets...")
    smart_wallets = load_smart_wallets()

    # Init confusion detector
    print("\n[2/3] Initializing confusion detector...")
    confusion = ConfusionDetector(
        window=20, pause_threshold=70.0, pause_duration=5
    )
    print(f"  Window: 20 markets, pause threshold: 70, pause duration: 5 markets")

    # Init clients
    print("\n[3/3] Connecting to Polymarket APIs...")
    gamma = GammaClient()
    data_api = DataAPIClient()
    ws = MarketWebSocketClient()

    # Start WS event consumer in the background
    ws_task = None

    async def ws_consumer():
        try:
            async for _ in ws.events():
                pass  # We just keep the connection alive; data collected via HTTP
        except Exception as e:
            logger.error(f"WS consumer error: {e}")

    try:
        session_start = time.time()
        session_end = session_start + args.duration_min * 60
        results: list[dict] = []
        market_count = 0

        ws_task = asyncio.create_task(ws_consumer())

        while time.time() < session_end:
            market = await fetch_current_market(gamma, min_remaining=60)
            if not market:
                print("  No upcoming market found, retrying in 5s...")
                await asyncio.sleep(5)
                continue

            remaining = market["_slug_ts"] + FIVE_MIN - time.time()
            if remaining < 60:
                print(f"  Skipping {market.get('slug')} (only {remaining:.0f}s left)")
                await asyncio.sleep(3)
                continue

            # Check if we've already traded this market
            if results and results[-1].get("slug") == market.get("slug"):
                await asyncio.sleep(5)
                continue

            market_count += 1
            print(f"\n========== Market {market_count} ==========")
            result = await trade_one_market(
                gamma, data_api, ws, market, smart_wallets, confusion
            )
            results.append(result)

        # Retroactive resolution fetch — wait for UMA then retry any unresolved
        print("\n" + "=" * 90)
        print("  FETCHING FINAL RESOLUTIONS (waiting 30s for UMA)...")
        print("=" * 90)
        await asyncio.sleep(30)

        for r in results:
            if r.get("action") == "PAUSED":
                continue
            if r.get("winning_idx") is not None:
                continue  # already known
            slug = r.get("slug", "")
            if not slug:
                continue
            # Retry up to 5 times with 10s between
            for retry in range(5):
                try:
                    data = await gamma.get("/events", {"slug": slug})
                    if isinstance(data, list) and data:
                        info = _extract_market_info(data[0])
                        if info and info.get("winning_index") is not None:
                            r["winning_idx"] = info["winning_index"]
                            # Recompute P&L with the actual outcome
                            if "position" in r and r.get("position") and r.get("entry_price"):
                                pos_idx = 0 if r["position"] == "YES" else 1
                                size = r.get("size", 0)
                                entry = r["entry_price"]
                                # reconstruct from saved state
                                realized = r.get("realized_pnl_flips", 0.0)
                                cost = r.get("cost_basis", 0.0)
                                settlement = size if pos_idx == r["winning_idx"] else 0.0
                                r["pnl"] = round(realized + settlement - cost, 2)
                            break
                except Exception:
                    pass
                await asyncio.sleep(10)

        # Summary
        print("\n" + "=" * 90)
        print("  SESSION COMPLETE")
        print("=" * 90)
        duration = time.time() - session_start
        print(f"  Duration: {duration/60:.1f} min")
        print(f"  Markets attempted: {len(results)}")

        traded = [r for r in results if r.get("action") != "PAUSED"]
        paused = [r for r in results if r.get("action") == "PAUSED"]

        total_pnl = sum(r["pnl"] for r in traded)
        wins = sum(1 for r in traded if r["pnl"] > 0)
        losses = sum(1 for r in traded if r["pnl"] < 0)
        flats = sum(1 for r in traded if r["pnl"] == 0)
        resolved = sum(1 for r in traded if r.get("winning_idx") is not None)

        print(f"  Markets traded: {len(traded)}")
        print(f"  Markets paused: {len(paused)}")
        print(f"  Markets resolved: {resolved}/{len(traded)}")
        print(f"  Total P&L: ${total_pnl:+.2f}")
        print(f"  Wins / Losses / Flats: {wins} / {losses} / {flats}")
        print(f"\n  Confusion detector final: {confusion.status()}")

        print("\n  Per-market breakdown:")
        for r in results:
            if r.get("action") == "PAUSED":
                print(f"    PAUSED  {r['slug']}: {r.get('reason', '')}")
            else:
                marker = "WIN " if r["pnl"] > 0 else ("LOSS" if r["pnl"] < 0 else "FLAT")
                win_idx = r.get("winning_idx")
                win_str = ("UP" if win_idx == 0 else "DOWN") if win_idx is not None else "?"
                pos = r.get("position", "-")
                print(f"    {marker}  {r['slug']}: ${r['pnl']:+.2f}  pos={pos}  outcome={win_str}")

        print("=" * 90)

    finally:
        if ws_task:
            ws_task.cancel()
        await ws.close()
        await gamma.close()
        await data_api.close()


if __name__ == "__main__":
    asyncio.run(main())
