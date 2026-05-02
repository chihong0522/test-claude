from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILE_FILE = REPO_ROOT / "data" / "mean_reversion_strategy_profiles.json"
SignalSource = Literal["wallets", "price"]
TradeMode = Literal["fade", "follow"]
PriceSignalMode = Literal["pop", "threshold_touch", "double_touch"]


@dataclass(frozen=True)
class MeanReversionProfile:
    name: str
    signal_source: SignalSource = "wallets"
    trade_mode: TradeMode = "fade"
    price_signal_mode: PriceSignalMode = "pop"
    wallet_set: str | None = None
    explicit_wallets: tuple[str, ...] = ()
    lookback_sec: int = 10
    min_signal_strength: int = 0
    signal_dominance: float = 1.0
    pop_threshold: float = 0.0
    hold_sec: int = 30
    latency_sec: int = 0
    entry_price_floor: float = 0.0
    entry_price_cap: float | None = None
    position_size_usd: float = 60.0
    fee_pct: float = 0.02
    min_elapsed_sec: int = 0
    max_elapsed_sec: int | None = None
    min_crowd_price: float = 0.0
    max_crowd_price: float = 1.0
    min_seconds_remaining: int = 0
    max_burst_age_sec: float = 99999.0
    max_spread: float = 1.0
    min_entry_ask_depth_usd: float = 0.0
    min_exit_bid_depth_usd: float = 0.0
    depth_window: float = 0.05
    target_price_delta: float = 999.0
    target_price_abs: float | None = None
    stop_price_delta: float = 999.0
    double_touch_crowd_price: float | None = None
    double_touch_deadline_sec: int | None = None
    double_touch_max_extension: float = 0.0


def load_profile_catalog() -> dict[str, dict]:
    if not PROFILE_FILE.exists():
        raise RuntimeError(f"Missing mean-reversion profile catalog: {PROFILE_FILE}")
    data = json.loads(PROFILE_FILE.read_text())
    if not isinstance(data, dict):
        raise RuntimeError(f"Invalid mean-reversion profile catalog: {PROFILE_FILE}")
    return data


def _optional_float(value, default: float | None = None) -> float | None:
    if value is None:
        return default
    return float(value)


def _optional_int(value, default: int | None = None) -> int | None:
    if value is None:
        return default
    return int(value)


def load_profile(name: str) -> MeanReversionProfile:
    catalog = load_profile_catalog()
    if name not in catalog:
        raise KeyError(f"Unknown mean-reversion profile: {name}")
    row = catalog[name]
    return MeanReversionProfile(
        name=name,
        signal_source=row.get("signal_source", "wallets"),
        trade_mode=row.get("trade_mode", "fade"),
        price_signal_mode=row.get("price_signal_mode", "pop"),
        wallet_set=row.get("wallet_set"),
        explicit_wallets=tuple(row.get("explicit_wallets", ())),
        lookback_sec=int(row.get("lookback_sec", 10)),
        min_signal_strength=int(row.get("min_signal_strength", 0)),
        signal_dominance=float(row.get("signal_dominance", 1.0)),
        pop_threshold=float(row.get("pop_threshold", 0.0)),
        hold_sec=int(row.get("hold_sec", 30)),
        latency_sec=int(row.get("latency_sec", 0)),
        entry_price_floor=float(row.get("entry_price_floor", 0.0)),
        entry_price_cap=_optional_float(row.get("entry_price_cap")),
        position_size_usd=float(row.get("position_size_usd", 60.0)),
        fee_pct=float(row.get("fee_pct", 0.02)),
        min_elapsed_sec=int(row.get("min_elapsed_sec", 0)),
        max_elapsed_sec=_optional_int(row.get("max_elapsed_sec")),
        min_crowd_price=float(row.get("min_crowd_price", 0.0)),
        max_crowd_price=float(row.get("max_crowd_price", 1.0)),
        min_seconds_remaining=int(row.get("min_seconds_remaining", 0)),
        max_burst_age_sec=float(row.get("max_burst_age_sec", 99999.0)),
        max_spread=float(row.get("max_spread", 1.0)),
        min_entry_ask_depth_usd=float(row.get("min_entry_ask_depth_usd", 0.0)),
        min_exit_bid_depth_usd=float(row.get("min_exit_bid_depth_usd", 0.0)),
        depth_window=float(row.get("depth_window", 0.05)),
        target_price_delta=float(row.get("target_price_delta", 999.0)),
        target_price_abs=_optional_float(row.get("target_price_abs")),
        stop_price_delta=float(row.get("stop_price_delta", 999.0)),
        double_touch_crowd_price=_optional_float(row.get("double_touch_crowd_price")),
        double_touch_deadline_sec=_optional_int(row.get("double_touch_deadline_sec")),
        double_touch_max_extension=float(row.get("double_touch_max_extension", 0.0)),
    )
