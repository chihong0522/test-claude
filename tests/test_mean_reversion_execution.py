from __future__ import annotations

import pytest

from polymarket.trading.mean_reversion_execution import (
    MeanReversionConfig,
    MeanReversionSignal,
    build_signal,
    check_exit,
    should_enter_mean_reversion,
)


def test_build_signal_returns_opposite_entry_side_and_pop():
    signal = build_signal(
        weighted_yes=4.0,
        weighted_no=1.0,
        anchor_up_price=0.58,
        current_up_price=0.82,
        remaining_s=120,
        burst_age_sec=1.0,
    )

    assert signal is not None
    assert signal.crowd_side == "YES"
    assert signal.entry_side == "NO"
    assert signal.crowd_price == pytest.approx(0.82)
    assert signal.entry_price == pytest.approx(0.18)
    assert signal.pop_abs == pytest.approx(0.24)


def test_build_signal_supports_follow_mode_and_explicit_crowd_side():
    cfg = MeanReversionConfig(trade_mode="follow", min_weighted_signal=99.0)

    signal = build_signal(
        weighted_yes=0.0,
        weighted_no=0.0,
        crowd_side="YES",
        anchor_up_price=0.42,
        current_up_price=0.52,
        remaining_s=180,
        burst_age_sec=0.5,
        config=cfg,
    )

    assert signal is not None
    assert signal.crowd_side == "YES"
    assert signal.entry_side == "YES"
    assert signal.entry_price == pytest.approx(0.52)


def test_should_enter_accepts_fast_cheap_two_sided_pop():
    cfg = MeanReversionConfig()
    signal = MeanReversionSignal(
        crowd_side="YES",
        weighted_yes=4.0,
        weighted_no=0.5,
        anchor_up_price=0.68,
        current_up_price=0.80,
        remaining_s=110,
        burst_age_sec=1.2,
        trade_mode="fade",
    )

    ok, reason = should_enter_mean_reversion(
        signal,
        cfg,
        entry_asks=[(0.20, 2000.0)],
        entry_bids=[(0.18, 1200.0), (0.17, 1200.0)],
    )

    assert ok is True
    assert reason.startswith("enter NO @ 0.20")


def test_should_enter_rejects_expensive_entry_even_if_consensus_is_strong():
    cfg = MeanReversionConfig()
    signal = MeanReversionSignal(
        crowd_side="YES",
        weighted_yes=5.0,
        weighted_no=0.0,
        anchor_up_price=0.60,
        current_up_price=0.72,
        remaining_s=140,
        burst_age_sec=0.8,
        trade_mode="fade",
    )

    ok, reason = should_enter_mean_reversion(
        signal,
        cfg,
        entry_asks=[(0.28, 2500.0)],
        entry_bids=[(0.26, 2500.0)],
    )

    assert ok is False
    assert reason == "entry_too_expensive"


def test_should_enter_rejects_entry_too_cheap_when_realism_floor_is_set():
    cfg = MeanReversionConfig(min_entry_price=0.05)
    signal = MeanReversionSignal(
        crowd_side="YES",
        weighted_yes=5.0,
        weighted_no=0.0,
        anchor_up_price=0.84,
        current_up_price=0.96,
        remaining_s=140,
        burst_age_sec=0.8,
        trade_mode="fade",
    )

    ok, reason = should_enter_mean_reversion(
        signal,
        cfg,
        entry_asks=[(0.04, 2500.0)],
        entry_bids=[(0.03, 2500.0)],
    )

    assert ok is False
    assert reason == "entry_too_cheap"


def test_should_enter_rejects_wrong_crowd_price_band_for_follow_mode():
    cfg = MeanReversionConfig(trade_mode="follow", min_crowd_price=0.35, max_crowd_price=0.55, max_entry_price=0.60)
    signal = MeanReversionSignal(
        crowd_side="YES",
        weighted_yes=3.0,
        weighted_no=0.0,
        anchor_up_price=0.40,
        current_up_price=0.70,
        remaining_s=220,
        burst_age_sec=0.4,
        trade_mode="follow",
    )

    ok, reason = should_enter_mean_reversion(
        signal,
        cfg,
        entry_asks=[(0.52, 3000.0)],
        entry_bids=[(0.50, 3000.0)],
    )

    assert ok is False
    assert reason == "crowd_price_out_of_range"


def test_should_enter_requires_tight_spread_and_exit_liquidity():
    cfg = MeanReversionConfig()
    signal = MeanReversionSignal(
        crowd_side="NO",
        weighted_yes=0.0,
        weighted_no=4.0,
        anchor_up_price=0.46,
        current_up_price=0.20,
        remaining_s=130,
        burst_age_sec=0.9,
        trade_mode="fade",
    )

    ok, reason = should_enter_mean_reversion(
        signal,
        cfg,
        entry_asks=[(0.20, 3000.0)],
        entry_bids=[(0.14, 200.0)],
    )

    assert ok is False
    assert reason.startswith("wide_spread") or reason.startswith("thin_exit_bid")


def test_check_exit_triggers_target_stop_and_time_stop():
    cfg = MeanReversionConfig(target_price_delta=0.03, stop_price_delta=0.03, max_hold_sec=30)

    should_exit, reason = check_exit(entry_price=0.18, current_bid_price=0.22, seconds_held=8, config=cfg)
    assert should_exit is True
    assert reason == "target"

    should_exit, reason = check_exit(entry_price=0.18, current_bid_price=0.14, seconds_held=8, config=cfg)
    assert should_exit is True
    assert reason == "stop"

    should_exit, reason = check_exit(entry_price=0.18, current_bid_price=0.19, seconds_held=31, config=cfg)
    assert should_exit is True
    assert reason == "time_stop"


def test_check_exit_supports_absolute_target_price():
    cfg = MeanReversionConfig(target_price_abs=0.25, stop_price_delta=0.04, max_hold_sec=20)

    should_exit, reason = check_exit(entry_price=0.14, current_bid_price=0.25, seconds_held=8, config=cfg)
    assert should_exit is True
    assert reason == "target_abs"
