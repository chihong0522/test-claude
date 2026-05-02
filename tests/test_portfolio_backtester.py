from __future__ import annotations

import pytest

from polymarket.backtester.portfolio import (
    allocate_capital,
    derive_wallet_tier_weight,
    filter_trades_to_market_window,
    select_wallet_rows,
)


@pytest.mark.parametrize(
    ("wallet_row", "expected"),
    [
        ({"oos_participations": 10, "oos_accuracy": 0.81, "signal_time_accuracy": 0.55}, 2.0),
        ({"oos_participations": 10, "oos_accuracy": 0.59, "signal_time_accuracy": 0.95}, 0.5),
        ({"oos_participations": 3, "oos_accuracy": 0.10, "signal_time_accuracy": 0.83}, 2.0),
        ({"oos_participations": 3, "oos_accuracy": 0.95, "signal_time_accuracy": 0.58}, 0.5),
        ({"oos_participations": 10, "oos_accuracy": 0.72, "signal_time_accuracy": 0.72}, 1.0),
    ],
)
def test_derive_wallet_tier_weight(wallet_row, expected):
    assert derive_wallet_tier_weight(wallet_row) == expected


def test_select_wallet_rows_elite_filters_to_two_x_wallets_and_keeps_rank_order():
    pool = {
        "wallets": [
            {"wallet": "wallet-a", "oos_participations": 10, "oos_accuracy": 0.85, "signal_time_accuracy": 0.7},
            {"wallet": "wallet-b", "oos_participations": 10, "oos_accuracy": 0.70, "signal_time_accuracy": 0.7},
            {"wallet": "wallet-c", "oos_participations": 2, "oos_accuracy": 0.30, "signal_time_accuracy": 0.83},
        ]
    }

    selected = select_wallet_rows(pool, wallet_set="elite", top_n=10)

    assert [row["wallet"] for row in selected] == ["wallet-a", "wallet-c"]
    assert [row["derived_weight"] for row in selected] == [2.0, 2.0]


def test_select_wallet_rows_top_obeys_top_n():
    pool = {
        "wallets": [
            {"wallet": f"wallet-{i}", "oos_participations": 10, "oos_accuracy": 0.70, "signal_time_accuracy": 0.7}
            for i in range(5)
        ]
    }

    selected = select_wallet_rows(pool, wallet_set="top", top_n=3)

    assert [row["wallet"] for row in selected] == ["wallet-0", "wallet-1", "wallet-2"]


def test_allocate_capital_equal_and_tiered_sum_to_total_capital():
    wallets = [
        {"wallet": "wallet-a", "derived_weight": 2.0},
        {"wallet": "wallet-b", "derived_weight": 1.0},
        {"wallet": "wallet-c", "derived_weight": 0.5},
    ]

    equal_alloc = allocate_capital(wallets, total_capital=350.0, weighting="equal")
    tiered_alloc = allocate_capital(wallets, total_capital=350.0, weighting="tiered")

    assert equal_alloc == {"wallet-a": 116.67, "wallet-b": 116.67, "wallet-c": 116.66}
    assert tiered_alloc == {"wallet-a": 200.0, "wallet-b": 100.0, "wallet-c": 50.0}
    assert round(sum(equal_alloc.values()), 2) == 350.0
    assert round(sum(tiered_alloc.values()), 2) == 350.0


def test_filter_trades_to_market_window_keeps_only_selected_markets_and_timestamps():
    trades = [
        {"conditionId": "cid-1", "timestamp": 100},
        {"conditionId": "cid-2", "timestamp": 150},
        {"conditionId": "cid-1", "timestamp": 250},
        {"conditionId": "cid-3", "timestamp": 180},
    ]

    filtered = filter_trades_to_market_window(
        trades,
        condition_ids={"cid-1", "cid-2"},
        start_ts=120,
        end_ts=220,
    )

    assert filtered == [{"conditionId": "cid-2", "timestamp": 150}]
