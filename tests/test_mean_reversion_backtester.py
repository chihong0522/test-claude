from __future__ import annotations

from polymarket.backtester.mean_reversion import (
    MeanReversionConfig,
    backtest_mean_reversion,
    search_mean_reversion_configs,
    simulate_mean_reversion_market,
)


START_TS = 1_735_689_600  # 2025-01-01 00:00:00 UTC
END_DATE = "2025-01-01T00:05:00Z"
SMART_WALLETS = {"wallet-a", "wallet-b", "wallet-c", "wallet-d"}


def _market(condition_id: str, winning_index: int = 1) -> dict:
    return {
        "condition_id": condition_id,
        "end_date": END_DATE,
        "resolved": True,
        "winning_index": winning_index,
        "title": f"BTC 5m {condition_id}",
    }


def _trade(timestamp: int, price: float, wallet: str, outcome_index: int = 0) -> dict:
    return {
        "timestamp": timestamp,
        "price": price,
        "proxyWallet": wallet,
        "side": "BUY",
        "outcomeIndex": outcome_index,
    }


def test_simulate_mean_reversion_market_enters_opposite_side_and_books_profit_after_reversion():
    market = _market("cid-1")
    trades = [
        _trade(START_TS + 1, 0.50, "noise-wallet"),
        _trade(START_TS + 12, 0.80, "wallet-a"),
        _trade(START_TS + 13, 0.80, "wallet-b"),
        _trade(START_TS + 14, 0.80, "wallet-c"),
        _trade(START_TS + 15, 0.80, "wallet-d"),
        _trade(START_TS + 41, 0.10, "noise-wallet"),
    ]

    trade = simulate_mean_reversion_market(
        market,
        trades,
        MeanReversionConfig(
            bucket_sec=10,
            lookback_sec=10,
            min_signal_strength=4,
            signal_dominance=3.0,
            pop_threshold=0.04,
            hold_sec=30,
            latency_sec=0,
            entry_price_cap=0.30,
            position_size_usd=60.0,
            fee_pct=0.02,
        ),
        smart_wallets=SMART_WALLETS,
    )

    assert trade is not None
    assert trade.signal_side == "YES"
    assert trade.position_side == "NO"
    assert round(trade.entry_price, 4) == 0.20
    assert round(trade.exit_price, 4) == 0.90
    assert round(trade.pop_amount, 4) == 0.30
    assert round(trade.pnl, 2) == 203.40
    assert trade.is_win is True


def test_simulate_mean_reversion_market_respects_entry_price_cap():
    market = _market("cid-2")
    trades = [
        _trade(START_TS + 1, 0.50, "noise-wallet"),
        _trade(START_TS + 12, 0.65, "wallet-a"),
        _trade(START_TS + 13, 0.65, "wallet-b"),
        _trade(START_TS + 14, 0.65, "wallet-c"),
        _trade(START_TS + 15, 0.65, "wallet-d"),
        _trade(START_TS + 41, 0.10, "noise-wallet"),
    ]

    trade = simulate_mean_reversion_market(
        market,
        trades,
        MeanReversionConfig(
            bucket_sec=10,
            lookback_sec=10,
            min_signal_strength=4,
            signal_dominance=3.0,
            pop_threshold=0.04,
            hold_sec=30,
            latency_sec=0,
            entry_price_cap=0.25,
        ),
        smart_wallets=SMART_WALLETS,
    )

    assert trade is None


def test_backtest_and_search_mean_reversion_configs_summarize_and_rank_candidates():
    markets = [_market("cid-win"), _market("cid-loss")]
    trades_by_market = {
        "cid-win": [
            _trade(START_TS + 1, 0.50, "noise-wallet"),
            _trade(START_TS + 12, 0.80, "wallet-a"),
            _trade(START_TS + 13, 0.80, "wallet-b"),
            _trade(START_TS + 14, 0.80, "wallet-c"),
            _trade(START_TS + 15, 0.80, "wallet-d"),
            _trade(START_TS + 41, 0.10, "noise-wallet"),
        ],
        "cid-loss": [
            _trade(START_TS + 1, 0.50, "noise-wallet"),
            _trade(START_TS + 12, 0.80, "wallet-a"),
            _trade(START_TS + 13, 0.80, "wallet-b"),
            _trade(START_TS + 14, 0.80, "wallet-c"),
            _trade(START_TS + 15, 0.80, "wallet-d"),
            _trade(START_TS + 41, 0.85, "noise-wallet"),
        ],
    }

    baseline = MeanReversionConfig(
        name="hold-30",
        bucket_sec=10,
        lookback_sec=10,
        min_signal_strength=4,
        signal_dominance=3.0,
        pop_threshold=0.04,
        hold_sec=30,
        latency_sec=0,
        entry_price_cap=0.30,
        position_size_usd=60.0,
        fee_pct=0.02,
    )
    strict = MeanReversionConfig(
        name="too-strict",
        bucket_sec=10,
        lookback_sec=10,
        min_signal_strength=4,
        signal_dominance=3.0,
        pop_threshold=0.04,
        hold_sec=30,
        latency_sec=0,
        entry_price_cap=0.15,
        position_size_usd=60.0,
        fee_pct=0.02,
    )

    summary = backtest_mean_reversion(
        markets,
        trades_by_market,
        baseline,
        smart_wallets=SMART_WALLETS,
        wallet_set_name="top-4",
    )

    assert summary.wallet_set_name == "top-4"
    assert summary.markets_evaluated == 2
    assert summary.trades_taken == 2
    assert summary.wins == 1
    assert summary.losses == 1
    assert round(summary.win_rate, 1) == 50.0
    assert round(summary.total_pnl, 2) == 186.30

    ranked = search_mean_reversion_configs(
        markets,
        trades_by_market,
        configs=[baseline, strict],
        wallet_sets={"top-4": SMART_WALLETS},
    )

    assert [result.config.name for result in ranked] == ["hold-30", "too-strict"]
    assert ranked[0].trades_taken == 2
    assert ranked[1].trades_taken == 0
