from __future__ import annotations

import json

from polymarket.trading import mean_reversion_profiles as profiles
from polymarket.trading.mean_reversion_profiles import load_profile_catalog, load_profile


def test_load_profile_catalog_returns_expected_top_and_next_round_profiles():
    catalog = load_profile_catalog()

    assert set(catalog) >= {
        "top10_selective_cap25",
        "top10_balanced_cap30",
        "top20_sample_cap30",
        "price_fade_extreme_v1",
        "leader10_early_follow_v1",
        "all42_late_extreme_fade_v1",
        "cheap_bounce_touch_v1",
        "cheap_bounce_touch_v2",
        "price_fade_24h_v1",
        "cheap_touch_24h_v1",
        "leader10_follow_24h_v1",
        "leader10_follow_dense_v1",
        "early10_follow_dense_v1",
        "double_touch_3dopt_v1",
        "double_touch_dense_v1",
        "double_touch_loose_v1",
        "price_fade_cheap80_cap20_v1",
        "price_fade_cheap100_cap20_v1",
        "price_fade_early92_cap20_v1",
        "price_fade_74_92_cap18_hold30_v1",
        "cheap_touch_74_94_cap22_v1",
        "cheap_touch_74_94_cap20_tgt22_hold10_end90_v1",
        "cheap_touch_72_94_cap20_tgt21_hold10_end90_v1",
        "cheap_touch_80_92_cap20_tgt22_hold10_end90_v1",
        "cheap_touch_76_90_cap18_tgt22_hold10_end90_v1",
        "cheap_touch_76_90_cap18_tgt22_hold10_end90_depth150_v1",
        "double_touch_touch0.84_dl10_ext0.04_cap0.18_hold10_v1",
        "double_touch_touch0.84_dl14_ext0.04_cap0.18_hold10_v1",
        "tail_ladder_conservative_v1",
        "tail_ladder_aggressive_v1",
        "tail_ladder_micro_1235_tp20_fast_v1",
        "tail_ladder_hybrid_5_8_10_13_tp15_fast_v1",
        "tail_ladder_merged_band_07_23_tp05_cap23_fast_v1",
    }
    assert catalog["top10_selective_cap25"]["wallet_set"] == "top10"
    assert catalog["top10_selective_cap25"]["entry_price_cap"] == 0.25
    assert catalog["top20_sample_cap30"]["hold_sec"] == 50


def test_load_profile_returns_price_fade_profile_with_strategy_and_realism_fields():
    profile = load_profile("price_fade_extreme_v1")

    assert profile.name == "price_fade_extreme_v1"
    assert profile.signal_source == "price"
    assert profile.trade_mode == "fade"
    assert profile.wallet_set is None
    assert profile.lookback_sec == 10
    assert profile.pop_threshold == 0.04
    assert profile.min_elapsed_sec == 20
    assert profile.max_elapsed_sec == 120
    assert profile.min_crowd_price == 0.80
    assert profile.max_crowd_price == 1.0
    assert profile.entry_price_floor == 0.05
    assert profile.entry_price_cap == 0.25
    assert profile.target_price_delta == 0.05
    assert profile.stop_price_delta == 0.04


def test_load_profile_returns_leader_follow_profile_with_explicit_wallets():
    profile = load_profile("leader10_early_follow_v1")

    assert profile.signal_source == "wallets"
    assert profile.trade_mode == "follow"
    assert profile.wallet_set is None
    assert len(profile.explicit_wallets) == 10
    assert profile.min_signal_strength == 3
    assert profile.signal_dominance == 2.0
    assert profile.max_elapsed_sec == 40
    assert profile.min_crowd_price == 0.35
    assert profile.max_crowd_price == 0.55
    assert profile.target_price_delta == 0.03
    assert profile.stop_price_delta == 0.03


def test_load_profile_returns_threshold_touch_cheap_bounce_profile():
    profile = load_profile("cheap_bounce_touch_v1")

    assert profile.signal_source == "price"
    assert profile.trade_mode == "fade"
    assert profile.price_signal_mode == "threshold_touch"
    assert profile.lookback_sec == 0
    assert profile.entry_price_floor == 0.05
    assert profile.entry_price_cap == 0.15
    assert profile.max_elapsed_sec == 120
    assert profile.min_crowd_price == 0.85
    assert profile.max_crowd_price == 0.95
    assert profile.target_price_abs == 0.25
    assert profile.stop_price_delta == 0.04

    profile_v2 = load_profile("cheap_bounce_touch_v2")
    assert profile_v2.price_signal_mode == "threshold_touch"
    assert profile_v2.entry_price_cap == 0.18
    assert profile_v2.min_crowd_price == 0.82
    assert profile_v2.target_price_abs == 0.25


def test_load_profile_returns_new_24h_shortlist_profiles():
    price_profile = load_profile("price_fade_24h_v1")
    assert price_profile.signal_source == "price"
    assert price_profile.trade_mode == "fade"
    assert price_profile.lookback_sec == 10
    assert price_profile.pop_threshold == 0.04
    assert price_profile.entry_price_cap == 0.20
    assert price_profile.max_elapsed_sec == 120
    assert price_profile.min_crowd_price == 0.80
    assert price_profile.max_crowd_price == 0.95
    assert price_profile.target_price_delta == 0.04
    assert price_profile.stop_price_delta == 0.03

    cheap_profile = load_profile("cheap_touch_24h_v1")
    assert cheap_profile.signal_source == "price"
    assert cheap_profile.trade_mode == "fade"
    assert cheap_profile.price_signal_mode == "threshold_touch"
    assert cheap_profile.entry_price_cap == 0.20
    assert cheap_profile.max_elapsed_sec == 90
    assert cheap_profile.min_crowd_price == 0.80
    assert cheap_profile.max_crowd_price == 0.92
    assert cheap_profile.target_price_abs == 0.22
    assert cheap_profile.stop_price_delta == 0.03

    leader_profile = load_profile("leader10_follow_24h_v1")
    assert leader_profile.signal_source == "wallets"
    assert leader_profile.trade_mode == "follow"
    assert len(leader_profile.explicit_wallets) == 10
    assert leader_profile.lookback_sec == 20
    assert leader_profile.min_signal_strength == 3
    assert leader_profile.signal_dominance == 2.0
    assert leader_profile.max_elapsed_sec == 30
    assert leader_profile.entry_price_cap == 0.50
    assert leader_profile.target_price_delta == 0.03
    assert leader_profile.stop_price_delta == 0.02


def test_load_profile_returns_dense_leader_follow_profile():
    profile = load_profile("leader10_follow_dense_v1")

    assert profile.signal_source == "wallets"
    assert profile.trade_mode == "follow"
    assert len(profile.explicit_wallets) == 10
    assert profile.lookback_sec == 20
    assert profile.min_signal_strength == 2
    assert profile.signal_dominance == 1.5
    assert profile.max_elapsed_sec == 40
    assert profile.entry_price_cap == 0.55
    assert profile.min_crowd_price == 0.30
    assert profile.max_crowd_price == 0.55
    assert profile.target_price_delta == 0.03
    assert profile.stop_price_delta == 0.02


def test_load_profile_returns_dense_early_follow_profile():
    profile = load_profile("early10_follow_dense_v1")

    assert profile.signal_source == "wallets"
    assert profile.trade_mode == "follow"
    assert len(profile.explicit_wallets) == 10
    assert profile.lookback_sec == 20
    assert profile.min_signal_strength == 2
    assert profile.signal_dominance == 2.0
    assert profile.max_elapsed_sec == 40
    assert profile.entry_price_floor == 0.35
    assert profile.entry_price_cap == 0.55
    assert profile.min_crowd_price == 0.35
    assert profile.max_crowd_price == 0.55
    assert profile.target_price_delta == 0.03
    assert profile.stop_price_delta == 0.02


def test_load_profile_returns_double_touch_profile_with_stateful_fields():
    profile = load_profile("double_touch_3dopt_v1")

    assert profile.signal_source == "price"
    assert profile.trade_mode == "fade"
    assert profile.price_signal_mode == "double_touch"
    assert profile.lookback_sec == 0
    assert profile.min_elapsed_sec == 0
    assert profile.max_elapsed_sec == 100
    assert profile.min_crowd_price == 0.82
    assert profile.max_crowd_price == 0.99
    assert profile.entry_price_cap == 0.30
    assert profile.hold_sec == 20
    assert profile.double_touch_crowd_price == 0.82
    assert profile.double_touch_deadline_sec == 100
    assert profile.double_touch_max_extension == 0.04


def test_load_profile_returns_tail_ladder_live_profiles():
    conservative = load_profile("tail_ladder_conservative_v1")
    assert conservative.signal_source == "price"
    assert conservative.trade_mode == "fade"
    assert conservative.price_signal_mode == "threshold_touch"
    assert conservative.entry_price_floor == 0.05
    assert conservative.entry_price_cap == 0.10
    assert conservative.min_crowd_price == 0.90
    assert conservative.max_crowd_price == 0.95
    assert conservative.target_price_abs == 0.12
    assert conservative.hold_sec == 120
    assert conservative.target_price_delta == 999.0
    assert conservative.stop_price_delta == 999.0

    aggressive = load_profile("tail_ladder_aggressive_v1")
    assert aggressive.signal_source == "price"
    assert aggressive.trade_mode == "fade"
    assert aggressive.price_signal_mode == "threshold_touch"
    assert aggressive.entry_price_floor == 0.08
    assert aggressive.entry_price_cap == 0.13
    assert aggressive.min_crowd_price == 0.87
    assert aggressive.max_crowd_price == 0.92
    assert aggressive.target_price_abs == 0.15
    assert aggressive.hold_sec == 20
    assert aggressive.target_price_delta == 999.0
    assert aggressive.stop_price_delta == 999.0

    micro = load_profile("tail_ladder_micro_235_tp20_fast_v1")
    assert micro.signal_source == "price"
    assert micro.trade_mode == "fade"
    assert micro.price_signal_mode == "threshold_touch"
    assert micro.entry_price_floor == 0.02
    assert micro.entry_price_cap == 0.05
    assert micro.min_crowd_price == 0.95
    assert micro.max_crowd_price == 0.98
    assert micro.target_price_abs == 0.20
    assert micro.hold_sec == 20
    assert micro.position_size_usd == 50.0
    assert micro.min_entry_ask_depth_usd == 150.0
    assert micro.min_exit_bid_depth_usd == 100.0
    assert micro.target_price_delta == 999.0
    assert micro.stop_price_delta == 999.0

    micro_1235 = load_profile("tail_ladder_micro_1235_tp20_fast_v1")
    assert micro_1235.entry_price_floor == 0.01
    assert micro_1235.entry_price_cap == 0.05
    assert micro_1235.min_crowd_price == 0.95
    assert micro_1235.max_crowd_price == 0.99
    assert micro_1235.target_price_abs == 0.20
    assert micro_1235.position_size_usd == 50.0
    assert micro_1235.min_entry_ask_depth_usd == 150.0
    assert micro_1235.min_exit_bid_depth_usd == 100.0

    hybrid = load_profile("tail_ladder_hybrid_5_8_10_13_tp15_fast_v1")
    assert hybrid.signal_source == "price"
    assert hybrid.trade_mode == "fade"
    assert hybrid.price_signal_mode == "threshold_touch"
    assert hybrid.entry_price_floor == 0.05
    assert hybrid.entry_price_cap == 0.13
    assert hybrid.min_crowd_price == 0.87
    assert hybrid.max_crowd_price == 0.95
    assert hybrid.target_price_abs == 0.15
    assert hybrid.hold_sec == 20
    assert hybrid.position_size_usd == 50.0
    assert hybrid.min_entry_ask_depth_usd == 150.0
    assert hybrid.min_exit_bid_depth_usd == 100.0
    assert hybrid.target_price_delta == 999.0
    assert hybrid.stop_price_delta == 999.0


def test_load_profile_returns_merged_band_fast_tp_profiles():
    merged_tp03 = load_profile("tail_ladder_merged_band_07_21_tp03_fast_v1")
    assert merged_tp03.signal_source == "price"
    assert merged_tp03.trade_mode == "fade"
    assert merged_tp03.price_signal_mode == "threshold_touch"
    assert merged_tp03.entry_price_floor == 0.07
    assert merged_tp03.entry_price_cap == 0.21
    assert merged_tp03.min_crowd_price == 0.79
    assert merged_tp03.max_crowd_price == 0.93
    assert merged_tp03.hold_sec == 20
    assert merged_tp03.position_size_usd == 50.0
    assert merged_tp03.min_entry_ask_depth_usd == 150.0
    assert merged_tp03.min_exit_bid_depth_usd == 100.0
    assert merged_tp03.target_price_delta == 0.03
    assert merged_tp03.target_price_abs is None
    assert merged_tp03.stop_price_delta == 999.0

    merged_tp05 = load_profile("tail_ladder_merged_band_07_21_tp05_fast_v1")
    assert merged_tp05.signal_source == "price"
    assert merged_tp05.trade_mode == "fade"
    assert merged_tp05.price_signal_mode == "threshold_touch"
    assert merged_tp05.entry_price_floor == 0.07
    assert merged_tp05.entry_price_cap == 0.21
    assert merged_tp05.min_crowd_price == 0.79
    assert merged_tp05.max_crowd_price == 0.93
    assert merged_tp05.hold_sec == 20
    assert merged_tp05.position_size_usd == 50.0
    assert merged_tp05.min_entry_ask_depth_usd == 150.0
    assert merged_tp05.min_exit_bid_depth_usd == 100.0
    assert merged_tp05.target_price_delta == 0.05
    assert merged_tp05.target_price_abs is None
    assert merged_tp05.stop_price_delta == 999.0

    cap23 = load_profile("tail_ladder_merged_band_07_21_tp05_cap23_fast_v1")
    assert cap23.entry_price_floor == 0.07
    assert cap23.entry_price_cap == 0.23
    assert cap23.min_entry_ask_depth_usd == 150.0
    assert cap23.min_exit_bid_depth_usd == 100.0
    assert cap23.target_price_delta == 0.05

    cap23_tp03 = load_profile("tail_ladder_merged_band_07_21_tp03_cap23_fast_v1")
    assert cap23_tp03.entry_price_floor == 0.07
    assert cap23_tp03.entry_price_cap == 0.23
    assert cap23_tp03.min_crowd_price == 0.77
    assert cap23_tp03.max_crowd_price == 0.93
    assert cap23_tp03.min_entry_ask_depth_usd == 150.0
    assert cap23_tp03.min_exit_bid_depth_usd == 100.0
    assert cap23_tp03.target_price_delta == 0.03

    depth100 = load_profile("tail_ladder_merged_band_07_21_tp05_depth100_fast_v1")
    assert depth100.entry_price_floor == 0.07
    assert depth100.entry_price_cap == 0.21
    assert depth100.min_entry_ask_depth_usd == 100.0
    assert depth100.min_exit_bid_depth_usd == 100.0
    assert depth100.target_price_delta == 0.05

    merged_0723_cap23 = load_profile("tail_ladder_merged_band_07_23_tp05_cap23_fast_v1")
    assert merged_0723_cap23.entry_price_floor == 0.07
    assert merged_0723_cap23.entry_price_cap == 0.23
    assert merged_0723_cap23.min_crowd_price == 0.77
    assert merged_0723_cap23.max_crowd_price == 0.93
    assert merged_0723_cap23.min_entry_ask_depth_usd == 150.0
    assert merged_0723_cap23.min_exit_bid_depth_usd == 100.0
    assert merged_0723_cap23.target_price_delta == 0.05


def test_load_profile_returns_density_focused_double_touch_profiles():
    dense = load_profile("double_touch_dense_v1")
    assert dense.signal_source == "price"
    assert dense.trade_mode == "fade"
    assert dense.price_signal_mode == "double_touch"
    assert dense.min_crowd_price == 0.82
    assert dense.max_elapsed_sec == 120
    assert dense.entry_price_cap == 0.30
    assert dense.hold_sec == 20
    assert dense.double_touch_crowd_price == 0.82
    assert dense.double_touch_deadline_sec == 120
    assert dense.double_touch_max_extension == 0.04

    loose = load_profile("double_touch_loose_v1")
    assert loose.signal_source == "price"
    assert loose.trade_mode == "fade"
    assert loose.price_signal_mode == "double_touch"
    assert loose.min_crowd_price == 0.78
    assert loose.max_elapsed_sec == 120
    assert loose.entry_price_cap == 0.30
    assert loose.hold_sec == 20
    assert loose.double_touch_crowd_price == 0.78
    assert loose.double_touch_deadline_sec == 120
    assert loose.double_touch_max_extension == 0.04


def test_load_profile_returns_wave8_price_fade_cheap_profiles():
    tight = load_profile("price_fade_cheap80_cap20_v1")
    assert tight.signal_source == "price"
    assert tight.trade_mode == "fade"
    assert tight.price_signal_mode == "pop"
    assert tight.lookback_sec == 10
    assert tight.pop_threshold == 0.04
    assert tight.hold_sec == 40
    assert tight.min_elapsed_sec == 20
    assert tight.max_elapsed_sec == 80
    assert tight.min_crowd_price == 0.76
    assert tight.max_crowd_price == 0.95
    assert tight.entry_price_cap == 0.20
    assert tight.target_price_delta == 0.03
    assert tight.stop_price_delta == 0.03

    dense = load_profile("price_fade_cheap100_cap20_v1")
    assert dense.signal_source == "price"
    assert dense.trade_mode == "fade"
    assert dense.price_signal_mode == "pop"
    assert dense.lookback_sec == 10
    assert dense.pop_threshold == 0.04
    assert dense.hold_sec == 40
    assert dense.min_elapsed_sec == 20
    assert dense.max_elapsed_sec == 100
    assert dense.min_crowd_price == 0.76
    assert dense.max_crowd_price == 0.95
    assert dense.entry_price_cap == 0.20
    assert dense.target_price_delta == 0.03
    assert dense.stop_price_delta == 0.03


def test_load_profile_returns_wave11_price_fade_early_profile():
    profile = load_profile("price_fade_early92_cap20_v1")
    assert profile.signal_source == "price"
    assert profile.trade_mode == "fade"
    assert profile.price_signal_mode == "pop"
    assert profile.lookback_sec == 8
    assert profile.pop_threshold == 0.03
    assert profile.hold_sec == 30
    assert profile.min_elapsed_sec == 20
    assert profile.max_elapsed_sec == 80
    assert profile.min_crowd_price == 0.74
    assert profile.max_crowd_price == 0.92
    assert profile.entry_price_cap == 0.20
    assert profile.target_price_delta == 0.03
    assert profile.stop_price_delta == 0.03


def test_load_profile_returns_wave16_price_fade_and_threshold_touch_profiles():
    price = load_profile("price_fade_74_92_cap18_hold30_v1")
    assert price.signal_source == "price"
    assert price.trade_mode == "fade"
    assert price.price_signal_mode == "pop"
    assert price.lookback_sec == 8
    assert price.pop_threshold == 0.03
    assert price.hold_sec == 30
    assert price.min_elapsed_sec == 20
    assert price.max_elapsed_sec == 80
    assert price.min_crowd_price == 0.74
    assert price.max_crowd_price == 0.92
    assert price.entry_price_cap == 0.18
    assert price.target_price_delta == 0.03
    assert price.stop_price_delta == 0.03

    cheap = load_profile("cheap_touch_72_94_cap20_tgt21_hold10_end90_v1")
    assert cheap.signal_source == "price"
    assert cheap.trade_mode == "fade"
    assert cheap.price_signal_mode == "threshold_touch"
    assert cheap.hold_sec == 10
    assert cheap.min_elapsed_sec == 0
    assert cheap.max_elapsed_sec == 90
    assert cheap.min_crowd_price == 0.72
    assert cheap.max_crowd_price == 0.94
    assert cheap.entry_price_cap == 0.20
    assert cheap.target_price_abs == 0.21
    assert cheap.stop_price_delta == 0.03


def test_load_profile_returns_wave18_executable_first_profiles():
    price = load_profile("price_fade_74_92_cap16_hold20_d04_v1")
    assert price.signal_source == "price"
    assert price.trade_mode == "fade"
    assert price.price_signal_mode == "pop"
    assert price.lookback_sec == 8
    assert price.pop_threshold == 0.04
    assert price.hold_sec == 20
    assert price.min_elapsed_sec == 20
    assert price.max_elapsed_sec == 80
    assert price.min_crowd_price == 0.74
    assert price.max_crowd_price == 0.92
    assert price.entry_price_cap == 0.16
    assert price.target_price_delta == 0.03
    assert price.stop_price_delta == 0.03

    cheap = load_profile("cheap_touch_84_94_cap18_tgt20_hold20_end90_v1")
    assert cheap.signal_source == "price"
    assert cheap.trade_mode == "fade"
    assert cheap.price_signal_mode == "threshold_touch"
    assert cheap.hold_sec == 20
    assert cheap.min_elapsed_sec == 0
    assert cheap.max_elapsed_sec == 90
    assert cheap.min_crowd_price == 0.84
    assert cheap.max_crowd_price == 0.94
    assert cheap.entry_price_cap == 0.18
    assert cheap.target_price_abs == 0.20
    assert cheap.stop_price_delta == 0.03

    wave24 = load_profile("cheap_touch_80_92_cap20_tgt22_hold10_end90_v1")
    assert wave24.signal_source == "price"
    assert wave24.trade_mode == "fade"
    assert wave24.price_signal_mode == "threshold_touch"
    assert wave24.hold_sec == 10
    assert wave24.min_elapsed_sec == 0
    assert wave24.max_elapsed_sec == 90
    assert wave24.min_crowd_price == 0.80
    assert wave24.max_crowd_price == 0.92
    assert wave24.entry_price_cap == 0.20
    assert wave24.target_price_abs == 0.22
    assert wave24.stop_price_delta == 0.03

    wave25 = load_profile("cheap_touch_76_90_cap18_tgt22_hold10_end90_v1")
    assert wave25.signal_source == "price"
    assert wave25.trade_mode == "fade"
    assert wave25.price_signal_mode == "threshold_touch"
    assert wave25.hold_sec == 10
    assert wave25.min_elapsed_sec == 0
    assert wave25.max_elapsed_sec == 90
    assert wave25.min_crowd_price == 0.76
    assert wave25.max_crowd_price == 0.90
    assert wave25.entry_price_cap == 0.18
    assert wave25.target_price_abs == 0.22
    assert wave25.stop_price_delta == 0.03

    depth150 = load_profile("cheap_touch_76_90_cap18_tgt22_hold10_end90_depth150_v1")
    assert depth150.signal_source == "price"
    assert depth150.trade_mode == "fade"
    assert depth150.price_signal_mode == "threshold_touch"
    assert depth150.hold_sec == 10
    assert depth150.min_elapsed_sec == 0
    assert depth150.max_elapsed_sec == 90
    assert depth150.min_crowd_price == 0.76
    assert depth150.max_crowd_price == 0.90
    assert depth150.entry_price_cap == 0.18
    assert depth150.min_entry_ask_depth_usd == 150.0
    assert depth150.min_exit_bid_depth_usd == 150.0
    assert depth150.target_price_abs == 0.22
    assert depth150.stop_price_delta == 0.03


def test_load_profile_returns_wave12_threshold_touch_and_double_touch_benchmarks():
    cheap = load_profile("cheap_touch_74_94_cap22_v1")
    assert cheap.signal_source == "price"
    assert cheap.trade_mode == "fade"
    assert cheap.price_signal_mode == "threshold_touch"
    assert cheap.min_elapsed_sec == 0
    assert cheap.max_elapsed_sec == 120
    assert cheap.min_crowd_price == 0.74
    assert cheap.max_crowd_price == 0.94
    assert cheap.entry_price_cap == 0.22
    assert cheap.target_price_abs == 0.23
    assert cheap.stop_price_delta == 0.03

    cheaper = load_profile("cheap_touch_74_94_cap20_tgt22_hold10_end90_v1")
    assert cheaper.signal_source == "price"
    assert cheaper.trade_mode == "fade"
    assert cheaper.price_signal_mode == "threshold_touch"
    assert cheaper.min_elapsed_sec == 0
    assert cheaper.max_elapsed_sec == 90
    assert cheaper.min_crowd_price == 0.74
    assert cheaper.max_crowd_price == 0.94
    assert cheaper.entry_price_cap == 0.20
    assert cheaper.target_price_abs == 0.22
    assert cheaper.hold_sec == 10
    assert cheaper.stop_price_delta == 0.03

    newer_double_touch = load_profile("double_touch_touch0.84_dl10_ext0.04_cap0.18_hold10_v1")
    assert newer_double_touch.signal_source == "price"
    assert newer_double_touch.trade_mode == "fade"
    assert newer_double_touch.price_signal_mode == "double_touch"
    assert newer_double_touch.min_crowd_price == 0.84
    assert newer_double_touch.max_elapsed_sec == 100
    assert newer_double_touch.entry_price_cap == 0.18
    assert newer_double_touch.hold_sec == 10
    assert newer_double_touch.double_touch_crowd_price == 0.84
    assert newer_double_touch.double_touch_deadline_sec == 100
    assert newer_double_touch.double_touch_max_extension == 0.04

    double_touch = load_profile("double_touch_touch0.84_dl14_ext0.04_cap0.18_hold10_v1")
    assert double_touch.signal_source == "price"
    assert double_touch.trade_mode == "fade"
    assert double_touch.price_signal_mode == "double_touch"
    assert double_touch.min_crowd_price == 0.84
    assert double_touch.max_elapsed_sec == 140
    assert double_touch.entry_price_cap == 0.18
    assert double_touch.hold_sec == 10
    assert double_touch.double_touch_crowd_price == 0.84
    assert double_touch.double_touch_deadline_sec == 140
    assert double_touch.double_touch_max_extension == 0.04


def test_load_profile_applies_defaults_for_optional_fields(tmp_path, monkeypatch):
    profile_path = tmp_path / "profiles.json"
    profile_path.write_text(
        json.dumps(
            {
                "tmp_profile": {
                    "signal_source": "price",
                    "trade_mode": "fade",
                    "lookback_sec": 10,
                    "pop_threshold": 0.05,
                    "hold_sec": 60,
                    "latency_sec": 0,
                }
            }
        )
    )
    monkeypatch.setattr(profiles, "PROFILE_FILE", profile_path)

    profile = load_profile("tmp_profile")

    assert profile.wallet_set is None
    assert profile.explicit_wallets == ()
    assert profile.min_signal_strength == 0
    assert profile.signal_dominance == 1.0
    assert profile.entry_price_floor == 0.0
    assert profile.entry_price_cap is None
    assert profile.position_size_usd == 60.0
    assert profile.fee_pct == 0.02
    assert profile.min_elapsed_sec == 0
    assert profile.max_elapsed_sec is None
    assert profile.min_crowd_price == 0.0
    assert profile.max_crowd_price == 1.0
    assert profile.min_seconds_remaining == 0
    assert profile.max_burst_age_sec == 99999.0
    assert profile.max_spread == 1.0
    assert profile.min_entry_ask_depth_usd == 0.0
    assert profile.min_exit_bid_depth_usd == 0.0
    assert profile.depth_window == 0.05
    assert profile.target_price_delta == 999.0
    assert profile.target_price_abs is None
    assert profile.stop_price_delta == 999.0
