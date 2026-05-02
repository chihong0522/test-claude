"""Historical BTC 5m passive tail-ladder backtesting helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from statistics import median
from typing import Literal


ExitMode = Literal["target_abs", "target_delta", "resolve"]


@dataclass(frozen=True)
class TailLadderConfig:
    name: str = ""
    entry_levels: tuple[float, ...] = (0.05,)
    target_price_abs: float | None = 0.15
    target_price_delta: float | None = None
    timeout_sec: int = 40
    min_elapsed_sec: int = 0
    max_elapsed_sec: int = 120
    stake_per_level_usd: float = 20.0
    fee_pct: float = 0.02
    exit_mode: ExitMode = "target_abs"


@dataclass(frozen=True)
class TailLadderTrade:
    condition_id: str
    title: str
    position_side: str
    entry_level: float
    entry_price: float
    entry_offset_sec: int
    exit_price: float
    exit_after_sec: int
    exit_reason: str
    pnl: float
    market_winning_index: int
    correct_at_resolution: bool

    @property
    def is_win(self) -> bool:
        return self.pnl > 0


@dataclass
class TailLadderSummary:
    config: TailLadderConfig
    markets_evaluated: int = 0
    markets_with_fills: int = 0
    markets_with_both_sides_filled: int = 0
    trades_taken: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    avg_pnl: float = 0.0
    avg_entry_price: float | None = None
    median_entry_price: float | None = None
    avg_entry_offset_sec: float | None = None
    median_entry_offset_sec: float | None = None
    best_trade_pnl: float = 0.0
    worst_trade_pnl: float = 0.0
    trade_log: list[TailLadderTrade] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        if not self.trades_taken:
            return 0.0
        return round(self.wins / self.trades_taken * 100, 2)


@dataclass(frozen=True)
class _FilledLevel:
    side: str
    level: float
    entry_offset_sec: int
    trade_index: int


def _parse_end_ts(market: dict) -> int:
    return int(datetime.fromisoformat(str(market["end_date"]).replace("Z", "+00:00")).timestamp())


def _start_ts(market: dict) -> int:
    return _parse_end_ts(market) - 300


def _up_price_of_trade(trade: dict) -> float:
    price = float(trade.get("price") or 0.5)
    return price if int(trade.get("outcomeIndex") or 0) == 0 else 1.0 - price


def _side_price(side: str, up_price: float) -> float:
    return up_price if side == "YES" else 1.0 - up_price


def _resolution_exit_price(side: str, market_winning_index: int) -> float:
    position_outcome_index = 0 if side == "YES" else 1
    return 1.0 if position_outcome_index == market_winning_index else 0.0


def _simulate_exit(
    market: dict,
    trades: list[dict],
    fill: _FilledLevel,
    config: TailLadderConfig,
) -> tuple[float, str, int]:
    if config.exit_mode == "resolve":
        market_winning_index = int(market.get("winning_index") or 0)
        exit_after_sec = 300 - fill.entry_offset_sec
        return _resolution_exit_price(fill.side, market_winning_index), "resolve", max(exit_after_sec, 0)

    t0 = int(trades[fill.trade_index]["timestamp"])
    last_seen_price = fill.level
    last_seen_dt = 0
    target: float | None = None
    target_reason = "target_abs"
    if config.exit_mode == "target_delta":
        if config.target_price_delta is None:
            raise ValueError("target_price_delta must be set when exit_mode='target_delta'")
        target = fill.level + config.target_price_delta
        target_reason = "target_delta"
    else:
        target = config.target_price_abs

    for trade in trades[fill.trade_index + 1 :]:
        dt = int(trade.get("timestamp") or 0) - t0
        side_price = _side_price(fill.side, _up_price_of_trade(trade))
        if dt <= config.timeout_sec:
            last_seen_price = side_price
            last_seen_dt = dt
            if target is not None and side_price >= target:
                return target, target_reason, dt
            continue
        break

    return last_seen_price, "timeout", last_seen_dt


def simulate_tail_ladder_market(market: dict, trades: list[dict], config: TailLadderConfig) -> list[TailLadderTrade]:
    if not market.get("resolved") or market.get("winning_index") is None:
        return []
    if not config.entry_levels:
        return []
    if config.timeout_sec <= 0:
        raise ValueError("timeout_sec must be positive")
    if config.max_elapsed_sec < config.min_elapsed_sec:
        raise ValueError("max_elapsed_sec must be >= min_elapsed_sec")

    start_ts = _start_ts(market)
    sorted_trades = sorted(trades, key=lambda row: int(row.get("timestamp") or 0))
    filled: set[tuple[str, float]] = set()
    fills: list[_FilledLevel] = []

    for idx, trade in enumerate(sorted_trades):
        offset = int(trade.get("timestamp") or 0) - start_ts
        if offset < config.min_elapsed_sec or offset > config.max_elapsed_sec:
            continue
        up_now = _up_price_of_trade(trade)
        for side in ("YES", "NO"):
            side_now = _side_price(side, up_now)
            for level in config.entry_levels:
                key = (side, level)
                if key in filled:
                    continue
                if side_now <= level:
                    filled.add(key)
                    fills.append(
                        _FilledLevel(
                            side=side,
                            level=level,
                            entry_offset_sec=offset,
                            trade_index=idx,
                        )
                    )

    trades_out: list[TailLadderTrade] = []
    for fill in fills:
        exit_price, exit_reason, exit_after_sec = _simulate_exit(market, sorted_trades, fill, config)
        stake = config.stake_per_level_usd
        size = stake / fill.level
        proceeds = size * exit_price * (1.0 - config.fee_pct)
        cost_basis = stake * (1.0 + config.fee_pct)
        pnl = round(proceeds - cost_basis, 2)
        market_winning_index = int(market.get("winning_index") or 0)
        trades_out.append(
            TailLadderTrade(
                condition_id=str(market.get("condition_id") or ""),
                title=str(market.get("title") or ""),
                position_side=fill.side,
                entry_level=fill.level,
                entry_price=fill.level,
                entry_offset_sec=fill.entry_offset_sec,
                exit_price=round(exit_price, 4),
                exit_after_sec=exit_after_sec,
                exit_reason=exit_reason,
                pnl=pnl,
                market_winning_index=market_winning_index,
                correct_at_resolution=(0 if fill.side == "YES" else 1) == market_winning_index,
            )
        )

    return trades_out


def backtest_tail_ladder(
    markets: list[dict],
    trades_by_market: dict[str, list[dict]],
    config: TailLadderConfig,
) -> TailLadderSummary:
    summary = TailLadderSummary(config=config, markets_evaluated=len(markets))

    for market in markets:
        condition_id = str(market.get("condition_id") or "")
        market_trades = simulate_tail_ladder_market(
            market,
            trades_by_market.get(condition_id, []),
            config,
        )
        if not market_trades:
            continue
        summary.markets_with_fills += 1
        filled_sides = {trade.position_side for trade in market_trades}
        if len(filled_sides) > 1:
            summary.markets_with_both_sides_filled += 1
        summary.trade_log.extend(market_trades)

    summary.trades_taken = len(summary.trade_log)
    summary.wins = sum(1 for trade in summary.trade_log if trade.pnl > 0)
    summary.losses = sum(1 for trade in summary.trade_log if trade.pnl < 0)
    summary.total_pnl = round(sum(trade.pnl for trade in summary.trade_log), 2)

    if summary.trade_log:
        entries = [trade.entry_price for trade in summary.trade_log]
        offsets = [trade.entry_offset_sec for trade in summary.trade_log]
        summary.avg_pnl = round(summary.total_pnl / summary.trades_taken, 2)
        summary.avg_entry_price = round(sum(entries) / len(entries), 4)
        summary.median_entry_price = round(median(entries), 4)
        summary.avg_entry_offset_sec = round(sum(offsets) / len(offsets), 1)
        summary.median_entry_offset_sec = round(median(offsets), 1)
        summary.best_trade_pnl = round(max(trade.pnl for trade in summary.trade_log), 2)
        summary.worst_trade_pnl = round(min(trade.pnl for trade in summary.trade_log), 2)

    return summary
