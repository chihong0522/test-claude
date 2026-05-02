"""Historical 5-minute BTC mean-reversion backtesting helpers."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime


@dataclass(frozen=True)
class MeanReversionConfig:
    name: str = ""
    bucket_sec: int = 10
    lookback_sec: int = 10
    min_signal_strength: int = 4
    signal_dominance: float = 3.0
    pop_threshold: float = 0.04
    hold_sec: int = 30
    latency_sec: int = 0
    entry_price_cap: float | None = None
    entry_price_floor: float | None = None
    position_size_usd: float = 60.0
    fee_pct: float = 0.02


@dataclass(frozen=True)
class MeanReversionTrade:
    condition_id: str
    title: str
    signal_side: str
    position_side: str
    entry_price: float
    exit_price: float
    pop_amount: float
    entry_bucket: int
    exit_bucket: int
    pnl: float
    market_winning_index: int
    correct_at_resolution: bool

    @property
    def is_win(self) -> bool:
        return self.pnl > 0


@dataclass
class MeanReversionSummary:
    config: MeanReversionConfig
    wallet_set_name: str = ""
    markets_evaluated: int = 0
    trades_taken: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    avg_pnl: float = 0.0
    best_trade_pnl: float = 0.0
    worst_trade_pnl: float = 0.0
    trade_log: list[MeanReversionTrade] = field(default_factory=list)


def _parse_end_ts(market: dict) -> float:
    end = market.get("end_date")
    if not end:
        return 0.0
    try:
        return datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, AttributeError):
        return 0.0


def _up_price_of_trade(trade: dict) -> float:
    price = float(trade.get("price") or 0.5)
    return price if int(trade.get("outcomeIndex") or 0) == 0 else 1.0 - price


def _bucketize_market_trades(market: dict, trades: list[dict], bucket_sec: int) -> tuple[dict[int, list[dict]], dict[int, float], int]:
    end_ts = _parse_end_ts(market)
    start_ts = int(end_ts - 300)
    buckets: dict[int, list[dict]] = defaultdict(list)
    sorted_trades = sorted(trades, key=lambda t: int(t.get("timestamp") or 0))
    for trade in sorted_trades:
        offset = int(trade.get("timestamp") or 0) - start_ts
        if 0 <= offset <= 300:
            bucket_idx = offset // bucket_sec
            buckets[bucket_idx].append(trade)

    max_bucket = 300 // bucket_sec
    last_up_price: dict[int, float] = {}
    running_price = 0.5
    for bucket_idx in range(max_bucket + 1):
        for trade in buckets.get(bucket_idx, []):
            running_price = _up_price_of_trade(trade)
        last_up_price[bucket_idx] = running_price

    return buckets, last_up_price, max_bucket


def simulate_mean_reversion_market(
    market: dict,
    trades: list[dict],
    config: MeanReversionConfig,
    smart_wallets: set[str],
) -> MeanReversionTrade | None:
    if not market.get("resolved") or market.get("winning_index") is None:
        return None
    if config.bucket_sec <= 0:
        raise ValueError("bucket_sec must be positive")
    if config.lookback_sec <= 0:
        raise ValueError("lookback_sec must be positive")
    if config.hold_sec <= 0:
        raise ValueError("hold_sec must be positive")

    end_ts = _parse_end_ts(market)
    if end_ts == 0:
        return None

    buckets, last_up_price, max_bucket = _bucketize_market_trades(market, trades, config.bucket_sec)
    lookback_buckets = max(1, config.lookback_sec // config.bucket_sec)
    hold_buckets = max(1, config.hold_sec // config.bucket_sec)
    latency_buckets = max(0, config.latency_sec // config.bucket_sec)

    for bucket_idx in range(max_bucket + 1):
        bucket = buckets.get(bucket_idx, [])
        if not bucket:
            continue

        smart_buy_trades = [
            trade for trade in bucket
            if trade.get("proxyWallet") in smart_wallets
            and (trade.get("side") or "BUY").upper() == "BUY"
        ]
        if not smart_buy_trades:
            continue

        yes_wallets = {
            trade.get("proxyWallet")
            for trade in smart_buy_trades
            if int(trade.get("outcomeIndex") or 0) == 0 and trade.get("proxyWallet")
        }
        no_wallets = {
            trade.get("proxyWallet")
            for trade in smart_buy_trades
            if int(trade.get("outcomeIndex") or 0) == 1 and trade.get("proxyWallet")
        }
        yes_strength = len(yes_wallets)
        no_strength = len(no_wallets)

        signal_side: str | None = None
        if (
            yes_strength >= config.min_signal_strength
            and yes_strength >= config.signal_dominance * max(no_strength, 1)
        ):
            signal_side = "YES"
        elif (
            no_strength >= config.min_signal_strength
            and no_strength >= config.signal_dominance * max(yes_strength, 1)
        ):
            signal_side = "NO"

        if signal_side is None:
            continue

        exec_bucket = bucket_idx + latency_buckets
        if exec_bucket > max_bucket:
            continue

        baseline_bucket = max(0, bucket_idx - lookback_buckets)
        baseline_up_price = last_up_price.get(baseline_bucket, 0.5)
        current_up_price = last_up_price.get(exec_bucket, last_up_price.get(bucket_idx, 0.5))
        pop_amount = (
            current_up_price - baseline_up_price
            if signal_side == "YES"
            else baseline_up_price - current_up_price
        )
        if pop_amount < config.pop_threshold:
            continue

        position_side = "NO" if signal_side == "YES" else "YES"
        entry_price = current_up_price if position_side == "YES" else 1.0 - current_up_price
        if config.entry_price_cap is not None and entry_price > config.entry_price_cap:
            continue
        if config.entry_price_floor is not None and entry_price < config.entry_price_floor:
            continue
        if entry_price <= 0:
            continue

        exit_bucket = min(max_bucket, exec_bucket + hold_buckets)
        exit_up_price = last_up_price.get(exit_bucket, current_up_price)
        exit_price = exit_up_price if position_side == "YES" else 1.0 - exit_up_price

        size = config.position_size_usd / entry_price
        cost_basis = config.position_size_usd * (1.0 + config.fee_pct)
        proceeds = size * exit_price * (1.0 - config.fee_pct)
        pnl = proceeds - cost_basis
        position_outcome_index = 0 if position_side == "YES" else 1

        return MeanReversionTrade(
            condition_id=str(market.get("condition_id") or ""),
            title=str(market.get("title") or ""),
            signal_side=signal_side,
            position_side=position_side,
            entry_price=entry_price,
            exit_price=exit_price,
            pop_amount=pop_amount,
            entry_bucket=exec_bucket,
            exit_bucket=exit_bucket,
            pnl=round(pnl, 2),
            market_winning_index=int(market.get("winning_index") or 0),
            correct_at_resolution=position_outcome_index == int(market.get("winning_index") or 0),
        )

    return None


def backtest_mean_reversion(
    markets: list[dict],
    trades_by_market: dict[str, list[dict]],
    config: MeanReversionConfig,
    smart_wallets: set[str],
    wallet_set_name: str = "",
) -> MeanReversionSummary:
    summary = MeanReversionSummary(
        config=config,
        wallet_set_name=wallet_set_name,
        markets_evaluated=len(markets),
    )

    for market in markets:
        condition_id = str(market.get("condition_id") or "")
        trade = simulate_mean_reversion_market(
            market,
            trades_by_market.get(condition_id, []),
            config,
            smart_wallets=smart_wallets,
        )
        if trade is None:
            continue
        summary.trade_log.append(trade)

    summary.trades_taken = len(summary.trade_log)
    summary.wins = sum(1 for trade in summary.trade_log if trade.is_win)
    summary.losses = summary.trades_taken - summary.wins
    summary.total_pnl = round(sum(trade.pnl for trade in summary.trade_log), 2)
    if summary.trades_taken:
        summary.win_rate = round(summary.wins / summary.trades_taken * 100, 2)
        summary.avg_pnl = round(summary.total_pnl / summary.trades_taken, 2)
        summary.best_trade_pnl = round(max(trade.pnl for trade in summary.trade_log), 2)
        summary.worst_trade_pnl = round(min(trade.pnl for trade in summary.trade_log), 2)

    return summary


def search_mean_reversion_configs(
    markets: list[dict],
    trades_by_market: dict[str, list[dict]],
    configs: list[MeanReversionConfig],
    wallet_sets: dict[str, set[str]],
) -> list[MeanReversionSummary]:
    results: list[MeanReversionSummary] = []
    for wallet_set_name, smart_wallets in wallet_sets.items():
        for config in configs:
            named_config = config if config.name else replace(config, name="mean-reversion")
            results.append(
                backtest_mean_reversion(
                    markets,
                    trades_by_market,
                    named_config,
                    smart_wallets=smart_wallets,
                    wallet_set_name=wallet_set_name,
                )
            )

    return sorted(
        results,
        key=lambda summary: (
            summary.win_rate,
            summary.total_pnl,
            summary.trades_taken,
            summary.wallet_set_name,
            summary.config.name,
        ),
        reverse=True,
    )
