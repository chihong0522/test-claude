from __future__ import annotations

from polymarket.backtester.tail_ladder import (
    TailLadderConfig,
    backtest_tail_ladder,
    simulate_tail_ladder_market,
)


def _market(*, winning_index: int = 0) -> dict:
    return {
        "condition_id": "cid-1",
        "title": "BTC Up or Down",
        "end_date": "2026-04-30T00:05:00Z",
        "resolved": True,
        "winning_index": winning_index,
    }


def _trade(ts: int, up_price: float) -> dict:
    return {
        "timestamp": ts,
        "outcomeIndex": 0,
        "price": up_price,
    }


START_TS = 1777507200  # 2026-04-30 00:00:00 UTC


def test_touch_fill_exits_at_absolute_target_price() -> None:
    market = _market()
    trades = [
        _trade(START_TS + 30, 0.05),
        _trade(START_TS + 36, 0.09),
        _trade(START_TS + 42, 0.18),
    ]
    config = TailLadderConfig(
        entry_levels=(0.05,),
        target_price_abs=0.15,
        timeout_sec=40,
        min_elapsed_sec=0,
        max_elapsed_sec=120,
        stake_per_level_usd=20.0,
    )

    fills = simulate_tail_ladder_market(market, trades, config)

    assert len(fills) == 1
    assert fills[0].position_side == "YES"
    assert fills[0].entry_price == 0.05
    assert fills[0].exit_reason == "target_abs"
    assert fills[0].exit_price == 0.15


def test_timeout_exit_uses_last_seen_price_before_timeout() -> None:
    market = _market()
    trades = [
        _trade(START_TS + 30, 0.05),
        _trade(START_TS + 42, 0.07),
        _trade(START_TS + 80, 0.20),
    ]
    config = TailLadderConfig(
        entry_levels=(0.05,),
        target_price_abs=0.15,
        timeout_sec=20,
        min_elapsed_sec=0,
        max_elapsed_sec=120,
        stake_per_level_usd=20.0,
    )

    fills = simulate_tail_ladder_market(market, trades, config)

    assert len(fills) == 1
    assert fills[0].exit_reason == "timeout"
    assert fills[0].exit_price == 0.07


def test_multiple_ladder_levels_can_fill_on_same_side() -> None:
    market = _market()
    trades = [
        _trade(START_TS + 30, 0.05),
        _trade(START_TS + 35, 0.03),
        _trade(START_TS + 45, 0.11),
    ]
    config = TailLadderConfig(
        entry_levels=(0.05, 0.03),
        target_price_abs=0.10,
        timeout_sec=30,
        min_elapsed_sec=0,
        max_elapsed_sec=120,
        stake_per_level_usd=20.0,
    )

    fills = simulate_tail_ladder_market(market, trades, config)

    assert [fill.entry_price for fill in fills] == [0.05, 0.03]
    assert all(fill.exit_reason == "target_abs" for fill in fills)
    assert all(fill.exit_price == 0.10 for fill in fills)


def test_relative_target_delta_exit_uses_entry_plus_delta() -> None:
    market = _market()
    trades = [
        _trade(START_TS + 30, 0.07),
        _trade(START_TS + 36, 0.09),
        _trade(START_TS + 42, 0.13),
    ]
    config = TailLadderConfig(
        entry_levels=(0.07,),
        target_price_abs=None,
        timeout_sec=40,
        min_elapsed_sec=0,
        max_elapsed_sec=120,
        stake_per_level_usd=20.0,
        exit_mode="target_delta",
        target_price_delta=0.05,
    )

    fills = simulate_tail_ladder_market(market, trades, config)

    assert len(fills) == 1
    assert fills[0].entry_price == 0.07
    assert fills[0].exit_reason == "target_delta"
    assert fills[0].exit_price == 0.12


def test_resolution_exit_uses_market_winner() -> None:
    market = _market(winning_index=0)
    trades = [
        _trade(START_TS + 30, 0.05),
        _trade(START_TS + 42, 0.04),
    ]
    config = TailLadderConfig(
        entry_levels=(0.05,),
        target_price_abs=None,
        timeout_sec=20,
        min_elapsed_sec=0,
        max_elapsed_sec=120,
        stake_per_level_usd=20.0,
        exit_mode="resolve",
    )

    summary = backtest_tail_ladder([market], {"cid-1": trades}, config)

    assert summary.trades_taken == 1
    assert summary.wins == 1
    assert summary.trade_log[0].exit_reason == "resolve"
    assert summary.trade_log[0].exit_price == 1.0
