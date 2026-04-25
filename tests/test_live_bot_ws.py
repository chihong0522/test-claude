from __future__ import annotations

import argparse
import asyncio
import inspect

import pytest

from scripts import live_bot_ws, refresh_smart_wallets


class _CapturedDefaults(Exception):
    pass


def _capture_parser_defaults(monkeypatch: pytest.MonkeyPatch, module_main, target_dest: str):
    captured: dict[str, object] = {}

    def fake_parse_args(self, *args, **kwargs):
        for action in self._actions:
            if action.dest != "help":
                captured[action.dest] = action.default
        raise _CapturedDefaults

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", fake_parse_args)

    with pytest.raises(_CapturedDefaults):
        asyncio.run(module_main())

    return captured[target_dest]


def _make_state(**overrides):
    base = dict(
        slug="btc-updown-5m-test",
        condition_id="cond-1",
        token_ids=["up-token", "down-token"],
        up_token_id="up-token",
        down_token_id="down-token",
        start_ts=0,
        end_ts=300,
        smart_wallets={"wallet-1"},
        smart_wallet_weights={"wallet-1": 1.0},
        min_signal_strength=1,
        signal_dominance=1.0,
        fee_pct=0.0,
        min_book_depth_usd=150.0,
        book_depth_window=0.05,
    )
    base.update(overrides)
    return live_bot_ws.MarketTradingState(**base)


def test_trade_one_market_default_time_gate_matches_live_bot_default():
    sig = inspect.signature(live_bot_ws.trade_one_market)
    assert sig.parameters["min_seconds_remaining"].default == 60


def test_refresh_wallet_selection_default_time_gate_is_60(monkeypatch: pytest.MonkeyPatch):
    default = _capture_parser_defaults(
        monkeypatch,
        refresh_smart_wallets.main,
        "min_seconds_remaining",
    )
    assert default == 60


def test_live_bot_default_book_depth_gate_is_enabled(monkeypatch: pytest.MonkeyPatch):
    default = _capture_parser_defaults(
        monkeypatch,
        live_bot_ws.main,
        "min_book_depth",
    )
    assert default == 150.0


def test_process_voting_enters_at_best_ask_not_midpoint():
    state = _make_state(
        ws_latest_up_price=0.50,
        up_book_asks=[(0.54, 400.0)],
        http_trades=[
            {
                "timestamp": 5,
                "proxyWallet": "wallet-1",
                "side": "BUY",
                "outcomeIndex": 0,
            }
        ],
    )

    live_bot_ws.process_voting(state, now_ts=15)

    assert state.position is not None
    side, entry_price, *_rest = state.position
    assert side == "YES"
    assert entry_price == pytest.approx(0.54)
    assert state.actions[-1]["price"] == pytest.approx(0.54)


def test_execute_exit_uses_best_bid_for_realized_pnl_and_logged_price():
    state = _make_state(
        fee_pct=0.0,
        ws_latest_up_price=0.70,
        up_book_bids=[(0.66, 150.0)],
        position=("YES", 0.40, 10.0, 4.0),
    )

    live_bot_ws.execute_exit(state, now_ts=120, reason="profit_take")

    assert state.exited is True
    assert state.position is None
    assert state.realized_pnl == pytest.approx(2.6)
    assert state.actions[-1]["price"] == pytest.approx(0.66)


def test_flip_rejects_expensive_tier_instead_of_opening_zero_size_position():
    state = _make_state(
        ws_latest_up_price=0.50,
        up_book_asks=[(0.65, 400.0)],
        down_book_bids=[(0.45, 400.0)],
        http_trades=[
            {
                "timestamp": 5,
                "proxyWallet": "wallet-1",
                "side": "BUY",
                "outcomeIndex": 0,
            }
        ],
        position=("NO", 0.30, 10.0, 3.0),
        min_flip_strength=1,
        flip_cooldown_sec=0,
        entry_ws_price=0.20,
        last_position_change_ts=0,
    )

    live_bot_ws.process_voting(state, now_ts=15)

    assert state.position == ("NO", 0.30, 10.0, 3.0)
    assert state.realized_pnl == pytest.approx(0.0)
    assert state.actions[-1]["action"] == "SKIP_TIER"
    assert "expensive tier rejected" in state.actions[-1]["reason"]
