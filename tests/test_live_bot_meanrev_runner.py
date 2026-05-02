from __future__ import annotations

from collections import deque

from polymarket.trading.mean_reversion_profiles import MeanReversionProfile
from scripts.live_bot_meanrev_ws import (
    MeanRevPaperState,
    _required_min_market_remaining,
    _scan_price_pending_signal,
    detect_price_signal_crowd_side,
    detect_price_signal_double_touch_crowd_side,
    detect_price_signal_threshold_touch_crowd_side,
    resolve_profile_wallets,
)


def test_resolve_profile_wallets_uses_explicit_wallets_before_pool_selection():
    profile = MeanReversionProfile(
        name="leader10_early_follow_v1",
        signal_source="wallets",
        trade_mode="follow",
        wallet_set=None,
        explicit_wallets=("wallet-a", "wallet-b"),
        lookback_sec=10,
        min_signal_strength=3,
        signal_dominance=2.0,
        pop_threshold=0.03,
        hold_sec=20,
        latency_sec=0,
        entry_price_floor=0.0,
        entry_price_cap=0.60,
        position_size_usd=60.0,
        fee_pct=0.02,
        min_elapsed_sec=0,
        max_elapsed_sec=40,
        min_crowd_price=0.35,
        max_crowd_price=0.55,
        min_seconds_remaining=0,
        max_burst_age_sec=99999.0,
        max_spread=1.0,
        min_entry_ask_depth_usd=0.0,
        min_exit_bid_depth_usd=0.0,
        depth_window=0.05,
        target_price_delta=0.03,
        stop_price_delta=0.03,
    )

    selected = resolve_profile_wallets(profile, {"wallets": [{"wallet": "other-wallet"}]})

    assert selected == {"wallet-a", "wallet-b"}


def test_resolve_profile_wallets_returns_empty_set_for_price_profiles():
    profile = MeanReversionProfile(
        name="price_fade_extreme_v1",
        signal_source="price",
        trade_mode="fade",
        wallet_set=None,
        explicit_wallets=(),
        lookback_sec=10,
        min_signal_strength=0,
        signal_dominance=1.0,
        pop_threshold=0.04,
        hold_sec=60,
        latency_sec=0,
        entry_price_floor=0.05,
        entry_price_cap=0.25,
        position_size_usd=60.0,
        fee_pct=0.02,
        min_elapsed_sec=20,
        max_elapsed_sec=120,
        min_crowd_price=0.80,
        max_crowd_price=1.0,
        min_seconds_remaining=0,
        max_burst_age_sec=99999.0,
        max_spread=1.0,
        min_entry_ask_depth_usd=0.0,
        min_exit_bid_depth_usd=0.0,
        depth_window=0.05,
        target_price_delta=0.05,
        stop_price_delta=0.04,
    )

    selected = resolve_profile_wallets(profile, {"wallets": [{"wallet": "wallet-a"}]})

    assert selected == set()


def test_detect_price_signal_crowd_side_handles_up_down_and_no_signal_cases():
    assert detect_price_signal_crowd_side(anchor_up_price=0.50, current_up_price=0.56, pop_threshold=0.04) == "YES"
    assert detect_price_signal_crowd_side(anchor_up_price=0.50, current_up_price=0.44, pop_threshold=0.04) == "NO"
    assert detect_price_signal_crowd_side(anchor_up_price=0.50, current_up_price=0.53, pop_threshold=0.04) is None


def test_detect_price_signal_threshold_touch_crowd_side_handles_mirrored_bands():
    assert detect_price_signal_threshold_touch_crowd_side(current_up_price=0.90, min_crowd_price=0.85, max_crowd_price=0.95) == "YES"
    assert detect_price_signal_threshold_touch_crowd_side(current_up_price=0.10, min_crowd_price=0.85, max_crowd_price=0.95) == "NO"
    assert detect_price_signal_threshold_touch_crowd_side(current_up_price=0.50, min_crowd_price=0.85, max_crowd_price=0.95) is None


def test_detect_price_signal_double_touch_crowd_side_requires_two_same_side_touches_with_limited_extension():
    history = [
        (0.0, 0.50),
        (80.0, 0.83),
        (100.0, 0.86),
    ]

    assert detect_price_signal_double_touch_crowd_side(
        price_history=history,
        market_start_ts=0.0,
        touch_price=0.82,
        deadline_sec=100,
        max_extension=0.04,
    ) == "YES"

    assert detect_price_signal_double_touch_crowd_side(
        price_history=[(0.0, 0.50), (80.0, 0.83), (100.0, 0.90)],
        market_start_ts=0.0,
        touch_price=0.82,
        deadline_sec=100,
        max_extension=0.04,
    ) is None


def test_detect_price_signal_double_touch_crowd_side_handles_mirrored_no_side():
    history = [
        (0.0, 0.50),
        (70.0, 0.17),
        (95.0, 0.15),
    ]

    assert detect_price_signal_double_touch_crowd_side(
        price_history=history,
        market_start_ts=0.0,
        touch_price=0.82,
        deadline_sec=100,
        max_extension=0.04,
    ) == "NO"


def test_detect_price_signal_double_touch_crowd_side_requires_latest_point_to_be_the_second_touch():
    history = [
        (0.0, 0.50),
        (80.0, 0.83),
        (90.0, 0.85),
        (100.0, 0.42),
    ]

    assert detect_price_signal_double_touch_crowd_side(
        price_history=history,
        market_start_ts=0.0,
        touch_price=0.82,
        deadline_sec=100,
        max_extension=0.04,
    ) is None


def test_scan_price_pending_signal_for_pop_mode_requires_crowd_band_before_queueing():
    profile = MeanReversionProfile(
        name="price_fade_cheap80_cap20_v1",
        signal_source="price",
        trade_mode="fade",
        wallet_set=None,
        explicit_wallets=(),
        lookback_sec=10,
        min_signal_strength=0,
        signal_dominance=1.0,
        pop_threshold=0.04,
        hold_sec=40,
        latency_sec=0,
        entry_price_floor=0.05,
        entry_price_cap=0.20,
        position_size_usd=60.0,
        fee_pct=0.02,
        min_elapsed_sec=20,
        max_elapsed_sec=80,
        min_crowd_price=0.76,
        max_crowd_price=0.95,
        min_seconds_remaining=180,
        max_burst_age_sec=5.0,
        max_spread=0.03,
        min_entry_ask_depth_usd=250.0,
        min_exit_bid_depth_usd=150.0,
        depth_window=0.03,
        target_price_delta=0.03,
        stop_price_delta=0.03,
    )
    state = MeanRevPaperState(
        slug="btc-updown-5m-test",
        condition_id="cid",
        token_ids=["yes", "no"],
        up_token_id="yes",
        down_token_id="no",
        start_ts=0,
        end_ts=300,
        profile=profile,
        selected_wallets=set(),
        ws_latest_up_price=0.60,
        ws_price_history=deque([(20.0, 0.55), (30.0, 0.60)]),
    )

    _scan_price_pending_signal(state, now_real=30.0, now_int=30)

    assert state.pending_signal is None


def test_required_min_market_remaining_counts_full_hold_time_without_capping():
    profile = MeanReversionProfile(
        name="long_hold_profile",
        signal_source="price",
        trade_mode="fade",
        wallet_set=None,
        explicit_wallets=(),
        lookback_sec=30,
        min_signal_strength=0,
        signal_dominance=1.0,
        pop_threshold=0.0,
        hold_sec=120,
        latency_sec=20,
        entry_price_floor=0.05,
        entry_price_cap=0.30,
        position_size_usd=60.0,
        fee_pct=0.02,
        min_elapsed_sec=0,
        max_elapsed_sec=None,
        min_crowd_price=0.80,
        max_crowd_price=1.0,
        min_seconds_remaining=0,
        max_burst_age_sec=99999.0,
        max_spread=1.0,
        min_entry_ask_depth_usd=0.0,
        min_exit_bid_depth_usd=0.0,
        depth_window=0.05,
        target_price_delta=999.0,
        stop_price_delta=999.0,
    )

    assert _required_min_market_remaining(profile) == 200
