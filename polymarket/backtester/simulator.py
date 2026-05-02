"""Copy-trade simulation engine — answers 'what if I copied this trader?'"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

from polymarket.backtester.slippage import apply_slippage, estimate_slippage_bps


@dataclass
class BacktestConfig:
    initial_capital: float = 3_000.0
    position_pct: float = 0.02     # 2% of capital per trade
    max_position_pct: float = 0.10  # Hard cap at 10%
    slippage_bps: int = 30
    delay_seconds: int = 30
    fee_rate: float = 0.002        # 0.2% taker fee
    start_date: int | None = None  # Unix timestamp
    end_date: int | None = None
    compound: bool = True


@dataclass
class TradeResult:
    timestamp: int
    side: str
    original_price: float
    copy_price: float
    size: float
    cost: float
    pnl: float
    equity_after: float
    condition_id: str
    title: str


@dataclass
class BacktestResult:
    config: BacktestConfig
    total_trades_copied: int = 0
    profitable_trades: int = 0
    losing_trades: int = 0
    skipped_trades: int = 0
    initial_capital: float = 0.0
    final_capital: float = 0.0
    total_return: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    win_rate: float = 0.0
    avg_trade_pnl: float = 0.0
    best_trade_pnl: float = 0.0
    worst_trade_pnl: float = 0.0
    equity_curve: list[dict] = field(default_factory=list)
    trade_log: list[TradeResult] = field(default_factory=list)


def run_backtest(
    trades: list[dict],
    config: BacktestConfig | None = None,
    market_liquidity: dict[str, float] | None = None,
    market_outcomes: dict[str, int] | None = None,
) -> BacktestResult:
    """Simulate copy-trading a wallet's trades.

    Args:
        trades: Chronological list of trades from the target wallet.
        config: Backtest configuration.
        market_liquidity: {condition_id: liquidity_usd} for slippage estimation.
        market_outcomes: Optional {condition_id: winning_outcome_index} map for
            settling any still-open resolved positions at the end of the run.
    """
    cfg = config or BacktestConfig()
    market_liq = market_liquidity or {}
    outcomes = market_outcomes or {}

    result = BacktestResult(
        config=cfg,
        initial_capital=cfg.initial_capital,
    )

    # Filter trades by date range
    sorted_trades = sorted(trades, key=lambda t: int(t.get("timestamp", 0)))
    if cfg.start_date:
        sorted_trades = [t for t in sorted_trades if int(t.get("timestamp", 0)) >= cfg.start_date]
    if cfg.end_date:
        sorted_trades = [t for t in sorted_trades if int(t.get("timestamp", 0)) <= cfg.end_date]

    if not sorted_trades:
        result.final_capital = cfg.initial_capital
        return result

    # State
    cash = cfg.initial_capital
    positions: dict[str, dict] = {}  # key: (condition_id, outcome_index) as str
    equity_curve: list[dict] = [{"timestamp": int(sorted_trades[0].get("timestamp", 0)), "equity": cash}]
    trade_pnls: list[float] = []

    for t in sorted_trades:
        ts = int(t.get("timestamp", 0))
        side = t.get("side", "BUY")
        price = float(t.get("price", 0))
        size = float(t.get("size", 0))
        condition_id = t.get("conditionId") or t.get("condition_id") or ""
        outcome_idx = t.get("outcomeIndex") or t.get("outcome_index") or 0
        title = t.get("title") or ""
        pos_key = f"{condition_id}:{outcome_idx}"

        if price <= 0 or size <= 0:
            continue

        # Estimate total equity for position sizing
        pos_value = sum(
            p["size"] * p.get("current_price", p["entry_price"])
            for p in positions.values()
        )
        total_equity = cash + pos_value

        # Calculate slippage
        trade_value = size * price
        liq = market_liq.get(condition_id)
        slippage = estimate_slippage_bps(trade_value, liq, cfg.slippage_bps)
        copy_price = apply_slippage(price, side, slippage)

        if side == "BUY":
            # Determine copy size based on position sizing rules
            max_spend = total_equity * min(cfg.position_pct, cfg.max_position_pct)
            copy_size = max_spend / (copy_price * (1 + cfg.fee_rate))
            cost = copy_size * copy_price * (1 + cfg.fee_rate)

            if cost > cash or cost <= 0:
                result.skipped_trades += 1
                continue

            cash -= cost

            # Update or create position
            if pos_key in positions:
                pos = positions[pos_key]
                total_size = pos["size"] + copy_size
                pos["entry_price"] = (
                    (pos["entry_price"] * pos["size"] + copy_price * copy_size) / total_size
                )
                pos["size"] = total_size
                pos["total_cost"] += cost
            else:
                positions[pos_key] = {
                    "size": copy_size,
                    "entry_price": copy_price,
                    "total_cost": cost,
                    "current_price": copy_price,
                    "condition_id": condition_id,
                    "title": title,
                }

            result.total_trades_copied += 1
            trade_log = TradeResult(
                timestamp=ts, side="BUY", original_price=price,
                copy_price=copy_price, size=copy_size, cost=cost,
                pnl=0.0, equity_after=cash + pos_value,
                condition_id=condition_id, title=title,
            )
            result.trade_log.append(trade_log)

        elif side == "SELL" and pos_key in positions:
            pos = positions[pos_key]
            # Sell proportional to what the original trader sold
            sell_ratio = min(1.0, size / max(size, pos["size"]))
            sell_size = pos["size"] * sell_ratio
            proceeds = sell_size * copy_price * (1 - cfg.fee_rate)
            entry_cost = sell_size * pos["entry_price"]
            trade_pnl = proceeds - entry_cost

            cash += proceeds
            trade_pnls.append(trade_pnl)

            pos["size"] -= sell_size
            if pos["size"] < 0.001:
                del positions[pos_key]

            if trade_pnl > 0:
                result.profitable_trades += 1
            else:
                result.losing_trades += 1
            result.total_trades_copied += 1

            pos_value_now = sum(p["size"] * p.get("current_price", p["entry_price"]) for p in positions.values())
            trade_log = TradeResult(
                timestamp=ts, side="SELL", original_price=price,
                copy_price=copy_price, size=sell_size, cost=0.0,
                pnl=trade_pnl, equity_after=cash + pos_value_now,
                condition_id=condition_id, title=title,
            )
            result.trade_log.append(trade_log)

        # Update equity curve
        pos_value_now = sum(
            p["size"] * p.get("current_price", p["entry_price"])
            for p in positions.values()
        )
        equity_curve.append({"timestamp": ts, "equity": round(cash + pos_value_now, 2)})

    # ── Handle unresolved positions at end ───────────────────────────────
    # If we know the market outcome, settle at binary resolution. Otherwise,
    # fall back to the last known price as a mark-to-market estimate.
    pos_value_final = 0.0
    for pos_key, pos in positions.items():
        condition_id = pos["condition_id"]
        if condition_id in outcomes:
            outcome_idx = int(pos_key.rsplit(":", 1)[1])
            pos_value_final += pos["size"] * (1.0 if outcome_idx == outcomes[condition_id] else 0.0)
        else:
            pos_value_final += pos["size"] * pos.get("current_price", pos["entry_price"])
    result.final_capital = round(cash + pos_value_final, 2)
    result.equity_curve = equity_curve

    # ── Compute summary stats ───────────────────────────────────────────
    if cfg.initial_capital > 0:
        result.total_return = round(
            (result.final_capital - cfg.initial_capital) / cfg.initial_capital * 100, 2
        )

    result.max_drawdown = _compute_max_drawdown(equity_curve)

    if trade_pnls:
        result.avg_trade_pnl = round(sum(trade_pnls) / len(trade_pnls), 2)
        result.best_trade_pnl = round(max(trade_pnls), 2)
        result.worst_trade_pnl = round(min(trade_pnls), 2)
        total_decided = result.profitable_trades + result.losing_trades
        result.win_rate = round(result.profitable_trades / total_decided * 100, 1) if total_decided > 0 else 0

    result.sharpe_ratio = _compute_equity_sharpe(equity_curve)

    return result


def _compute_max_drawdown(equity_curve: list[dict]) -> float:
    if len(equity_curve) < 2:
        return 0.0
    equities = [p["equity"] for p in equity_curve]
    peak = equities[0]
    max_dd = 0.0
    for eq in equities:
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (peak - eq) / peak
            max_dd = max(max_dd, dd)
    return round(max_dd * 100, 2)


def _compute_equity_sharpe(equity_curve: list[dict]) -> float:
    if len(equity_curve) < 3:
        return 0.0
    equities = [p["equity"] for p in equity_curve]
    returns = [(equities[i] - equities[i - 1]) / equities[i - 1]
               for i in range(1, len(equities)) if equities[i - 1] > 0]
    if len(returns) < 2:
        return 0.0
    arr = np.array(returns)
    mean_r = float(np.mean(arr))
    std_r = float(np.std(arr, ddof=1))
    if std_r == 0:
        return 0.0
    return round(mean_r / std_r * math.sqrt(365), 2)
