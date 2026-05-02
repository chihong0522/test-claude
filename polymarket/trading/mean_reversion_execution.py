from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Side = Literal["YES", "NO"]
BookSide = Literal["ask", "bid"]
TradeMode = Literal["fade", "follow"]


@dataclass(frozen=True)
class MeanReversionConfig:
    signal_dominance: float = 2.0
    min_weighted_signal: float = 3.0
    min_pop_abs: float = 0.08
    min_seconds_remaining: int = 90
    max_burst_age_sec: float = 3.0
    trade_mode: TradeMode = "fade"
    min_entry_price: float = 0.0
    max_entry_price: float = 0.25
    min_crowd_price: float = 0.0
    max_crowd_price: float = 1.0
    max_spread: float = 0.03
    min_entry_ask_depth_usd: float = 250.0
    min_exit_bid_depth_usd: float = 150.0
    depth_window: float = 0.03
    target_price_delta: float = 0.03
    target_price_abs: float | None = None
    stop_price_delta: float = 0.03
    max_hold_sec: int = 30


@dataclass(frozen=True)
class MeanReversionSignal:
    crowd_side: Side
    weighted_yes: float
    weighted_no: float
    anchor_up_price: float
    current_up_price: float
    remaining_s: int
    burst_age_sec: float = 0.0
    trade_mode: TradeMode = "fade"

    @property
    def entry_side(self) -> Side:
        if self.trade_mode == "follow":
            return self.crowd_side
        return "NO" if self.crowd_side == "YES" else "YES"

    @property
    def crowd_price(self) -> float:
        return self.current_up_price if self.crowd_side == "YES" else 1.0 - self.current_up_price

    @property
    def entry_price(self) -> float:
        return self.current_up_price if self.entry_side == "YES" else 1.0 - self.current_up_price

    @property
    def pop_abs(self) -> float:
        if self.crowd_side == "YES":
            return max(0.0, self.current_up_price - self.anchor_up_price)
        return max(0.0, self.anchor_up_price - self.current_up_price)


def build_signal(
    *,
    weighted_yes: float,
    weighted_no: float,
    anchor_up_price: float,
    current_up_price: float,
    remaining_s: int,
    burst_age_sec: float = 0.0,
    crowd_side: Side | None = None,
    config: MeanReversionConfig | None = None,
) -> MeanReversionSignal | None:
    cfg = config or MeanReversionConfig()
    if not 0.0 <= current_up_price <= 1.0:
        return None
    if not 0.0 <= anchor_up_price <= 1.0:
        return None

    resolved_crowd_side = crowd_side
    if resolved_crowd_side is None:
        if (
            weighted_yes >= cfg.min_weighted_signal
            and weighted_yes >= cfg.signal_dominance * max(weighted_no, 1.0)
        ):
            resolved_crowd_side = "YES"
        elif (
            weighted_no >= cfg.min_weighted_signal
            and weighted_no >= cfg.signal_dominance * max(weighted_yes, 1.0)
        ):
            resolved_crowd_side = "NO"

    if resolved_crowd_side is None:
        return None

    return MeanReversionSignal(
        crowd_side=resolved_crowd_side,
        weighted_yes=weighted_yes,
        weighted_no=weighted_no,
        anchor_up_price=anchor_up_price,
        current_up_price=current_up_price,
        remaining_s=remaining_s,
        burst_age_sec=burst_age_sec,
        trade_mode=cfg.trade_mode,
    )


def depth_usd_near_best(levels: list[tuple[float, float]], *, side: BookSide, window: float) -> float:
    if not levels:
        return 0.0
    best_price = levels[0][0]
    total = 0.0
    for price, size in levels:
        if price <= 0 or size <= 0:
            continue
        if side == "ask":
            if price > best_price + window:
                break
        else:
            if price < best_price - window:
                break
        total += price * size
    return total


def should_enter_mean_reversion(
    signal: MeanReversionSignal,
    config: MeanReversionConfig,
    *,
    entry_asks: list[tuple[float, float]],
    entry_bids: list[tuple[float, float]],
    already_traded: bool = False,
    has_open_position: bool = False,
) -> tuple[bool, str]:
    if has_open_position:
        return False, "position_open"
    if already_traded:
        return False, "one_trade_per_market"
    if signal.remaining_s < config.min_seconds_remaining:
        return False, "too_late"
    if signal.burst_age_sec > config.max_burst_age_sec:
        return False, "stale_burst"
    if signal.pop_abs < config.min_pop_abs:
        return False, "pop_too_small"
    if signal.crowd_price < config.min_crowd_price or signal.crowd_price > config.max_crowd_price:
        return False, "crowd_price_out_of_range"
    if not entry_asks or not entry_bids:
        return False, "book_unavailable"

    best_ask = entry_asks[0][0]
    best_bid = entry_bids[0][0]
    if best_ask <= 0 or best_bid <= 0:
        return False, "book_invalid"
    if best_ask < config.min_entry_price:
        return False, "entry_too_cheap"
    if best_ask > config.max_entry_price:
        return False, "entry_too_expensive"

    spread = best_ask - best_bid
    if spread > config.max_spread:
        return False, f"wide_spread {spread:.4f}>{config.max_spread:.4f}"

    ask_depth = depth_usd_near_best(entry_asks, side="ask", window=config.depth_window)
    if ask_depth < config.min_entry_ask_depth_usd:
        return False, f"thin_entry_ask ${ask_depth:.0f}<${config.min_entry_ask_depth_usd:.0f}"

    bid_depth = depth_usd_near_best(entry_bids, side="bid", window=config.depth_window)
    if bid_depth < config.min_exit_bid_depth_usd:
        return False, f"thin_exit_bid ${bid_depth:.0f}<${config.min_exit_bid_depth_usd:.0f}"

    return True, f"enter {signal.entry_side} @ {best_ask:.2f}"


def check_exit(
    *,
    entry_price: float,
    current_bid_price: float,
    seconds_held: int,
    config: MeanReversionConfig,
) -> tuple[bool, str | None]:
    if entry_price <= 0 or current_bid_price <= 0:
        return False, None

    if config.target_price_abs is not None and current_bid_price >= config.target_price_abs:
        return True, "target_abs"

    delta = current_bid_price - entry_price
    if delta >= config.target_price_delta:
        return True, "target"
    if delta <= -config.stop_price_delta:
        return True, "stop"
    if seconds_held >= config.max_hold_sec:
        return True, "time_stop"
    return False, None
