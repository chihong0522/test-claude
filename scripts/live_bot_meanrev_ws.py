#!/usr/bin/env python3
"""Paper runner for 5-minute BTC mean-reversion / continuation strategies."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

from polymarket.backtester.portfolio import select_wallet_rows
from polymarket.clients.clob_websocket import MarketWebSocketClient
from polymarket.clients.data_api import DataAPIClient
from polymarket.clients.gamma import GammaClient
from polymarket.trading.mean_reversion_execution import (
    MeanReversionConfig as ExecutionConfig,
    build_signal,
    check_exit,
    should_enter_mean_reversion,
)
from polymarket.trading.mean_reversion_profiles import MeanReversionProfile, load_profile
from scripts.live_bot_ws import (
    BASELINE_POLL_INTERVAL,
    FIVE_MIN,
    MAIN_LOOP_INTERVAL,
    _fmt_ts,
    fetch_current_market,
    get_exit_price,
    poll_http_trades,
    record_ws_trade_event,
    update_ws_price,
)


@dataclass
class PendingSignal:
    crowd_side: str
    bucket_idx: int
    weighted_yes: float
    weighted_no: float
    baseline_up_price: float
    detected_at: float
    signal_source: str


@dataclass
class OpenPosition:
    side: str
    entry_price: float
    size: float
    cost: float
    entered_at: float
    bucket_idx: int


@dataclass
class MeanRevPaperState:
    slug: str
    condition_id: str
    token_ids: list[str]
    up_token_id: str
    down_token_id: str
    start_ts: int
    end_ts: int
    profile: MeanReversionProfile
    selected_wallets: set[str]
    bucket_sec: int = 10

    ws_trade_events: list[float] = field(default_factory=list)
    ws_events_count: int = 0
    ws_latest_up_price: float = 0.5
    ws_price_history: deque[tuple[float, float]] = field(default_factory=deque)
    last_burst_trigger_ts: float = 0.0

    up_book_bids: list[tuple[float, float]] = field(default_factory=list)
    up_book_asks: list[tuple[float, float]] = field(default_factory=list)
    down_book_bids: list[tuple[float, float]] = field(default_factory=list)
    down_book_asks: list[tuple[float, float]] = field(default_factory=list)

    seen_tx_hashes: set[str] = field(default_factory=set)
    http_trades: list[dict] = field(default_factory=list)
    last_http_poll_ts: float = 0.0
    http_poll_count: int = 0
    burst_triggered_polls: int = 0

    buckets_processed: set[int] = field(default_factory=set)
    pending_signal: PendingSignal | None = None
    position: OpenPosition | None = None
    traded: bool = False
    actions: list[dict] = field(default_factory=list)
    realized_pnl: float = 0.0


def _top_n_from_wallet_set(wallet_set: str) -> int:
    m = re.fullmatch(r"top(\d+)", wallet_set)
    if not m:
        raise ValueError(f"Unsupported wallet_set for paper runner: {wallet_set}")
    return int(m.group(1))


def resolve_profile_wallets(profile: MeanReversionProfile, pool_data: dict) -> set[str]:
    if profile.signal_source == "price":
        return set()
    if profile.explicit_wallets:
        return set(profile.explicit_wallets)
    if not profile.wallet_set:
        return set()
    top_n = _top_n_from_wallet_set(profile.wallet_set)
    selected_rows = select_wallet_rows(pool_data, wallet_set="top", top_n=top_n)
    return {row["wallet"] for row in selected_rows}


def detect_price_signal_crowd_side(anchor_up_price: float, current_up_price: float, pop_threshold: float) -> str | None:
    if current_up_price - anchor_up_price >= pop_threshold:
        return "YES"
    if anchor_up_price - current_up_price >= pop_threshold:
        return "NO"
    return None


def detect_price_signal_threshold_touch_crowd_side(
    current_up_price: float,
    min_crowd_price: float,
    max_crowd_price: float,
) -> str | None:
    if min_crowd_price <= current_up_price <= max_crowd_price:
        return "YES"
    mirrored_crowd_price = 1.0 - current_up_price
    if min_crowd_price <= mirrored_crowd_price <= max_crowd_price:
        return "NO"
    return None


def detect_price_signal_double_touch_crowd_side(
    *,
    price_history: list[tuple[float, float]] | deque[tuple[float, float]],
    market_start_ts: float,
    touch_price: float,
    deadline_sec: int,
    max_extension: float,
) -> str | None:
    valid_points = [
        (ts, up_price)
        for ts, up_price in price_history
        if 0 <= ts - market_start_ts <= deadline_sec
    ]
    if len(valid_points) < 2:
        return None

    _latest_ts, latest_up = valid_points[-1]
    latest_yes = latest_up
    latest_no = 1.0 - latest_up

    if latest_yes >= touch_price:
        first_yes = next((up_price for _ts, up_price in valid_points[:-1] if up_price >= touch_price), None)
        if first_yes is not None and latest_yes <= first_yes + max_extension:
            return "YES"

    if latest_no >= touch_price:
        first_no = next((1.0 - up_price for _ts, up_price in valid_points[:-1] if 1.0 - up_price >= touch_price), None)
        if first_no is not None and latest_no <= first_no + max_extension:
            return "NO"

    return None


def _trim_ws_price_history(state: MeanRevPaperState, now_real: float) -> None:
    history_requirement = max(state.profile.lookback_sec + 10, 30)
    if state.profile.price_signal_mode == "double_touch" and state.profile.double_touch_deadline_sec is not None:
        history_requirement = max(history_requirement, state.profile.double_touch_deadline_sec + 10)
    keep_after = now_real - history_requirement
    while state.ws_price_history and state.ws_price_history[0][0] < keep_after:
        state.ws_price_history.popleft()


def _record_ws_price_point(state: MeanRevPaperState, now_real: float) -> None:
    if not 0.0 <= state.ws_latest_up_price <= 1.0:
        return
    if state.ws_price_history and abs(state.ws_price_history[-1][1] - state.ws_latest_up_price) < 1e-9:
        state.ws_price_history[-1] = (now_real, state.ws_latest_up_price)
    else:
        state.ws_price_history.append((now_real, state.ws_latest_up_price))
    _trim_ws_price_history(state, now_real)


def _price_n_seconds_ago(state: MeanRevPaperState, now_real: float, lookback_sec: int) -> float | None:
    target = now_real - lookback_sec
    candidate: float | None = None
    for ts, price in state.ws_price_history:
        if ts <= target:
            candidate = price
        else:
            break
    return candidate


def _entry_books(state: MeanRevPaperState, side: str) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    if side == "YES":
        return state.up_book_asks, state.up_book_bids
    return state.down_book_asks, state.down_book_bids


def _execution_config(state: MeanRevPaperState) -> ExecutionConfig:
    profile = state.profile
    return ExecutionConfig(
        signal_dominance=profile.signal_dominance,
        min_weighted_signal=float(profile.min_signal_strength),
        min_pop_abs=profile.pop_threshold,
        min_seconds_remaining=profile.min_seconds_remaining,
        max_burst_age_sec=profile.max_burst_age_sec,
        trade_mode=profile.trade_mode,
        min_entry_price=profile.entry_price_floor,
        max_entry_price=profile.entry_price_cap if profile.entry_price_cap is not None else 1.0,
        min_crowd_price=profile.min_crowd_price,
        max_crowd_price=profile.max_crowd_price,
        max_spread=profile.max_spread,
        min_entry_ask_depth_usd=profile.min_entry_ask_depth_usd,
        min_exit_bid_depth_usd=profile.min_exit_bid_depth_usd,
        depth_window=profile.depth_window,
        target_price_delta=profile.target_price_delta,
        target_price_abs=profile.target_price_abs,
        stop_price_delta=profile.stop_price_delta,
        max_hold_sec=profile.hold_sec,
    )


def _bucketize_http_trades(state: MeanRevPaperState) -> tuple[dict[int, list[dict]], dict[int, float], int]:
    buckets: dict[int, list[dict]] = defaultdict(list)
    sorted_trades = sorted(state.http_trades, key=lambda t: int(t.get("timestamp") or 0))
    for trade in sorted_trades:
        offset = int(trade.get("timestamp") or 0) - state.start_ts
        if 0 <= offset <= FIVE_MIN:
            bucket_idx = offset // state.bucket_sec
            buckets[bucket_idx].append(trade)

    max_bucket = FIVE_MIN // state.bucket_sec
    last_up_price: dict[int, float] = {}
    running_price = state.ws_latest_up_price if 0 < state.ws_latest_up_price < 1 else 0.5
    for bucket_idx in range(max_bucket + 1):
        for trade in buckets.get(bucket_idx, []):
            price = float(trade.get("price") or 0.5)
            outcome_idx = int(trade.get("outcomeIndex") or 0)
            running_price = price if outcome_idx == 0 else 1.0 - price
        last_up_price[bucket_idx] = running_price
    return buckets, last_up_price, max_bucket


def _elapsed_within_profile_window(state: MeanRevPaperState, elapsed_sec: int) -> bool:
    if elapsed_sec < state.profile.min_elapsed_sec:
        return False
    if state.profile.max_elapsed_sec is not None and elapsed_sec > state.profile.max_elapsed_sec:
        return False
    return True


def _queue_signal(
    state: MeanRevPaperState,
    *,
    crowd_side: str,
    bucket_idx: int,
    weighted_yes: float,
    weighted_no: float,
    baseline_up_price: float,
    now_real: float,
    now_int: int,
    signal_source: str,
) -> None:
    state.pending_signal = PendingSignal(
        crowd_side=crowd_side,
        bucket_idx=bucket_idx,
        weighted_yes=weighted_yes,
        weighted_no=weighted_no,
        baseline_up_price=baseline_up_price,
        detected_at=now_real,
        signal_source=signal_source,
    )
    action = {
        "bucket": bucket_idx,
        "action": "QUEUE",
        "crowd_side": crowd_side,
        "baseline_up": round(baseline_up_price, 4),
        "signal_source": signal_source,
        "ts": now_int,
    }
    if signal_source == "wallets":
        action["yes_wallets"] = weighted_yes
        action["no_wallets"] = weighted_no
    state.actions.append(action)


def _scan_wallet_pending_signal(state: MeanRevPaperState, now_real: float, now_int: int) -> None:
    if not state.selected_wallets:
        return
    buckets, last_up_price, _max_bucket = _bucketize_http_trades(state)
    current_real_bucket = max(0, (now_int - state.start_ts) // state.bucket_sec)
    lookback_buckets = max(1, state.profile.lookback_sec // state.bucket_sec)

    for bucket_idx in sorted(buckets.keys()):
        if bucket_idx in state.buckets_processed:
            continue
        if bucket_idx > current_real_bucket:
            break
        state.buckets_processed.add(bucket_idx)
        elapsed_sec = bucket_idx * state.bucket_sec
        if not _elapsed_within_profile_window(state, elapsed_sec):
            continue

        bucket = buckets[bucket_idx]
        smart_buy_trades = [
            trade
            for trade in bucket
            if trade.get("proxyWallet") in state.selected_wallets and (trade.get("side") or "BUY").upper() == "BUY"
        ]
        if not smart_buy_trades:
            continue

        yes_wallets = {
            trade.get("proxyWallet")
            for trade in smart_buy_trades
            if int(trade.get("outcomeIndex") or 0) == 0 and trade.get("proxyWallet")
        }
        no_wallets = {
            trade.get("proxyWallet")
            for trade in smart_buy_trades
            if int(trade.get("outcomeIndex") or 0) == 1 and trade.get("proxyWallet")
        }
        yes_count = float(len(yes_wallets))
        no_count = float(len(no_wallets))
        crowd_side = None
        if yes_count >= state.profile.min_signal_strength and yes_count >= state.profile.signal_dominance * max(no_count, 1.0):
            crowd_side = "YES"
        elif no_count >= state.profile.min_signal_strength and no_count >= state.profile.signal_dominance * max(yes_count, 1.0):
            crowd_side = "NO"
        if crowd_side is None:
            continue

        baseline_bucket = max(0, bucket_idx - lookback_buckets)
        baseline_up_price = last_up_price.get(baseline_bucket, 0.5)
        _queue_signal(
            state,
            crowd_side=crowd_side,
            bucket_idx=bucket_idx,
            weighted_yes=yes_count,
            weighted_no=no_count,
            baseline_up_price=baseline_up_price,
            now_real=now_real,
            now_int=now_int,
            signal_source="wallets",
        )
        return


def _scan_price_pending_signal(state: MeanRevPaperState, now_real: float, now_int: int) -> None:
    current_real_bucket = max(0, (now_int - state.start_ts) // state.bucket_sec)
    if current_real_bucket in state.buckets_processed:
        return

    elapsed_sec = now_int - state.start_ts
    if not _elapsed_within_profile_window(state, elapsed_sec):
        return
    current_up_price = state.ws_latest_up_price if 0.0 <= state.ws_latest_up_price <= 1.0 else None
    if current_up_price is None:
        return

    if state.profile.price_signal_mode == "double_touch":
        state.buckets_processed.add(current_real_bucket)
        if state.profile.double_touch_crowd_price is None or state.profile.double_touch_deadline_sec is None:
            return
        crowd_side = detect_price_signal_double_touch_crowd_side(
            price_history=state.ws_price_history,
            market_start_ts=float(state.start_ts),
            touch_price=state.profile.double_touch_crowd_price,
            deadline_sec=state.profile.double_touch_deadline_sec,
            max_extension=state.profile.double_touch_max_extension,
        )
        if crowd_side is None:
            return
        _queue_signal(
            state,
            crowd_side=crowd_side,
            bucket_idx=current_real_bucket,
            weighted_yes=0.0,
            weighted_no=0.0,
            baseline_up_price=current_up_price,
            now_real=now_real,
            now_int=now_int,
            signal_source="price",
        )
        return

    if state.profile.price_signal_mode == "threshold_touch":
        state.buckets_processed.add(current_real_bucket)
        crowd_side = detect_price_signal_threshold_touch_crowd_side(
            current_up_price=current_up_price,
            min_crowd_price=state.profile.min_crowd_price,
            max_crowd_price=state.profile.max_crowd_price,
        )
        if crowd_side is None:
            return
        _queue_signal(
            state,
            crowd_side=crowd_side,
            bucket_idx=current_real_bucket,
            weighted_yes=0.0,
            weighted_no=0.0,
            baseline_up_price=current_up_price,
            now_real=now_real,
            now_int=now_int,
            signal_source="price",
        )
        return

    if elapsed_sec < state.profile.lookback_sec:
        return

    baseline_up_price = _price_n_seconds_ago(state, now_real, state.profile.lookback_sec)
    if baseline_up_price is None:
        return

    state.buckets_processed.add(current_real_bucket)
    crowd_side = detect_price_signal_crowd_side(
        anchor_up_price=baseline_up_price,
        current_up_price=current_up_price,
        pop_threshold=state.profile.pop_threshold,
    )
    if crowd_side is None:
        return

    crowd_price = current_up_price if crowd_side == "YES" else 1.0 - current_up_price
    if crowd_price < state.profile.min_crowd_price or crowd_price > state.profile.max_crowd_price:
        return

    _queue_signal(
        state,
        crowd_side=crowd_side,
        bucket_idx=current_real_bucket,
        weighted_yes=0.0,
        weighted_no=0.0,
        baseline_up_price=baseline_up_price,
        now_real=now_real,
        now_int=now_int,
        signal_source="price",
    )


def _scan_for_new_pending_signal(state: MeanRevPaperState, now_real: float, now_int: int) -> None:
    if state.position is not None or state.pending_signal is not None or state.traded:
        return
    if state.profile.signal_source == "price":
        _scan_price_pending_signal(state, now_real, now_int)
    else:
        _scan_wallet_pending_signal(state, now_real, now_int)


def _try_execute_pending_signal(state: MeanRevPaperState, now_real: float, now_int: int) -> None:
    pending = state.pending_signal
    if pending is None or state.position is not None or state.traded:
        return
    if now_real - pending.detected_at < state.profile.latency_sec:
        return

    elapsed_sec = now_int - state.start_ts
    if not _elapsed_within_profile_window(state, elapsed_sec):
        state.actions.append({"bucket": pending.bucket_idx, "action": "SKIP_SIGNAL_LOST", "ts": now_int})
        state.pending_signal = None
        return

    exec_cfg = _execution_config(state)
    signal = build_signal(
        weighted_yes=pending.weighted_yes,
        weighted_no=pending.weighted_no,
        crowd_side=pending.crowd_side,
        anchor_up_price=pending.baseline_up_price,
        current_up_price=state.ws_latest_up_price,
        remaining_s=max(0, state.end_ts - now_int),
        burst_age_sec=now_real - pending.detected_at,
        config=exec_cfg,
    )
    if signal is None:
        state.actions.append({"bucket": pending.bucket_idx, "action": "SKIP_SIGNAL_LOST", "ts": now_int})
        state.pending_signal = None
        return

    entry_asks, entry_bids = _entry_books(state, signal.entry_side)
    ok, reason = should_enter_mean_reversion(
        signal,
        exec_cfg,
        entry_asks=entry_asks,
        entry_bids=entry_bids,
        already_traded=state.traded,
        has_open_position=state.position is not None,
    )
    if not ok:
        state.actions.append(
            {
                "bucket": pending.bucket_idx,
                "action": "SKIP_ENTER",
                "side": signal.entry_side,
                "reason": reason,
                "price": round(signal.entry_price, 4),
                "pop": round(signal.pop_abs, 4),
                "crowd_side": signal.crowd_side,
                "signal_source": pending.signal_source,
                "ts": now_int,
            }
        )
        state.pending_signal = None
        return

    entry_price = entry_asks[0][0]
    size = state.position_size_usd / entry_price
    cost = state.position_size_usd * (1 + state.profile.fee_pct)
    state.position = OpenPosition(
        side=signal.entry_side,
        entry_price=entry_price,
        size=size,
        cost=cost,
        entered_at=now_real,
        bucket_idx=pending.bucket_idx,
    )
    state.traded = True
    state.actions.append(
        {
            "bucket": pending.bucket_idx,
            "action": "ENTER",
            "side": signal.entry_side,
            "crowd_side": signal.crowd_side,
            "price": round(entry_price, 4),
            "crowd_price": round(signal.crowd_price, 4),
            "pop": round(signal.pop_abs, 4),
            "remaining_s": max(0, state.end_ts - now_int),
            "stake_usd": round(state.position_size_usd, 2),
            "signal_source": pending.signal_source,
            "ts": now_int,
        }
    )
    state.pending_signal = None


def _close_position(state: MeanRevPaperState, exit_price: float, reason: str, held: int, now_int: int) -> None:
    if state.position is None:
        return
    proceeds = state.position.size * exit_price * (1 - state.profile.fee_pct)
    pnl = proceeds - state.position.cost
    state.realized_pnl += pnl
    state.actions.append(
        {
            "bucket": state.position.bucket_idx,
            "action": "EXIT",
            "side": state.position.side,
            "price": round(exit_price, 4),
            "entry": round(state.position.entry_price, 4),
            "realized_delta": round(pnl, 2),
            "reason": reason,
            "held_s": held,
            "ts": now_int,
        }
    )
    state.position = None


def _maybe_exit_position(state: MeanRevPaperState, now_real: float, now_int: int) -> None:
    if state.position is None:
        return
    held = int(now_real - state.position.entered_at)
    current_bid = get_exit_price(state, state.position.side)
    should_exit, reason = check_exit(
        entry_price=state.position.entry_price,
        current_bid_price=current_bid,
        seconds_held=held,
        config=_execution_config(state),
    )
    if should_exit and reason is not None:
        _close_position(state, current_bid, reason, held, now_int)


def summarize_market(state: MeanRevPaperState) -> dict:
    return {
        "slug": state.slug,
        "condition_id": state.condition_id,
        "actions": list(state.actions),
        "pnl": round(state.realized_pnl, 2),
        "position": None if state.position is None else state.position.side,
        "traded": state.traded,
        "http_poll_count": state.http_poll_count,
        "burst_triggered_polls": state.burst_triggered_polls,
        "ws_events_count": state.ws_events_count,
    }


async def trade_one_market_meanrev(
    gamma: GammaClient,
    data_api: DataAPIClient,
    ws: MarketWebSocketClient,
    market_info: dict,
    selected_wallets: set[str],
    profile_name: str,
) -> dict:
    profile = load_profile(profile_name)
    slug_ts = market_info["_slug_ts"]
    condition_id = market_info["condition_id"]
    token_ids = market_info.get("token_ids") or []
    if len(token_ids) < 2:
        return {"slug": market_info.get("slug"), "pnl": 0.0, "action": "NO_TOKENS"}

    state = MeanRevPaperState(
        slug=market_info.get("slug", ""),
        condition_id=condition_id,
        token_ids=token_ids,
        up_token_id=token_ids[0],
        down_token_id=token_ids[1],
        start_ts=slug_ts,
        end_ts=slug_ts + FIVE_MIN,
        profile=profile,
        selected_wallets=selected_wallets,
    )
    state.position_size_usd = profile.position_size_usd  # attribute used by existing status/output helpers

    print(f"\n  [Market] {state.slug}")
    print(f"  Window: {_fmt_ts(state.start_ts)} -> {_fmt_ts(state.end_ts)} UTC")
    print(
        f"  Profile: {profile.name} source={profile.signal_source} mode={profile.trade_mode} "
        f"wallets={len(selected_wallets)} lb={profile.lookback_sec}s pop>={profile.pop_threshold:.2f} "
        f"hold={profile.hold_sec}s lat={profile.latency_sec}s cap={profile.entry_price_cap}"
    )

    try:
        await ws.resubscribe(token_ids)
    except Exception:
        pass

    burst_pending = False
    last_status_print = 0
    stop_flag = asyncio.Event()

    async def ws_consumer():
        nonlocal burst_pending
        try:
            async for ev in ws.events():
                if stop_flag.is_set():
                    break
                update_ws_price(state, ev)
                _record_ws_price_point(state, time.time())
                if record_ws_trade_event(state, ev):
                    burst_pending = True
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

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

            should_poll = False
            trigger_reason = ""
            if burst_pending:
                should_poll = True
                burst_pending = False
                trigger_reason = "BURST"
            elif now_real - state.last_http_poll_ts >= BASELINE_POLL_INTERVAL:
                should_poll = True
                trigger_reason = "baseline"

            if should_poll:
                new_trades = await poll_http_trades(data_api, state, triggered_by_burst=(trigger_reason == "BURST"))
                if new_trades > 0 or trigger_reason == "BURST" or state.profile.signal_source == "price":
                    _scan_for_new_pending_signal(state, now_real, now_int)
            elif state.profile.signal_source == "price":
                _scan_for_new_pending_signal(state, now_real, now_int)

            _try_execute_pending_signal(state, now_real, now_int)
            _maybe_exit_position(state, now_real, now_int)

            if now_int - last_status_print >= 10:
                last_status_print = now_int
                smart_count = sum(1 for trade in state.http_trades if trade.get("proxyWallet") in state.selected_wallets)
                pos_str = "FLAT" if state.position is None else f"{state.position.side}@{state.position.entry_price:.3f}"
                pending_str = "N"
                if state.pending_signal:
                    pending_str = f"Y/{state.pending_signal.crowd_side}"
                offset = now_int - state.start_ts
                print(
                    f"    [{_fmt_ts(now_int)}] t+{offset:3d}s ws={state.ws_events_count:4d} http={len(state.http_trades):4d} "
                    f"smart={smart_count:3d} up={state.ws_latest_up_price:.3f} polls={state.http_poll_count}({state.burst_triggered_polls}brst) "
                    f"pending={pending_str} pos={pos_str} rPnL=${state.realized_pnl:+.2f}"
                )

            await asyncio.sleep(MAIN_LOOP_INTERVAL)
    finally:
        stop_flag.set()
        ws_consumer_task.cancel()
        try:
            await ws_consumer_task
        except Exception:
            pass

    if state.position is not None:
        held = max(0, int(min(time.time(), state.end_ts) - state.position.entered_at))
        _close_position(state, get_exit_price(state, state.position.side), "market_close", held, int(time.time()))

    print(f"  Market closed. Polls: {state.http_poll_count} (burst: {state.burst_triggered_polls})  WS events: {state.ws_events_count}")
    summary = summarize_market(state)
    for action in state.actions:
        kind = action["action"]
        if kind == "QUEUE":
            if action.get("signal_source") == "price":
                print(f"    b{action['bucket']:3d}: QUEUE      PRICE {action['crowd_side']} baseline_up={action['baseline_up']}")
            else:
                print(
                    f"    b{action['bucket']:3d}: QUEUE      {action['crowd_side']} "
                    f"(wallets {action.get('yes_wallets', 0)}Y/{action.get('no_wallets', 0)}N, baseline_up={action['baseline_up']})"
                )
        elif kind == "ENTER":
            print(
                f"    b{action['bucket']:3d}: ENTER      {action['side']} @ {action['price']:.2f} "
                f"(crowd={action['crowd_side']} @{action['crowd_price']:.2f}, pop={action['pop']:.2f}, remaining {action['remaining_s']}s, stake=${action['stake_usd']})"
            )
        elif kind == "EXIT":
            print(
                f"    b{action['bucket']:3d}: EXIT       {action['side']} @ {action['price']:.2f} "
                f"(entry={action['entry']:.2f}, realized={action['realized_delta']:+.2f}) — {action['reason']}"
            )
        elif kind.startswith("SKIP"):
            print(f"    b{action['bucket']:3d}: {kind:10s} {action.get('side', '')} — {action.get('reason', '')}")
    return summary


def _required_min_market_remaining(profile: MeanReversionProfile) -> int:
    time_window_requirement = 0
    if profile.max_elapsed_sec is not None:
        time_window_requirement = max(0, FIVE_MIN - profile.max_elapsed_sec)
    return max(
        time_window_requirement,
        profile.min_seconds_remaining,
        profile.lookback_sec + profile.latency_sec + profile.hold_sec + 30,
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description="5-minute BTC paper runner for multi-family microstructure strategies")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--duration-min", type=float, default=200)
    parser.add_argument("--max-cycles", type=int, default=30)
    args = parser.parse_args()

    profile = load_profile(args.profile)
    min_market_remaining = _required_min_market_remaining(profile)

    pool_data = json.loads(open("data/smart_wallets_latest.json", "r").read())
    selected_wallets = resolve_profile_wallets(profile, pool_data)

    print("=" * 90)
    print(f"  BTC 5M PAPER BOT — PROFILE: {profile.name}")
    print("=" * 90)
    print(f"  Duration:     {args.duration_min} minutes")
    print(f"  Max cycles:   {args.max_cycles} markets")
    print(f"  Source/mode:  {profile.signal_source} / {profile.trade_mode}")
    if profile.signal_source == "price":
        print(f"  Price signal: {profile.price_signal_mode}")
    print(f"  Wallets:      {len(selected_wallets)}")
    print(f"  Lookback:     {profile.lookback_sec}s")
    print(f"  Strength:     {profile.min_signal_strength}")
    print(f"  Dominance:    {profile.signal_dominance:.1f}x")
    print(f"  Pop thresh:   {profile.pop_threshold:.2f}")
    print(f"  Crowd band:   {profile.min_crowd_price:.2f} .. {profile.max_crowd_price:.2f}")
    print(f"  Elapsed gate: {profile.min_elapsed_sec}s .. {profile.max_elapsed_sec if profile.max_elapsed_sec is not None else 'end'}")
    print(f"  Entry band:   {profile.entry_price_floor:.2f} .. {profile.entry_price_cap}")
    print(f"  Spread max:   {profile.max_spread:.2f}")
    print(f"  Depth ask/bid:${profile.min_entry_ask_depth_usd:.0f}/${profile.min_exit_bid_depth_usd:.0f}")
    if profile.target_price_abs is not None:
        print(f"  Exit target:  abs {profile.target_price_abs:.2f} / stop -{profile.stop_price_delta:.2f}")
    else:
        print(f"  Exit TP/SL:   +{profile.target_price_delta:.2f} / -{profile.stop_price_delta:.2f}")
    print(f"  Hold:         {profile.hold_sec}s")
    print(f"  Min remain:   {min_market_remaining}s")
    print("=" * 90)

    gamma = GammaClient()
    data_api = DataAPIClient()
    ws = MarketWebSocketClient()
    results: list[dict] = []
    market_count = 0
    session_start = time.time()
    session_end = session_start + args.duration_min * 60
    try:
        while time.time() < session_end:
            market = await fetch_current_market(gamma, min_remaining=min_market_remaining)
            if not market:
                await asyncio.sleep(5)
                continue
            remaining = market["_slug_ts"] + FIVE_MIN - time.time()
            if remaining < min_market_remaining:
                await asyncio.sleep(3)
                continue
            if results and results[-1].get("slug") == market.get("slug"):
                await asyncio.sleep(5)
                continue
            market_count += 1
            print(f"\n========== Market {market_count} ==========")
            result = await trade_one_market_meanrev(gamma, data_api, ws, market, selected_wallets, args.profile)
            results.append(result)
            if args.max_cycles > 0 and market_count >= args.max_cycles:
                print(f"\n  Reached max cycles ({args.max_cycles}) — exiting loop.")
                break

        print("\n" + "=" * 90)
        print("  SESSION COMPLETE")
        print("=" * 90)
        duration = time.time() - session_start
        traded = [r for r in results if r.get("traded")]
        total_pnl = round(sum(r["pnl"] for r in traded), 2)
        wins = sum(1 for r in traded if r["pnl"] > 0)
        losses = sum(1 for r in traded if r["pnl"] < 0)
        flats = len(results) - wins - losses
        decided = wins + losses
        print(f"  Duration: {duration/60:.1f} min")
        print(f"  Markets attempted: {len(results)}")
        print(f"  Markets traded: {len(traded)}")
        print(f"  Total P&L: ${total_pnl:+.2f}")
        print(f"  Wins / Losses / Flats: {wins} / {losses} / {flats}")
        if decided > 0:
            print(f"  Decided win rate: {wins}/{decided} = {wins/decided*100:.2f}%")
        print(f"\n  Per-market breakdown:")
        for result in results:
            if result.get("traded"):
                marker = "WIN " if result["pnl"] > 0 else ("LOSS" if result["pnl"] < 0 else "FLAT")
                print(f"    {marker}  {result['slug']}: ${result['pnl']:+.2f}  bursts={result['burst_triggered_polls']}")
            else:
                print(f"    FLAT  {result['slug']}: $+0.00  bursts={result['burst_triggered_polls']}")
        print("=" * 90)
    finally:
        await ws.close()
        await gamma.close()
        await data_api.close()


if __name__ == "__main__":
    asyncio.run(main())
