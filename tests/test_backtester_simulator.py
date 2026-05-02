from __future__ import annotations

from polymarket.backtester.simulator import BacktestConfig, run_backtest


def test_run_backtest_settles_open_positions_when_market_outcomes_are_provided():
    trades = [
        {
            "timestamp": 100,
            "side": "BUY",
            "price": 0.20,
            "size": 100.0,
            "conditionId": "cid-1",
            "outcomeIndex": 0,
            "title": "BTC up/down",
        }
    ]

    result = run_backtest(
        trades,
        BacktestConfig(initial_capital=1000.0, position_pct=0.10, max_position_pct=0.10, slippage_bps=0, fee_rate=0.0),
        market_outcomes={"cid-1": 0},
    )

    assert result.final_capital == 1399.25
    assert result.total_return == 39.92
