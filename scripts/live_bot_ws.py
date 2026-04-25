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

# Live-trading imports (gracefully degrade if deps missing)
_LIVE_TRADING_AVAILABLE = False
try:
    from polymarket.clients.clob_order_client import ClobOrderClient
    from polymarket.trading.position_store import LivePosition, PositionStore
    from polymarket.trading.risk_manager import RiskLimits, RiskManager
    _LIVE_TRADING_AVAILABLE = True
except ImportError:
    pass

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


def load_smart_wallets() -> dict[str, float]:
    """Load the v2 quality pool and compute a per-wallet VOTE WEIGHT.

    Weight tiers (based on OOS signal-time accuracy, which is the most
    conservative measure — held-out performance on markets the wallet
    selection pipeline never saw):
      - OOS >= 0.80  → weight 2.0  (proven strong predictors)
      - OOS 0.60-0.80 → weight 1.0  (solid)
      - OOS < 0.60   → weight 0.5  (barely above 52% selection floor)

    Wallets with < 5 OOS participations (insufficient evidence) default
    to weight 1.0 rather than being down-weighted — we don't want to
    penalize a good train-set wallet just because its validate window
    happened to be quiet.
    """
    if not SMART_WALLETS_FILE.exists():
        raise RuntimeError(
            f"Smart wallets file not found: {SMART_WALLETS_FILE}\n"
            f"Run: python scripts/refresh_smart_wallets.py"
        )
    with open(SMART_WALLETS_FILE, "r") as f:
        data = json.load(f)
    refreshed = data.get("refreshed_at", "unknown")
    version = data.get("version", 1)

    weights: dict[str, float] = {}
    tier_counts = {"2.0x": 0, "1.0x": 0, "0.5x": 0}
    for w in data.get("wallets", []):
        wallet = w["wallet"]
        oos_n = w.get("oos_participations", 0)
        oos_acc = w.get("oos_accuracy", 0.0)
        train_acc = w.get("signal_time_accuracy", 0.0)
        # Use OOS accuracy if we have enough samples, otherwise fall back to train
        acc = oos_acc if oos_n >= 5 else train_acc
        if acc >= 0.80:
            weights[wallet] = 2.0
            tier_counts["2.0x"] += 1
        elif acc < 0.60:
            weights[wallet] = 0.5
            tier_counts["0.5x"] += 1
        else:
            weights[wallet] = 1.0
            tier_counts["1.0x"] += 1

    print(
        f"Loaded {len(weights)} smart wallets (v{version}, refreshed {refreshed[:19]}) "
        f"— tiers: {tier_counts['2.0x']}×2.0, {tier_counts['1.0x']}×1.0, {tier_counts['0.5x']}×0.5"
    )
    if version < 2:
        print(
            "  WARNING: wallet pool is v1 (legacy PnL-only selection). "
            "Strongly recommend: python scripts/refresh_smart_wallets.py "
            "to regenerate with signal-time accuracy + OOS validation."
        )
    return weights


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
    smart_wallets: set[str]  # pool membership (for fast `in` checks)
    smart_wallet_weights: dict[str, float] = field(default_factory=dict)  # wallet -> vote weight

    # Strategy params (Config N — quality-first)
    min_signal_strength: int = 7  # distinct wallets (not raw votes)
    signal_dominance: float = 2.0
    min_seconds_remaining: int = 60  # time gate: only fire with >= 1 min left
    max_bucket_age_sec: int = 60  # staleness gate: skip buckets older than this when we poll them
    position_size_usd: float = 60.0  # BASE stake; see differential sizing below
    fee_pct: float = 0.02

    # Differential position sizing based on entry price.
    # Rationale: at very low entry prices (e.g. buying a contract at $0.15),
    # the win:loss ratio is 5.67:1 — a 40% accuracy signal is profitable.
    # At high entry prices (e.g. $0.70), the ratio is 0.43:1 — we need ~70%
    # accuracy just to break even. Sizing larger in the favorable zone and
    # smaller (or not at all) in the unfavorable zone lifts expected value
    # without changing the signal logic itself.
    sizing_mode: str = "differential"  # "differential" or "fixed"
    sizing_very_low_mult: float = 1.5   # entry <= sizing_very_low_max (juicy asymmetry)
    sizing_normal_mult: float = 1.0     # entry in (very_low_max, normal_max]
    sizing_moderate_mult: float = 0.5   # entry in (normal_max, moderate_max]
    sizing_expensive_mult: float = 0.0  # entry > moderate_max: SKIP (structurally unfavorable)
    sizing_very_low_max: float = 0.20
    sizing_normal_max: float = 0.40
    sizing_moderate_max: float = 0.60

    # Flip-specific gates (all must pass to flip an existing position)
    min_flip_strength: int = 5  # stricter wallet count for flips (default = min_signal_strength + 2)
    flip_cooldown_sec: int = 60  # no flip within N seconds of the last entry/flip
    min_adverse_move: float = 0.15  # price must move >= this much against us before we flip

    # Exit rules (profit-take / stop-loss / late-window de-risking).
    # Once an exit fires, the position is closed and no re-entry is attempted
    # for this market. See process_voting()'s `state.exited` guard.
    exits_enabled: bool = True
    profit_take_threshold: float = 0.20  # sell when our side price rises +N since entry
    stop_loss_threshold: float = 0.25  # sell when our side price drops -N since entry
    stop_loss_min_remaining: int = 60  # only stop-loss if this much time is still left
    late_window_sec: int = 30  # "late" means this many seconds before market close
    late_window_min_price: float = 0.85  # in late window, sell if our side price < N

    # WebSocket event tracking
    ws_trade_events: deque = field(default_factory=lambda: deque(maxlen=500))
    ws_events_count: int = 0
    ws_latest_up_price: float = 0.5  # updated real-time from WS
    last_burst_trigger_ts: float = 0.0

    # Latest full orderbook snapshots per token (updated on `book` events).
    # Each list is (price, size), bids high-to-low, asks low-to-high.
    up_book_bids: list[tuple[float, float]] = field(default_factory=list)
    up_book_asks: list[tuple[float, float]] = field(default_factory=list)
    down_book_bids: list[tuple[float, float]] = field(default_factory=list)
    down_book_asks: list[tuple[float, float]] = field(default_factory=list)

    # Orderbook depth confirmation: before entering we require at least
    # min_book_depth_usd of resting ASK size within book_depth_window of
    # best ask on our side. Thin books gap hard on smart-wallet buys —
    # our Run-5 Cycle 7 and Run-6 Cycle 9 both cratered this way.
    min_book_depth_usd: float = 150.0
    book_depth_window: float = 0.05  # cents band above best ask to consider

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
    exited: bool = False  # once an EXIT fires, block further trades on this market
    exit_side: str | None = None  # "YES"/"NO" — side that was held when we exited (for summary)
    exit_reason: str | None = None  # why we exited (profit_take / stop_loss / late_window)

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
        # Capture full depth so process_voting can assess liquidity
        # before firing an entry (see check_book_depth_ok).
        if ev.bid_levels is not None and ev.ask_levels is not None:
            if ev.asset_id == state.up_token_id:
                state.up_book_bids = ev.bid_levels
                state.up_book_asks = ev.ask_levels
            elif ev.asset_id == state.down_token_id:
                state.down_book_bids = ev.bid_levels
                state.down_book_asks = ev.ask_levels
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


def get_entry_price(state: MarketTradingState, signal: str) -> float:
    """Best executable entry price for the side we want to buy.

    Prefer the current best ask from the orderbook because that is the price a
    real limit-at-touch order would actually pay. Fall back to the synthetic WS
    price if we do not yet have a book snapshot.
    """
    asks = state.up_book_asks if signal == "YES" else state.down_book_asks
    if asks and asks[0][0] > 0:
        return asks[0][0]
    current_up = state.ws_latest_up_price
    return current_up if signal == "YES" else 1.0 - current_up


def get_exit_price(state: MarketTradingState, side: str) -> float:
    """Best executable exit price for the side we currently hold.

    Exits should be marked to the best bid because selling into the book
    realizes bid, not mid/last. Fall back to the synthetic WS price if we do
    not yet have a bid snapshot.
    """
    bids = state.up_book_bids if side == "YES" else state.down_book_bids
    if bids and bids[0][0] > 0:
        return bids[0][0]
    current_up = state.ws_latest_up_price
    return current_up if side == "YES" else 1.0 - current_up


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


def check_book_depth_ok(state: MarketTradingState, signal: str) -> tuple[bool, str]:
    """Inspect the latest orderbook snapshot for the side we want to buy.
    Returns (ok, reason). `ok=False` means the book is too thin to safely
    enter — reject the signal to avoid the gap-fill failure mode we saw
    in Run-5 Cycle 7 (price 0.31 → 0.01 in 20s) and Run-6 Cycle 9
    (0.50 → 0.01 in 30s): both had a smart-wallet buy signal into a
    near-empty ask side.

    We check the ask side of the token we'd buy (YES → up_token_asks,
    NO → down_token_asks). If total USD notional within
    book_depth_window of best ask is below min_book_depth_usd, the market
    is too thin — one market order will walk the book way up.

    If we have no book data yet (bot just connected), allow entry by
    default (don't stall trading on a slow first book event).
    """
    asks = state.up_book_asks if signal == "YES" else state.down_book_asks
    if not asks:
        return True, "no_book_yet"
    best_ask = asks[0][0]
    if best_ask <= 0:
        return True, "no_ask"
    cap = best_ask + state.book_depth_window
    # Sum notional (USD) of asks within the depth window.
    # On Polymarket, size is in shares; USD = shares * price.
    total_usd = 0.0
    for price, size in asks:
        if price > cap:
            break
        total_usd += price * size
    if total_usd < state.min_book_depth_usd:
        return False, f"thin_book ${total_usd:.0f}<${state.min_book_depth_usd:.0f}"
    return True, f"ok ${total_usd:.0f}"


def compute_stake(state: MarketTradingState, entry_price: float) -> tuple[float, str]:
    """Return (stake_usd, tier_label) for the given entry price.

    Differential sizing puts more capital behind favorable-asymmetry entries
    (low entry price) and less behind unfavorable-asymmetry entries. In
    `fixed` mode, always returns (position_size_usd, "fixed").
    """
    if state.sizing_mode != "differential":
        return state.position_size_usd, "fixed"
    if entry_price <= state.sizing_very_low_max:
        return state.position_size_usd * state.sizing_very_low_mult, "very_low"
    if entry_price <= state.sizing_normal_max:
        return state.position_size_usd * state.sizing_normal_mult, "normal"
    if entry_price <= state.sizing_moderate_max:
        return state.position_size_usd * state.sizing_moderate_mult, "moderate"
    return state.position_size_usd * state.sizing_expensive_mult, "expensive"


def check_exit_signal(state: MarketTradingState, now_ts: int) -> str | None:
    """Decide whether to exit our current position.

    Motivated by the observation from a public Polymarket trading article:
    "The goal is not to always hold shares until the market resolves. As soon
    as you have an appropriate profit, you sell and exit."

    Three independent rules, any of which triggers a sell:
      1. Profit-take: our-side price has risen >= profit_take_threshold vs entry
      2. Stop-loss:   our-side price has dropped >= stop_loss_threshold vs entry,
                      AND there's at least stop_loss_min_remaining seconds left
                      (so we don't stop out on normal noise near close)
      3. Late-window: with < late_window_sec left AND our-side price < late_window_min_price,
                      sell to avoid last-second reversals on uncleared markets

    Returns an exit reason string or None if no exit.
    """
    if not state.exits_enabled or state.exited or state.position is None:
        return None

    side, entry_price, _size, _cost = state.position
    if state.ws_latest_up_price <= 0:  # no WS price yet
        return None
    current_our_price = get_exit_price(state, side)
    delta = current_our_price - entry_price  # positive = profitable
    time_to_close = state.end_ts - now_ts

    # Rule 1 — profit-take
    if delta >= state.profit_take_threshold:
        return f"profit_take (+{delta:.2f})"

    # Rule 2 — stop-loss. Disabled entirely for expensive-tier entries
    # (entry >= 0.60) because those trades are already small-sized and the
    # 5-min market's full-range wicks to 0/1 regularly reverse. Seen twice:
    # Run-8 C4 (NO @ 0.88, NO won after wick to 0.01 — we stopped for -$15)
    # Run-8 C18 (YES @ 0.86, YES won after wick to 0.01 — we stopped for -$15).
    # Both were directionally correct but the stop-loss converted them to
    # losses. For the cheaper tiers the asymmetric stop still applies.
    if entry_price < 0.60:
        effective_stop = max(state.stop_loss_threshold, entry_price * 0.5)
        if delta <= -effective_stop and time_to_close >= state.stop_loss_min_remaining:
            return f"stop_loss ({delta:.2f}, gate=-{effective_stop:.2f})"

    # Rule 3 — late-window de-risking
    if time_to_close <= state.late_window_sec and current_our_price < state.late_window_min_price:
        return f"late_window (price={current_our_price:.2f})"

    return None


def execute_exit(state: MarketTradingState, now_ts: int, reason: str) -> None:
    """Close the current position at the best executable sell price, book P&L,
    mark the market as exited so no re-entry happens."""
    if state.position is None:
        return
    side, entry_price, size, cost = state.position
    current_our_price = get_exit_price(state, side)
    proceeds = size * current_our_price * (1 - state.fee_pct)
    state.realized_pnl += proceeds - cost
    state.exited = True
    state.exit_side = side
    state.exit_reason = reason
    state.position = None
    bi = int((now_ts - state.start_ts) // 10)
    state.actions.append(
        {
            "bucket": bi,
            "action": "EXIT",
            "side": side,
            "price": round(current_our_price, 4),
            "entry": round(entry_price, 4),
            "delta": round(current_our_price - entry_price, 4),
            "realized_delta": round(proceeds - cost, 2),
            "reason": reason,
            "ts": now_ts,
        }
    )


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
    # Once an exit has fired on this market, don't re-enter — the exit rule
    # already realized P&L, and re-entering would churn the position.
    if state.exited:
        return

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
        # WEIGHTED voting: each distinct wallet contributes its per-wallet
        # weight (2.0 / 1.0 / 0.5 based on OOS accuracy). Falls back to 1.0
        # for any wallet missing from the weights dict (defensive default).
        # `yes_count` / `no_count` now represent "weighted vote strength",
        # not raw distinct-wallet counts. The min_signal_strength and
        # signal_dominance thresholds operate on the weighted sums.
        yes_count = sum(state.smart_wallet_weights.get(w, 1.0) for w in yes_wallets)
        no_count = sum(state.smart_wallet_weights.get(w, 1.0) for w in no_wallets)

        signal = None
        if (
            yes_count >= state.min_signal_strength
            and yes_count >= state.signal_dominance * max(no_count, 1.0)
        ):
            signal = "YES"
        elif (
            no_count >= state.min_signal_strength
            and no_count >= state.signal_dominance * max(yes_count, 1.0)
        ):
            signal = "NO"

        if signal is None:
            continue

        # Use the current executable best ask, not historical bucket price.
        # This keeps paper/live aligned with what we can actually pay.
        current_up = state.ws_latest_up_price
        our_entry_price = get_entry_price(state, signal)
        if our_entry_price < 0.05 or our_entry_price > 0.95:
            continue

        # Orderbook depth gate — reject if the ask side we'd buy into is
        # too thin to absorb our order without walking the book. Applied
        # to both ENTER and FLIP (flipping also requires buying the new side).
        book_ok, book_reason = check_book_depth_ok(state, signal)
        if not book_ok:
            state.actions.append(
                {
                    "bucket": bi,
                    "action": "SKIP_DEPTH",
                    "side": signal,
                    "reason": book_reason,
                    "yes_wallets": round(yes_count, 1),
                    "no_wallets": round(no_count, 1),
                    "ts": now_ts,
                }
            )
            continue

        if state.position is None:
            stake_usd, tier = compute_stake(state, our_entry_price)
            if stake_usd <= 0:
                state.actions.append(
                    {
                        "bucket": bi,
                        "action": "SKIP_TIER",
                        "side": signal,
                        "price": round(our_entry_price, 4),
                        "reason": f"expensive tier rejected (entry={our_entry_price:.2f})",
                        "ts": now_ts,
                    }
                )
                continue
            size = stake_usd / our_entry_price
            cost = stake_usd * (1 + state.fee_pct)
            state.position = (signal, our_entry_price, size, cost)
            state.last_position_change_ts = now_ts
            state.entry_ws_price = current_up
            state.actions.append(
                {
                    "bucket": bi,
                    "action": "ENTER",
                    "side": signal,
                    "price": round(our_entry_price, 4),
                    "yes_wallets": round(yes_count, 1),
                    "no_wallets": round(no_count, 1),
                    "remaining_s": seconds_remaining,
                    "stake_usd": round(stake_usd, 2),
                    "sizing_tier": tier,
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
            old_current = get_exit_price(state, old_side)
            proceeds = old_size * old_current * (1 - state.fee_pct)
            stake_usd, tier = compute_stake(state, our_entry_price)
            if stake_usd <= 0:
                state.actions.append(
                    {
                        "bucket": bi,
                        "action": "SKIP_TIER",
                        "side": signal,
                        "price": round(our_entry_price, 4),
                        "reason": f"expensive tier rejected (entry={our_entry_price:.2f})",
                        "ts": now_ts,
                    }
                )
                continue
            state.realized_pnl += proceeds - old_cost
            new_size = stake_usd / our_entry_price
            new_cost = stake_usd * (1 + state.fee_pct)
            state.position = (signal, our_entry_price, new_size, new_cost)
            state.last_position_change_ts = now_ts
            state.entry_ws_price = current_up
            state.actions.append(
                {
                    "bucket": bi,
                    "action": "FLIP",
                    "side": signal,
                    "price": round(our_entry_price, 4),
                    "yes_wallets": round(yes_count, 1),
                    "no_wallets": round(no_count, 1),
                    "remaining_s": seconds_remaining,
                    "stake_usd": round(stake_usd, 2),
                    "sizing_tier": tier,
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
        # Three sub-cases:
        #   (a) No trade ever — realized_pnl == 0, return flat
        #   (b) Exited via profit-take / stop-loss / late-window — pnl = realized_pnl
        #       (no settlement credit because we already sold before resolution)
        #   (c) Flipped and closed — same as (b)
        result.update(
            {
                "pnl": round(state.realized_pnl, 2),
                "position": None,
                "exited": state.exited,
                "exit_side": state.exit_side,
                "exit_reason": state.exit_reason,
                "winning_idx": state.winning_index,
            }
        )
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
    smart_wallet_weights: dict[str, float],
    confusion_detector: ConfusionDetector,
    min_signal_strength: int = 7,
    min_seconds_remaining: int = 60,
    max_bucket_age_sec: int = 30,
    min_flip_strength: int = 9,
    flip_cooldown_sec: int = 60,
    min_adverse_move: float = 0.15,
    exits_enabled: bool = True,
    profit_take_threshold: float = 0.20,
    stop_loss_threshold: float = 0.25,
    stop_loss_min_remaining: int = 60,
    late_window_sec: int = 30,
    late_window_min_price: float = 0.85,
    sizing_mode: str = "differential",
    position_size_usd: float = 60.0,
    min_book_depth_usd: float = 150.0,
    book_depth_window: float = 0.05,
    trading_mode: str = "paper",
    clob_client: object | None = None,
    position_store: object | None = None,
    risk_manager: object | None = None,
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
        smart_wallet_weights=smart_wallet_weights,
        min_signal_strength=min_signal_strength,
        min_seconds_remaining=min_seconds_remaining,
        max_bucket_age_sec=max_bucket_age_sec,
        min_flip_strength=min_flip_strength,
        flip_cooldown_sec=flip_cooldown_sec,
        min_adverse_move=min_adverse_move,
        exits_enabled=exits_enabled,
        profit_take_threshold=profit_take_threshold,
        stop_loss_threshold=stop_loss_threshold,
        stop_loss_min_remaining=stop_loss_min_remaining,
        late_window_sec=late_window_sec,
        late_window_min_price=late_window_min_price,
        sizing_mode=sizing_mode,
        position_size_usd=position_size_usd,
        min_book_depth_usd=min_book_depth_usd,
        book_depth_window=book_depth_window,
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
                prev_position = state.position
                new_trades = await poll_http_trades(
                    data_api, state, triggered_by_burst=(trigger_reason == "BURST")
                )
                if new_trades > 0 or trigger_reason == "BURST":
                    process_voting(state, now_int)

                # Live-execution hook: if process_voting just opened a position
                # (prev was None, now is not None), submit a real order
                if trading_mode != "paper" and prev_position is None and state.position is not None:
                    side, entry_p, size, cost = state.position
                    token_id = state.up_token_id if side == "YES" else state.down_token_id
                    if trading_mode == "dry-run":
                        print(
                            f"    [DRY-RUN] WOULD BUY {side} token {token_id[:12]}... "
                            f"@ {entry_p:.4f} size={size:.2f} cost=${cost:.2f}",
                            flush=True,
                        )
                    elif clob_client is not None:
                        # Pre-trade risk check
                        stake_usd = cost / 1.02  # reverse fee to get stake
                        balance = await clob_client.get_usdc_balance()
                        if risk_manager:
                            ok, reason = risk_manager.pre_trade_check(
                                daily_pnl=position_store.get_daily_pnl() if position_store else 0,
                                session_pnl=position_store.get_session_pnl() if position_store else 0,
                                balance_usd=balance,
                                stake_usd=stake_usd,
                                open_positions=1 if state.position else 0,
                            )
                            if not ok:
                                print(f"    [RISK BLOCKED] {reason}", flush=True)
                                state.position = None  # revert paper position
                                continue
                        order = await clob_client.place_market_buy(
                            token_id=token_id,
                            amount_usd=stake_usd,
                            price=entry_p,
                        )
                        fill = await clob_client.wait_for_fill(order.order_id, timeout_sec=10)
                        print(
                            f"    [LIVE] ORDER {fill.status}: {side} {token_id[:12]}... "
                            f"@ {entry_p:.4f} id={order.order_id[:16]}",
                            flush=True,
                        )
                        if position_store:
                            position_store.record_entry(LivePosition(
                                market_slug=state.slug,
                                condition_id=state.condition_id,
                                token_id=token_id,
                                side=side,
                                entry_price=entry_p,
                                size=size,
                                cost_usd=cost,
                                order_id=order.order_id,
                                order_status=fill.status,
                                entered_at=datetime.utcnow().isoformat() + "Z",
                                sizing_tier=state.actions[-1].get("sizing_tier", "") if state.actions else "",
                            ))
                        if risk_manager:
                            risk_manager.record_trade()

            # Exit-signal check runs every main-loop tick (200ms), not only on
            # HTTP polls, so profit-take / stop-loss can fire on pure WS price
            # moves. Fires at most once per market (execute_exit sets state.exited).
            if state.position is not None and not state.exited:
                reason = check_exit_signal(state, now_int)
                if reason is not None:
                    side_before = state.position[0]
                    size_before = state.position[2]
                    exit_price = get_exit_price(state, side_before)
                    print(
                        f"    [{_fmt_ts(now_int)}] EXIT {side_before} "
                        f"@ {exit_price:.3f} — {reason}",
                        flush=True,
                    )

                    # Live exit: submit sell order
                    if trading_mode == "dry-run":
                        token_id = state.up_token_id if side_before == "YES" else state.down_token_id
                        print(
                            f"    [DRY-RUN] WOULD SELL {side_before} token {token_id[:12]}... "
                            f"@ {exit_price:.4f} size={size_before:.2f}",
                            flush=True,
                        )
                    elif trading_mode == "live" and clob_client is not None:
                        token_id = state.up_token_id if side_before == "YES" else state.down_token_id
                        order = await clob_client.place_market_sell(
                            token_id=token_id,
                            size=size_before,
                            price=exit_price,
                        )
                        fill = await clob_client.wait_for_fill(order.order_id, timeout_sec=10)
                        print(
                            f"    [LIVE] SELL {fill.status}: id={order.order_id[:16]}",
                            flush=True,
                        )

                    execute_exit(state, now_int, reason)

                    if position_store and state.exited:
                        pnl = state.realized_pnl
                        position_store.record_exit(pnl, reason)

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
        action = a.get("action", "")
        side = a.get("side", "-")
        price = a.get("price", 0.0)
        if action in ("ENTER", "FLIP"):
            stake = a.get("stake_usd")
            tier = a.get("sizing_tier")
            stake_str = f", stake=${stake} [{tier}]" if stake is not None else ""
            print(
                f"    b{a['bucket']:3d}: {action:9s} {side} @ {price}  "
                f"(wallets {a.get('yes_wallets', 0)}Y/{a.get('no_wallets', 0)}N, "
                f"remaining {a.get('remaining_s', 0)}s{stake_str})"
            )
        elif action == "EXIT":
            print(
                f"    b{a['bucket']:3d}: EXIT      {side} @ {price}  "
                f"(entry={a.get('entry', 0):.2f}, delta={a.get('delta', 0):+.2f}, "
                f"realized={a.get('realized_delta', 0):+.2f}) — {a.get('reason', '')}"
            )
        elif action == "SKIP_FLIP":
            print(
                f"    b{a['bucket']:3d}: SKIP_FLIP  {side} — {a.get('reason', '')}"
            )
        elif action == "SKIP_DEPTH":
            print(
                f"    b{a['bucket']:3d}: SKIP_DEPTH {side} "
                f"(votes {a.get('yes_wallets', 0)}Y/{a.get('no_wallets', 0)}N) — {a.get('reason', '')}"
            )
        elif action == "SKIP_TIER":
            print(
                f"    b{a['bucket']:3d}: SKIP_TIER  {side} @ {a.get('price', 0):.2f} — {a.get('reason', '')}"
            )
        else:
            print(f"    b{a['bucket']:3d}: {action}")

    # Record outcome for confusion detector.
    # A market is "signal-bearing" if we entered at any point, even if we
    # subsequently exited (the ensemble voted, which is what the detector
    # cares about).
    has_entered = any(a.get("action") in ("ENTER", "FLIP") for a in state.actions)
    has_open_position = state.position is not None
    had_signal = has_entered or has_open_position
    was_correct: bool | None = None
    if state.winning_index is not None:
        if has_open_position:
            pos_idx = 0 if state.position[0] == "YES" else 1
            was_correct = pos_idx == state.winning_index
        elif state.exited and state.exit_side is not None:
            pos_idx = 0 if state.exit_side == "YES" else 1
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
        default=3,
        help=(
            "Minimum WEIGHTED vote sum (not raw distinct wallets) to fire a "
            "signal. With the v2 pool's weight tiers (2.0x for OOS>=80%%, "
            "1.0x for 60-80%%, 0.5x for <60%%), 3.0 is the data-validated "
            "floor: 30-cycle tests showed 2.0 added losers while 4.0 blocks "
            "profitable trades. Also requires 2x dominance over opposing side."
        ),
    )
    parser.add_argument(
        "--min-seconds-remaining",
        type=int,
        default=60,
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
        default=60,
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
    parser.add_argument(
        "--profit-take",
        type=float,
        default=0.20,
        help=(
            "Sell when our side's price has risen by this much since entry. "
            "Turns the 'always hold to resolution' strategy into a profit-taker."
        ),
    )
    parser.add_argument(
        "--stop-loss",
        type=float,
        default=1.0,
        help=(
            "Sell when our side's price has dropped by this much since entry. "
            "Default 1.0 effectively disables (price can't move >1.0). "
            "30-cycle data showed 3/3 stop-losses were losers; 2/3 killed "
            "correct positions. Use 0.25 to re-enable."
        ),
    )
    parser.add_argument(
        "--stop-loss-min-remaining",
        type=int,
        default=60,
        help="Only stop-loss while at least N seconds remain in the market",
    )
    parser.add_argument(
        "--late-window-sec",
        type=int,
        default=30,
        help="Treat the last N seconds of a market as the late-window (de-risking zone)",
    )
    parser.add_argument(
        "--late-window-min-price",
        type=float,
        default=0.85,
        help=(
            "In the late window, sell if our side's price is below this threshold. "
            "Aggressive default (0.85): force-sell any position that hasn't clearly "
            "cleared in our favor before market close, to eliminate last-second "
            "reversal risk. Conservative value 0.70 leaves moderate winners exposed."
        ),
    )
    parser.add_argument(
        "--no-exits",
        action="store_true",
        help="Disable profit-take / stop-loss / late-window exits (hold to resolution)",
    )
    parser.add_argument(
        "--sizing-mode",
        choices=["differential", "fixed"],
        default="differential",
        help=(
            "Position sizing: 'differential' scales stake by entry price tier "
            "(1.5x at <=0.20, 1.0x at 0.20-0.40, 0.5x at 0.40-0.60, SKIP above); "
            "'fixed' uses --position-size for every trade regardless of entry. "
            "Expensive-tier rejection (>0.60) is data-backed: both expensive "
            "entries in the 30-cycle test were losses."
        ),
    )
    parser.add_argument(
        "--position-size",
        type=float,
        default=60.0,
        help="Base stake in USD (scaled by --sizing-mode tier multipliers)",
    )
    parser.add_argument(
        "--min-book-depth",
        type=float,
        default=150.0,
        help=(
            "Minimum USD notional of resting ASK size (within "
            "--book-depth-window of best ask) required on our side before "
            "we'll enter. Thin-book entries gap-fill catastrophically "
            "(see Run-5 C7 / Run-6 C9). Set 0 to disable the gate."
        ),
    )
    parser.add_argument(
        "--book-depth-window",
        type=float,
        default=0.05,
        help="Ask-price band (cents above best ask) to sum for --min-book-depth",
    )
    parser.add_argument(
        "--mode",
        choices=["paper", "live", "dry-run"],
        default="paper",
        help=(
            "Trading mode: 'paper' = simulated fills (no real money), "
            "'live' = real order submission via CLOB API (requires .env credentials), "
            "'dry-run' = same as live but prints 'WOULD PLACE' without submitting"
        ),
    )
    parser.add_argument(
        "--max-daily-loss",
        type=float,
        default=20.0,
        help="Kill-switch: halt trading if daily realized loss exceeds this (USD). Live/dry-run only.",
    )
    args = parser.parse_args()

    # Resolve --no-flips and auto-default for min-flip-strength
    if args.no_flips:
        args.min_flip_strength = 999
    elif args.min_flip_strength == 0:
        args.min_flip_strength = args.min_signal_strength + 2

    mode_label = {"paper": "PAPER (no real money)", "live": "LIVE (REAL MONEY)", "dry-run": "DRY-RUN (live logic, no orders)"}
    print("=" * 90)
    print(f"  TRADING BOT — WebSocket-First | MODE: {mode_label[args.mode]}")
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
    if args.no_exits:
        print(f"  Exits:        DISABLED (hold to resolution)")
    else:
        print(
            f"  Exit rules:   profit>=+{args.profit_take:.2f}, "
            f"stop<=-{args.stop_loss:.2f} (if >={args.stop_loss_min_remaining}s left), "
            f"late<{args.late_window_min_price:.2f} (<{args.late_window_sec}s left)"
        )
    if args.sizing_mode == "differential":
        print(
            f"  Sizing:       differential base=${args.position_size:.0f} "
            f"(1.5x<=0.20, 1.0x 0.20-0.40, 0.5x 0.40-0.60, SKIP>0.60)"
        )
    else:
        print(f"  Sizing:       fixed ${args.position_size:.0f} per trade")
    if args.min_book_depth > 0:
        print(
            f"  Book gate:    require >=${args.min_book_depth:.0f} ask-depth "
            f"within {args.book_depth_window:.2f} of best ask on our side"
        )
    else:
        print(f"  Book gate:    DISABLED")
    print(f"  Voting:       weighted (2.0x OOS>=80%, 1.0x 60-80%, 0.5x <60%)")
    if args.mode != "paper":
        print(f"  Daily loss:   -${args.max_daily_loss:.0f} kill switch")
    print(f"  Mode:         {mode_label[args.mode]}")
    print(f"  Polling:      {BASELINE_POLL_INTERVAL}s baseline + burst-triggered HTTP")
    print(f"  Burst thresh: {BURST_TRADE_THRESHOLD} trades in {BURST_LOOKBACK_SEC}s")
    print(f"  Price source: WebSocket real-time (used for entry pricing)")
    print("=" * 90)

    # --- Initialize infrastructure ---
    clob_client = None
    position_store = None
    risk_mgr = None

    if args.mode in ("live", "dry-run"):
        if not _LIVE_TRADING_AVAILABLE:
            print(
                "\n  ERROR: Live trading deps not installed."
                "\n  Run: pip install py-clob-client eth-account python-dotenv"
                "\n  Then set credentials in .env (see polymarket/clients/clob_order_client.py)"
            )
            return
        risk_mgr = RiskManager(RiskLimits(max_daily_loss_usd=args.max_daily_loss))
        position_store = PositionStore()
        if position_store.has_open_position():
            pos = position_store.state.active_position
            print(f"\n  WARNING: open position from previous session: {pos.side} {pos.market_slug}")
            print(f"  The bot will NOT manage this position. Resolve it manually or clear data/live_position.json")
        if args.mode == "live":
            clob_client = ClobOrderClient()
            print(f"\n  CLOB client initialized — REAL ORDERS WILL BE SUBMITTED")
        else:
            print(f"\n  DRY-RUN mode — will print 'WOULD PLACE' without submitting")

    print("\n[1/3] Loading smart wallets...")
    smart_wallet_weights = load_smart_wallets()
    smart_wallets = set(smart_wallet_weights.keys())

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

            # --- Pre-market risk checks (live/dry-run only) ---
            if risk_mgr is not None:
                ok, reason = risk_mgr.check_kill_switch()
                if not ok:
                    print(f"\n  KILL SWITCH: {reason}")
                    break
                ok, reason = risk_mgr.check_daily_loss(
                    position_store.get_daily_pnl() if position_store else 0
                )
                if not ok:
                    print(f"\n  DAILY LOSS LIMIT: {reason}")
                    break

            market_count += 1
            print(f"\n========== Market {market_count} ==========", flush=True)
            result = await trade_one_market(
                gamma,
                data_api,
                ws,
                market,
                smart_wallets,
                smart_wallet_weights,
                confusion,
                min_signal_strength=args.min_signal_strength,
                min_seconds_remaining=args.min_seconds_remaining,
                max_bucket_age_sec=args.max_bucket_age_sec,
                min_flip_strength=args.min_flip_strength,
                flip_cooldown_sec=args.flip_cooldown_sec,
                min_adverse_move=args.min_adverse_move,
                exits_enabled=(not args.no_exits),
                profit_take_threshold=args.profit_take,
                stop_loss_threshold=args.stop_loss,
                stop_loss_min_remaining=args.stop_loss_min_remaining,
                late_window_sec=args.late_window_sec,
                late_window_min_price=args.late_window_min_price,
                sizing_mode=args.sizing_mode,
                position_size_usd=args.position_size,
                min_book_depth_usd=args.min_book_depth,
                book_depth_window=args.book_depth_window,
                trading_mode=args.mode,
                clob_client=clob_client,
                position_store=position_store,
                risk_manager=risk_mgr,
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
                if r.get("exited"):
                    pos = f"{r.get('exit_side', '?')}(EXIT:{r.get('exit_reason', '')})"
                else:
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
