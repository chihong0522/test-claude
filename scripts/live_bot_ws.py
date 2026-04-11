#!/usr/bin/env python3
"""
Live trading bot (paper mode) — WebSocket-first with burst-triggered HTTP enrichment.

Architecture:
- WebSocket stream consumes last_trade_price events in real-time
- Tracks trade velocity to detect bursts (15+ trades in last 2s)
- HTTP poll triggered by:
    * Baseline: every 1 second
    * OR burst detection: immediately when WS sees a burst
- WebSocket provides the CURRENT market price in real-time
- HTTP provides wallet identity (needed for smart-wallet filter)

This dramatically reduces signal-to-action latency from ~5-10s (old) to ~1s (new).

Usage:
    python scripts/refresh_smart_wallets.py  # refresh wallet pool first
    python scripts/live_bot_ws.py --duration-min 30
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
from polymarket.clients.clob_websocket import MarketWebSocketClient, WSEvent
from polymarket.clients.data_api import DataAPIClient
from polymarket.clients.gamma import GammaClient
from polymarket.collector.btc_5min_discovery import _extract_market_info

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
SMART_WALLETS_FILE = REPO_ROOT / "data" / "smart_wallets_latest.json"
FIVE_MIN = 300

# Polling configuration
BASELINE_POLL_INTERVAL = 1.0  # seconds — much faster than old 5s
BURST_TRADE_THRESHOLD = 15  # trades in lookback window → trigger poll
BURST_LOOKBACK_SEC = 2.0  # how far back to count recent trades
MAIN_LOOP_INTERVAL = 0.2  # 200ms main loop tick


def load_smart_wallets() -> set[str]:
    if not SMART_WALLETS_FILE.exists():
        raise RuntimeError(
            f"Smart wallets file not found: {SMART_WALLETS_FILE}\n"
            f"Run: python scripts/refresh_smart_wallets.py"
        )
    with open(SMART_WALLETS_FILE, "r") as f:
        data = json.load(f)
    refreshed = data.get("refreshed_at", "unknown")
    version = data.get("version", 1)
    wallets = {w["wallet"] for w in data.get("wallets", [])}
    print(f"Loaded {len(wallets)} smart wallets (v{version}, refreshed {refreshed[:19]})")
    if version < 2:
        print(
            "  WARNING: wallet pool is v1 (legacy PnL-only selection). "
            "Strongly recommend: python scripts/refresh_smart_wallets.py "
            "to regenerate with signal-time accuracy + OOS validation."
        )
    return wallets


def _fmt_ts(ts: int | None) -> str:
    if not ts:
        return "?"
    return datetime.utcfromtimestamp(ts).strftime("%H:%M:%S")


def up_price_from_trade(t: dict) -> float:
    """Compute implied UP-token price from an HTTP trade dict."""
    try:
        price = float(t.get("price", 0.5) or 0.5)
    except (TypeError, ValueError):
        return 0.5
    outcome_idx = t.get("outcomeIndex")
    if outcome_idx is None:
        return price
    return price if outcome_idx == 0 else 1.0 - price


@dataclass
class MarketTradingState:
    slug: str
    condition_id: str
    token_ids: list[str]  # [Up_token, Down_token]
    up_token_id: str
    down_token_id: str
    start_ts: int
    end_ts: int
    smart_wallets: set[str]

    # Strategy params (Config N — quality-first)
    min_signal_strength: int = 7  # distinct wallets (not raw votes)
    signal_dominance: float = 2.0
    min_seconds_remaining: int = 180  # time gate: only fire with >= 3 min left
    max_bucket_age_sec: int = 30  # staleness gate: skip buckets older than this when we poll them
    position_size_usd: float = 60.0
    fee_pct: float = 0.02

    # Flip-specific gates (all must pass to flip an existing position)
    min_flip_strength: int = 5  # stricter wallet count for flips (default = min_signal_strength + 2)
    flip_cooldown_sec: int = 60  # no flip within N seconds of the last entry/flip
    min_adverse_move: float = 0.15  # price must move >= this much against us before we flip

    # WebSocket event tracking
    ws_trade_events: deque = field(default_factory=lambda: deque(maxlen=500))
    ws_events_count: int = 0
    ws_latest_up_price: float = 0.5  # updated real-time from WS
    last_burst_trigger_ts: float = 0.0

    # HTTP state
    seen_tx_hashes: set[str] = field(default_factory=set)
    http_trades: list[dict] = field(default_factory=list)
    last_http_poll_ts: float = 0.0
    http_poll_count: int = 0
    burst_triggered_polls: int = 0

    # Position
    position: tuple[str, float, float, float] | None = None  # side, entry, size, cost
    realized_pnl: float = 0.0
    actions: list[dict] = field(default_factory=list)
    buckets_processed: set[int] = field(default_factory=set)
    last_position_change_ts: float = 0.0  # unix ts of last ENTER/FLIP (for cooldown)
    entry_ws_price: float = 0.0  # ws_latest_up_price captured at entry/flip time

    # Resolution
    winning_index: int | None = None


def update_ws_price(state: MarketTradingState, ev: WSEvent):
    """Update current up-price from a WebSocket event."""
    if not ev.asset_id:
        return
    if ev.event_type in ("last_trade_price", "price_change"):
        if ev.price <= 0:
            return
        if ev.asset_id == state.up_token_id:
            state.ws_latest_up_price = ev.price
        elif ev.asset_id == state.down_token_id:
            state.ws_latest_up_price = 1.0 - ev.price
    elif ev.event_type == "book":
        # Midpoint of bid/ask
        if ev.best_bid > 0 and ev.best_ask > 0:
            mid = (ev.best_bid + ev.best_ask) / 2
            if ev.asset_id == state.up_token_id:
                state.ws_latest_up_price = mid
            elif ev.asset_id == state.down_token_id:
                state.ws_latest_up_price = 1.0 - mid
    elif ev.event_type == "best_bid_ask":
        if ev.best_bid > 0 and ev.best_ask > 0:
            mid = (ev.best_bid + ev.best_ask) / 2
            if ev.asset_id == state.up_token_id:
                state.ws_latest_up_price = mid
            elif ev.asset_id == state.down_token_id:
                state.ws_latest_up_price = 1.0 - mid


def record_ws_trade_event(state: MarketTradingState, ev: WSEvent) -> bool:
    """Record a trade event and return True if a burst was just detected."""
    if ev.event_type != "last_trade_price" or ev.size <= 0:
        return False
    now = time.time()
    state.ws_trade_events.append(now)
    state.ws_events_count += 1

    # Count recent events
    lookback_start = now - BURST_LOOKBACK_SEC
    recent_count = sum(1 for t in state.ws_trade_events if t >= lookback_start)

    # Burst detection (with cooldown to avoid repeated triggers)
    if recent_count >= BURST_TRADE_THRESHOLD and now - state.last_burst_trigger_ts > 1.0:
        state.last_burst_trigger_ts = now
        return True
    return False


async def poll_http_trades(
    data_api: DataAPIClient,
    state: MarketTradingState,
    triggered_by_burst: bool = False,
) -> int:
    """Fetch trades via HTTP, dedupe, and append. Returns count of new trades."""
    new_count = 0
    try:
        # Paginate up to 5 pages
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
        logger.debug(f"HTTP poll failed: {e}")
    state.last_http_poll_ts = time.time()
    state.http_poll_count += 1
    if triggered_by_burst:
        state.burst_triggered_polls += 1
    return new_count


def process_voting(state: MarketTradingState, now_ts: int):
    """Voting logic: process NEW buckets only, enter at CURRENT WS price.

    Upgraded per Config N + post-10-cycle optimizations:
      - Counts DISTINCT wallets, not raw votes (kills "3 bots firing 7 trades" loophole)
      - Time gate: signals with < min_seconds_remaining in the window are rejected
        (empirically, signals in the last 180s of a 5-min window are ~50% noise)
      - Staleness gate: skip buckets whose bucket_start_ts is older than
        max_bucket_age_sec — prevents the "late-join replay trade" bug where
        the bot enters on a bucket-2 signal several minutes after it fired, at
        a much worse current price.
      - Flip gates: flipping an existing position requires (a) a stricter
        distinct-wallet count than entry, (b) a cooldown since the last
        entry/flip, and (c) the WS mid has moved min_adverse_move against
        the current position. Without these, 3-wallet consensus flips ate
        the 4% round-trip fee in the first live test.
    """
    buckets: dict[int, list[dict]] = defaultdict(list)
    for t in state.http_trades:
        ts = int(t.get("timestamp") or 0)
        offset = ts - state.start_ts
        if 0 <= offset <= 300:
            bucket_idx = int(offset // 10)
            buckets[bucket_idx].append(t)

    current_real_bucket = max(0, (now_ts - state.start_ts) // 10)

    for bi in sorted(buckets.keys()):
        if bi in state.buckets_processed:
            continue
        if bi > current_real_bucket:
            break
        state.buckets_processed.add(bi)

        # Time gate — reject late-window buckets (high-noise region)
        bucket_start_ts = state.start_ts + bi * 10
        seconds_remaining = state.end_ts - bucket_start_ts
        if seconds_remaining < state.min_seconds_remaining:
            continue

        # Staleness gate — reject buckets that already fired their signal
        # long ago (bot joined the market late). The bot processes historical
        # trades for the full 5-min window each poll, so without this guard
        # it will "replay" bucket-2 signals at t+180s at the CURRENT price,
        # which is materially worse than when the smart wallets actually
        # voted. See cycles 1 and 7 of the first post-fix test.
        bucket_age = now_ts - (bucket_start_ts + 10)  # use bucket END, not start
        if bucket_age > state.max_bucket_age_sec:
            continue

        bucket = buckets[bi]
        smart_trades = [
            t for t in bucket if t.get("proxyWallet") in state.smart_wallets
        ]
        yes_trades = [
            t
            for t in smart_trades
            if (t.get("side") or "BUY").upper() == "BUY"
            and (t.get("outcomeIndex") or 0) == 0
        ]
        no_trades = [
            t
            for t in smart_trades
            if (t.get("side") or "BUY").upper() == "BUY"
            and (t.get("outcomeIndex") or 0) == 1
        ]
        yes_wallets = {t.get("proxyWallet") for t in yes_trades if t.get("proxyWallet")}
        no_wallets = {t.get("proxyWallet") for t in no_trades if t.get("proxyWallet")}
        yes_count = len(yes_wallets)
        no_count = len(no_wallets)

        signal = None
        if (
            yes_count >= state.min_signal_strength
            and yes_count >= state.signal_dominance * max(no_count, 1)
        ):
            signal = "YES"
        elif (
            no_count >= state.min_signal_strength
            and no_count >= state.signal_dominance * max(yes_count, 1)
        ):
            signal = "NO"

        if signal is None:
            continue

        # USE WEBSOCKET REAL-TIME PRICE, not historical bucket price
        current_up = state.ws_latest_up_price
        our_entry_price = current_up if signal == "YES" else 1.0 - current_up
        if our_entry_price < 0.05 or our_entry_price > 0.95:
            continue

        if state.position is None:
            size = state.position_size_usd / our_entry_price
            cost = state.position_size_usd * (1 + state.fee_pct)
            state.position = (signal, our_entry_price, size, cost)
            state.last_position_change_ts = now_ts
            state.entry_ws_price = current_up
            state.actions.append(
                {
                    "bucket": bi,
                    "action": "ENTER",
                    "side": signal,
                    "price": round(our_entry_price, 4),
                    "yes_wallets": yes_count,
                    "no_wallets": no_count,
                    "remaining_s": seconds_remaining,
                    "ts": now_ts,
                }
            )
        elif state.position[0] != signal:
            # Flip gates — all three must pass
            count_for_signal = yes_count if signal == "YES" else no_count
            if count_for_signal < state.min_flip_strength:
                state.actions.append(
                    {
                        "bucket": bi,
                        "action": "SKIP_FLIP",
                        "reason": f"strength {count_for_signal}<{state.min_flip_strength}",
                        "side": signal,
                        "ts": now_ts,
                    }
                )
                continue
            if now_ts - state.last_position_change_ts < state.flip_cooldown_sec:
                state.actions.append(
                    {
                        "bucket": bi,
                        "action": "SKIP_FLIP",
                        "reason": (
                            f"cooldown {now_ts - state.last_position_change_ts:.0f}s"
                            f"<{state.flip_cooldown_sec}s"
                        ),
                        "side": signal,
                        "ts": now_ts,
                    }
                )
                continue
            old_side = state.position[0]
            moving_against = (
                (old_side == "YES" and current_up < state.entry_ws_price - state.min_adverse_move)
                or (old_side == "NO" and current_up > state.entry_ws_price + state.min_adverse_move)
            )
            if not moving_against:
                state.actions.append(
                    {
                        "bucket": bi,
                        "action": "SKIP_FLIP",
                        "reason": (
                            f"no_adverse_move (entry={state.entry_ws_price:.2f}, "
                            f"now={current_up:.2f}, need>={state.min_adverse_move:.2f})"
                        ),
                        "side": signal,
                        "ts": now_ts,
                    }
                )
                continue

            # All gates passed — execute the flip
            _, old_entry, old_size, old_cost = state.position
            old_current = current_up if old_side == "YES" else 1.0 - current_up
            proceeds = old_size * old_current * (1 - state.fee_pct)
            state.realized_pnl += proceeds - old_cost
            new_size = state.position_size_usd / our_entry_price
            new_cost = state.position_size_usd * (1 + state.fee_pct)
            state.position = (signal, our_entry_price, new_size, new_cost)
            state.last_position_change_ts = now_ts
            state.entry_ws_price = current_up
            state.actions.append(
                {
                    "bucket": bi,
                    "action": "FLIP",
                    "side": signal,
                    "price": round(our_entry_price, 4),
                    "yes_wallets": yes_count,
                    "no_wallets": no_count,
                    "remaining_s": seconds_remaining,
                    "ts": now_ts,
                }
            )


def summarize_market(state: MarketTradingState) -> dict:
    result = {
        "slug": state.slug,
        "condition_id": state.condition_id,
        "actions": len(state.actions),
        "action_log": list(state.actions),
        "realized_pnl_flips": round(state.realized_pnl, 2),
        "http_poll_count": state.http_poll_count,
        "burst_triggered_polls": state.burst_triggered_polls,
        "ws_events_count": state.ws_events_count,
    }

    if state.position is None:
        result.update({"pnl": 0.0, "position": None, "winning_idx": state.winning_index})
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
        result["pnl"] = round(state.realized_pnl, 2)
        result["winning_idx"] = None
    else:
        settlement = size if pos_idx == state.winning_index else 0.0
        result["pnl"] = round(state.realized_pnl + settlement - cost, 2)
        result["winning_idx"] = state.winning_index

    return result


async def fetch_current_market(gamma: GammaClient, min_remaining: int = 60) -> dict | None:
    now = int(time.time())
    current_boundary = (now // FIVE_MIN) * FIVE_MIN
    current_end = current_boundary + FIVE_MIN
    target_boundary = (
        current_boundary if current_end - now >= min_remaining else current_boundary + FIVE_MIN
    )
    slug = f"btc-updown-5m-{target_boundary}"

    try:
        data = await gamma.get("/events", {"slug": slug})
        if isinstance(data, list) and data:
            info = _extract_market_info(data[0])
            if not info:
                return None
            info["_slug_ts"] = target_boundary
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


async def trade_one_market(
    gamma: GammaClient,
    data_api: DataAPIClient,
    ws: MarketWebSocketClient,
    market_info: dict,
    smart_wallets: set[str],
    confusion_detector: ConfusionDetector,
    min_signal_strength: int = 7,
    min_seconds_remaining: int = 180,
    max_bucket_age_sec: int = 30,
    min_flip_strength: int = 9,
    flip_cooldown_sec: int = 60,
    min_adverse_move: float = 0.15,
) -> dict:
    """Trade one market with WebSocket-driven polling."""
    slug_ts = market_info["_slug_ts"]
    condition_id = market_info["condition_id"]
    token_ids = market_info.get("token_ids") or []

    if len(token_ids) < 2:
        print(f"  [SKIP] No token IDs for {market_info.get('slug')}")
        return {"slug": market_info.get("slug"), "pnl": 0.0, "action": "NO_TOKENS"}

    state = MarketTradingState(
        slug=market_info.get("slug", ""),
        condition_id=condition_id,
        token_ids=token_ids,
        up_token_id=token_ids[0],  # first token is Up (convention)
        down_token_id=token_ids[1],
        start_ts=slug_ts,
        end_ts=slug_ts + FIVE_MIN,
        smart_wallets=smart_wallets,
        min_signal_strength=min_signal_strength,
        min_seconds_remaining=min_seconds_remaining,
        max_bucket_age_sec=max_bucket_age_sec,
        min_flip_strength=min_flip_strength,
        flip_cooldown_sec=flip_cooldown_sec,
        min_adverse_move=min_adverse_move,
    )

    # Check confusion detector
    should_pause, pause_reason = confusion_detector.should_pause()
    if should_pause:
        print(f"\n  [SKIP] {state.slug}: {pause_reason}")
        return {"slug": state.slug, "pnl": 0.0, "action": "PAUSED", "reason": pause_reason}

    print(f"\n  [Market] {state.slug}")
    print(f"  Window: {_fmt_ts(state.start_ts)} -> {_fmt_ts(state.end_ts)} UTC")
    print(f"  Confusion: {confusion_detector.status()}")

    # Resubscribe WebSocket to this market's tokens
    try:
        await ws.resubscribe(token_ids)
    except Exception as e:
        logger.warning(f"WS resubscribe failed: {e}")

    # Event-driven main loop
    burst_pending = False
    last_status_print = 0
    ws_consumer_task = None
    stop_flag = asyncio.Event()

    async def ws_consumer():
        """Background task: consume WS events and update state."""
        try:
            async for ev in ws.events():
                if stop_flag.is_set():
                    break
                update_ws_price(state, ev)
                was_burst = record_ws_trade_event(state, ev)
                if was_burst:
                    nonlocal burst_pending
                    burst_pending = True
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"WS consumer error: {e}")

    ws_consumer_task = asyncio.create_task(ws_consumer())

    try:
        while True:
            now_real = time.time()
            now_int = int(now_real)

            if now_int >= state.end_ts + 3:
                break
            if now_int < state.start_ts:
                await asyncio.sleep(0.2)
                continue

            # Decide whether to poll HTTP
            should_poll = False
            trigger_reason = ""
            time_since_poll = now_real - state.last_http_poll_ts

            if burst_pending:
                should_poll = True
                burst_pending = False
                trigger_reason = "BURST"
            elif time_since_poll >= BASELINE_POLL_INTERVAL:
                should_poll = True
                trigger_reason = "baseline"

            if should_poll:
                new_trades = await poll_http_trades(
                    data_api, state, triggered_by_burst=(trigger_reason == "BURST")
                )
                if new_trades > 0 or trigger_reason == "BURST":
                    process_voting(state, now_int)

            # Print status every 10 seconds
            if now_int - last_status_print >= 10:
                last_status_print = now_int
                smart_count = sum(
                    1 for t in state.http_trades
                    if t.get("proxyWallet") in state.smart_wallets
                )
                pos_str = "FLAT"
                if state.position:
                    side, entry, size, _ = state.position
                    pos_str = f"{side}@{entry:.3f}"
                offset = now_int - state.start_ts
                print(
                    f"    [{_fmt_ts(now_int)}] t+{offset:3d}s "
                    f"ws={state.ws_events_count:4d}  "
                    f"http={state.http_trades.__len__():4d}  "
                    f"smart={smart_count:3d}  "
                    f"up={state.ws_latest_up_price:.3f}  "
                    f"polls={state.http_poll_count}({state.burst_triggered_polls}brst)  "
                    f"pos={pos_str}  rPnL=${state.realized_pnl:+.2f}"
                )

            await asyncio.sleep(MAIN_LOOP_INTERVAL)

    finally:
        stop_flag.set()
        if ws_consumer_task:
            ws_consumer_task.cancel()
            try:
                await ws_consumer_task
            except (asyncio.CancelledError, Exception):
                pass

    print(f"  Market closed. Polls: {state.http_poll_count} (burst: {state.burst_triggered_polls})  WS events: {state.ws_events_count}")

    summary = summarize_market(state)
    for a in state.actions:
        print(
            f"    b{a['bucket']:3d}: {a['action']:5s} {a['side']} @ {a['price']}  "
            f"(wallets {a.get('yes_wallets', 0)}Y/{a.get('no_wallets', 0)}N, "
            f"remaining {a.get('remaining_s', 0)}s)"
        )

    # Record outcome for confusion detector
    had_signal = state.position is not None
    was_correct: bool | None = None
    if had_signal and state.winning_index is not None:
        pos_idx = 0 if state.position[0] == "YES" else 1
        was_correct = pos_idx == state.winning_index

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
    was_tied = (
        not had_signal and yes_count >= 3 and no_count >= 3 and abs(yes_count - no_count) <= 2
    )

    confusion_detector.record_market(
        MarketOutcome(
            timestamp=state.start_ts,
            had_signal=had_signal,
            was_correct=was_correct,
            signal_strength=max(yes_count, no_count),
            was_tied=was_tied,
            yes_votes=yes_count,
            no_votes=no_count,
        )
    )

    return summary


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration-min", type=float, default=30)
    parser.add_argument(
        "--min-signal-strength",
        type=int,
        default=4,
        help=(
            "Minimum DISTINCT wallets (not raw votes) to fire a signal. "
            "Default 4 is tuned for the 15-wallet v2 quality pool; with a "
            "legacy 50-wallet pool use 7."
        ),
    )
    parser.add_argument(
        "--min-seconds-remaining",
        type=int,
        default=180,
        help="Reject signals with < N seconds remaining in the 5-min window",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=0,
        help="Stop after N markets traded (0 = unlimited, use --duration-min only)",
    )
    parser.add_argument(
        "--max-bucket-age-sec",
        type=int,
        default=30,
        help=(
            "Staleness gate: skip buckets whose end was >N seconds ago. "
            "Prevents the bot from replaying a bucket-2 signal minutes later "
            "at a materially worse price when it joins the market late."
        ),
    )
    parser.add_argument(
        "--min-flip-strength",
        type=int,
        default=0,
        help=(
            "Distinct wallets required to FLIP an existing position "
            "(0 = auto, uses min_signal_strength + 2). Flips are expensive "
            "(4%% round-trip fee) so they should demand stronger consensus "
            "than the initial entry."
        ),
    )
    parser.add_argument(
        "--flip-cooldown-sec",
        type=int,
        default=60,
        help="Reject flips within N seconds of the last entry/flip",
    )
    parser.add_argument(
        "--min-adverse-move",
        type=float,
        default=0.15,
        help=(
            "WS mid must have moved at least this much AGAINST the current "
            "position before a flip is allowed. Prevents flipping on pure "
            "wallet-consensus noise when the market isn't disagreeing with us."
        ),
    )
    parser.add_argument(
        "--no-flips",
        action="store_true",
        help="Disable flips entirely (equivalent to --min-flip-strength 999)",
    )
    args = parser.parse_args()

    # Resolve --no-flips and auto-default for min-flip-strength
    if args.no_flips:
        args.min_flip_strength = 999
    elif args.min_flip_strength == 0:
        args.min_flip_strength = args.min_signal_strength + 2

    print("=" * 90)
    print("  LIVE TRADING BOT — WebSocket-First (Config N — quality-first)")
    print("=" * 90)
    print(f"  Duration:     {args.duration_min} minutes")
    if args.max_cycles > 0:
        print(f"  Max cycles:   {args.max_cycles} markets")
    print(f"  Min strength: {args.min_signal_strength} distinct wallets")
    print(f"  Time gate:    >= {args.min_seconds_remaining}s remaining")
    print(f"  Staleness:    skip buckets > {args.max_bucket_age_sec}s old")
    if args.no_flips:
        print(f"  Flips:        DISABLED")
    else:
        print(
            f"  Flip gates:   strength>={args.min_flip_strength}, "
            f"cooldown>={args.flip_cooldown_sec}s, adverse>={args.min_adverse_move:.2f}"
        )
    print(f"  Mode:         PAPER (no real money)")
    print(f"  Polling:      {BASELINE_POLL_INTERVAL}s baseline + burst-triggered HTTP")
    print(f"  Burst thresh: {BURST_TRADE_THRESHOLD} trades in {BURST_LOOKBACK_SEC}s")
    print(f"  Price source: WebSocket real-time (used for entry pricing)")
    print("=" * 90)

    print("\n[1/3] Loading smart wallets...")
    smart_wallets = load_smart_wallets()

    print("\n[2/3] Initializing confusion detector...")
    confusion = ConfusionDetector(window=20, pause_threshold=70.0, pause_duration=5)
    print(f"  Window: 20 markets, pause threshold: 70, pause duration: 5 markets")

    print("\n[3/3] Connecting to Polymarket APIs...")
    gamma = GammaClient()
    data_api = DataAPIClient()
    ws = MarketWebSocketClient()

    try:
        session_start = time.time()
        session_end = session_start + args.duration_min * 60
        results: list[dict] = []
        market_count = 0

        while time.time() < session_end:
            market = await fetch_current_market(gamma, min_remaining=60)
            if not market:
                await asyncio.sleep(5)
                continue

            remaining = market["_slug_ts"] + FIVE_MIN - time.time()
            if remaining < 60:
                await asyncio.sleep(3)
                continue

            if results and results[-1].get("slug") == market.get("slug"):
                await asyncio.sleep(5)
                continue

            market_count += 1
            print(f"\n========== Market {market_count} ==========", flush=True)
            result = await trade_one_market(
                gamma,
                data_api,
                ws,
                market,
                smart_wallets,
                confusion,
                min_signal_strength=args.min_signal_strength,
                min_seconds_remaining=args.min_seconds_remaining,
                max_bucket_age_sec=args.max_bucket_age_sec,
                min_flip_strength=args.min_flip_strength,
                flip_cooldown_sec=args.flip_cooldown_sec,
                min_adverse_move=args.min_adverse_move,
            )
            results.append(result)

            if args.max_cycles > 0 and market_count >= args.max_cycles:
                print(f"\n  Reached max cycles ({args.max_cycles}) — exiting loop.", flush=True)
                break

        # Retroactive resolution — Chainlink oracle typically publishes the
        # resolution 1-3 min after market close. The initial 30s wait was too
        # short; many markets ended the 10-cycle live test still "unresolved"
        # and the bot's reported P&L omitted settlement credits entirely.
        # New budget: 60s initial + 12 × 15s retries ≈ 4 minutes total per
        # unresolved market. Still bounded so a truly-stuck market doesn't
        # deadlock the session summary.
        print("\n" + "=" * 90)
        print("  FETCHING FINAL RESOLUTIONS (waiting 60s for Chainlink)...", flush=True)
        print("=" * 90)
        await asyncio.sleep(60)

        for r in results:
            if r.get("action") in ("PAUSED", "NO_TOKENS"):
                continue
            if r.get("winning_idx") is not None:
                continue
            slug = r.get("slug", "")
            if not slug:
                continue
            for retry in range(12):
                try:
                    data = await gamma.get("/events", {"slug": slug})
                    if isinstance(data, list) and data:
                        info = _extract_market_info(data[0])
                        if info and info.get("winning_index") is not None:
                            r["winning_idx"] = info["winning_index"]
                            if r.get("position") and r.get("size"):
                                pos_idx = 0 if r["position"] == "YES" else 1
                                size = r["size"]
                                realized = r.get("realized_pnl_flips", 0.0)
                                cost = r.get("cost_basis", 0.0)
                                settlement = size if pos_idx == r["winning_idx"] else 0.0
                                r["pnl"] = round(realized + settlement - cost, 2)
                            break
                except Exception:
                    pass
                await asyncio.sleep(15)

        # Summary
        print("\n" + "=" * 90)
        print("  SESSION COMPLETE")
        print("=" * 90)
        duration = time.time() - session_start
        print(f"  Duration: {duration/60:.1f} min")
        print(f"  Markets attempted: {len(results)}")

        traded = [r for r in results if r.get("action") not in ("PAUSED", "NO_TOKENS")]
        paused = [r for r in results if r.get("action") == "PAUSED"]
        total_pnl = sum(r["pnl"] for r in traded)
        wins = sum(1 for r in traded if r["pnl"] > 0)
        losses = sum(1 for r in traded if r["pnl"] < 0)
        flats = sum(1 for r in traded if r["pnl"] == 0)
        resolved = sum(1 for r in traded if r.get("winning_idx") is not None)

        total_ws_events = sum(r.get("ws_events_count", 0) for r in traded)
        total_polls = sum(r.get("http_poll_count", 0) for r in traded)
        total_burst_polls = sum(r.get("burst_triggered_polls", 0) for r in traded)

        print(f"  Markets traded: {len(traded)}")
        print(f"  Markets paused: {len(paused)}")
        print(f"  Markets resolved: {resolved}/{len(traded)}")
        print(f"  Total P&L: ${total_pnl:+.2f}")
        print(f"  Wins / Losses / Flats: {wins} / {losses} / {flats}")
        print(f"\n  Infrastructure metrics:")
        print(f"    Total WS events: {total_ws_events:,}")
        print(f"    Total HTTP polls: {total_polls} ({total_burst_polls} burst-triggered)")
        print(f"    Avg polls/market: {total_polls/max(len(traded),1):.1f}")
        print(f"    Burst-trigger rate: {total_burst_polls/max(total_polls,1)*100:.1f}%")
        print(f"\n  Confusion detector final: {confusion.status()}")

        print("\n  Per-market breakdown:")
        for r in results:
            if r.get("action") == "PAUSED":
                print(f"    PAUSED  {r['slug']}: {r.get('reason', '')}")
            elif r.get("action") == "NO_TOKENS":
                print(f"    NOTOK   {r['slug']}")
            else:
                marker = "WIN " if r["pnl"] > 0 else ("LOSS" if r["pnl"] < 0 else "FLAT")
                win_idx = r.get("winning_idx")
                win_str = ("UP" if win_idx == 0 else "DOWN") if win_idx is not None else "?"
                pos = r.get("position", "-")
                burst = r.get("burst_triggered_polls", 0)
                print(f"    {marker}  {r['slug']}: ${r['pnl']:+.2f}  pos={pos}  outcome={win_str}  bursts={burst}")

        print("=" * 90)

    finally:
        await ws.close()
        await gamma.close()
        await data_api.close()


if __name__ == "__main__":
    asyncio.run(main())
