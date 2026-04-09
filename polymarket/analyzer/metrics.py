"""Individual metric calculators — pure functions operating on trade/position data."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime

import numpy as np


# ── Volume / Activity ────────────────────────────────────────────────────────

def trade_count(trades: list[dict]) -> int:
    return len(trades)


def time_span_days(trades: list[dict]) -> int:
    if not trades:
        return 0
    timestamps = [int(t.get("timestamp", 0)) for t in trades if t.get("timestamp")]
    if not timestamps:
        return 0
    return max(1, (max(timestamps) - min(timestamps)) // 86400)


def active_days(trades: list[dict]) -> int:
    if not trades:
        return 0
    days = set()
    for t in trades:
        ts = t.get("timestamp")
        if ts:
            days.add(int(ts) // 86400)
    return len(days)


def days_since_last_trade(trades: list[dict]) -> int:
    """Days since the most recent trade. 0 = traded today."""
    if not trades:
        return 9999
    timestamps = [int(t.get("timestamp", 0)) for t in trades if t.get("timestamp")]
    if not timestamps:
        return 9999
    now = int(datetime.utcnow().timestamp())
    return max(0, (now - max(timestamps)) // 86400)


def unique_markets(trades: list[dict]) -> int:
    slugs = set()
    for t in trades:
        slug = t.get("event_slug") or t.get("eventSlug") or t.get("condition_id") or t.get("conditionId")
        if slug:
            slugs.add(slug)
    return len(slugs)


def total_volume(trades: list[dict]) -> float:
    total = 0.0
    for t in trades:
        size = float(t.get("size", 0))
        price = float(t.get("price", 0))
        total += size * price
    return total


# ── ROI ──────────────────────────────────────────────────────────────────────

def compute_roi(
    closed_positions: list[dict],
    open_positions: list[dict] | None = None,
) -> float:
    """ROI = (realized + unrealized P&L) / capital deployed."""
    realized = sum(float(p.get("realizedPnl") or p.get("realized_pnl") or 0) for p in closed_positions)
    unrealized = 0.0
    if open_positions:
        unrealized = sum(float(p.get("cashPnl") or p.get("cash_pnl") or 0) for p in open_positions)

    capital = sum(
        float(p.get("totalBought") or p.get("total_bought") or 0) * float(p.get("avgPrice") or p.get("avg_price") or 0)
        for p in closed_positions
    )
    if open_positions:
        capital += sum(
            float(p.get("initialValue") or p.get("initial_value") or 0)
            for p in open_positions
        )

    if capital <= 0:
        return 0.0
    return (realized + unrealized) / capital


# ── Win Rate ─────────────────────────────────────────────────────────────────

def compute_win_rate(closed_positions: list[dict]) -> float:
    """Win rate = winning positions / total positions with nonzero P&L."""
    if not closed_positions:
        return 0.0
    wins = 0
    losses = 0
    for p in closed_positions:
        pnl = float(p.get("realizedPnl") or p.get("realized_pnl") or 0)
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1
    total = wins + losses
    if total == 0:
        return 0.0
    return wins / total


# ── Profit Factor ────────────────────────────────────────────────────────────

def compute_profit_factor(closed_positions: list[dict]) -> float:
    """Gross profit / gross loss. Returns 99.0 if no losses."""
    gross_profit = 0.0
    gross_loss = 0.0
    for p in closed_positions:
        pnl = float(p.get("realizedPnl") or p.get("realized_pnl") or 0)
        if pnl > 0:
            gross_profit += pnl
        elif pnl < 0:
            gross_loss += abs(pnl)
    if gross_loss <= 0:
        return 99.0 if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


# ── Sharpe Ratio ─────────────────────────────────────────────────────────────

def compute_sharpe_ratio(trades: list[dict]) -> float:
    """Annualized Sharpe ratio from daily P&L. Uses 365 days (crypto = 24/7)."""
    daily_pnl = _daily_pnl_series(trades)
    if len(daily_pnl) < 2:
        return 0.0
    arr = np.array(daily_pnl)
    mean = float(np.mean(arr))
    std = float(np.std(arr, ddof=1))
    if std == 0:
        return 0.0
    return mean / std * math.sqrt(365)


def _daily_pnl_series(trades: list[dict]) -> list[float]:
    """Aggregate P&L by calendar day from trades."""
    by_day: dict[int, float] = defaultdict(float)
    for t in trades:
        ts = int(t.get("timestamp", 0))
        if ts == 0:
            continue
        day = ts // 86400
        size = float(t.get("size", 0))
        price = float(t.get("price", 0))
        side = t.get("side", "BUY")
        # BUY = spend, SELL = receive
        pnl = size * price if side == "SELL" else -(size * price)
        by_day[day] += pnl

    if not by_day:
        return []
    return [by_day[d] for d in sorted(by_day)]


# ── Max Drawdown ─────────────────────────────────────────────────────────────

def compute_max_drawdown(trades: list[dict], initial_equity: float = 0.0) -> float:
    """Maximum peak-to-trough decline as a fraction (0.0 to 1.0)."""
    equity_curve = _build_equity_curve(trades, initial_equity)
    if len(equity_curve) < 2:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (peak - eq) / peak
            max_dd = max(max_dd, dd)
    return max_dd


def _build_equity_curve(trades: list[dict], initial: float = 0.0) -> list[float]:
    """Build equity curve from chronological trades."""
    sorted_trades = sorted(trades, key=lambda t: int(t.get("timestamp", 0)))
    equity = initial
    curve = [equity]
    for t in sorted_trades:
        size = float(t.get("size", 0))
        price = float(t.get("price", 0))
        side = t.get("side", "BUY")
        if side == "BUY":
            equity -= size * price
        else:
            equity += size * price
        curve.append(equity)
    return curve


# ── Recovery Factor ──────────────────────────────────────────────────────────

def compute_recovery_factor(net_profit: float, max_drawdown: float) -> float:
    if max_drawdown <= 0:
        return 99.0 if net_profit > 0 else 0.0
    return abs(net_profit) / abs(max_drawdown)


# ── Calmar Ratio ─────────────────────────────────────────────────────────────

def compute_calmar_ratio(
    net_profit: float,
    capital_deployed: float,
    time_span: int,
    max_drawdown: float,
) -> float:
    if max_drawdown <= 0 or capital_deployed <= 0 or time_span <= 0:
        return 0.0
    annual_return = (net_profit / capital_deployed) / (time_span / 365)
    return annual_return / abs(max_drawdown)


# ── Consistency Score ────────────────────────────────────────────────────────

def compute_consistency(trades: list[dict]) -> float:
    """1 / (1 + CV of monthly P&L). Higher = more consistent. Range [0, 1]."""
    monthly = _monthly_pnl(trades)
    if len(monthly) < 3:
        return 0.0
    arr = np.array(monthly)
    mean = float(np.mean(arr))
    std = float(np.std(arr, ddof=1))
    if mean == 0:
        return 0.0
    cv = abs(std / mean)
    return 1.0 / (1.0 + cv)


def _monthly_pnl(trades: list[dict]) -> list[float]:
    """Aggregate P&L by year-month."""
    by_month: dict[str, float] = defaultdict(float)
    for t in trades:
        ts = int(t.get("timestamp", 0))
        if ts == 0:
            continue
        dt = datetime.utcfromtimestamp(ts)
        key = f"{dt.year}-{dt.month:02d}"
        size = float(t.get("size", 0))
        price = float(t.get("price", 0))
        side = t.get("side", "BUY")
        pnl = size * price if side == "SELL" else -(size * price)
        by_month[key] += pnl
    return list(by_month.values()) if by_month else []


# ── Market Diversity ─────────────────────────────────────────────────────────

def compute_market_diversity(trades: list[dict]) -> float:
    """log(unique_markets) / log(50), capped at 1.0."""
    n = unique_markets(trades)
    if n <= 1:
        return 0.0
    return min(1.0, math.log(n) / math.log(50))


# ── Position Sizing Discipline ───────────────────────────────────────────────

def compute_position_sizing_score(trades: list[dict]) -> float:
    """Fraction of trades where position size is 0.5-5% of estimated portfolio."""
    if not trades:
        return 0.0

    # Estimate portfolio value as running total
    sorted_trades = sorted(trades, key=lambda t: int(t.get("timestamp", 0)))
    equity = 0.0
    good = 0
    total = 0

    for t in sorted_trades:
        size = float(t.get("size", 0))
        price = float(t.get("price", 0))
        trade_value = size * price
        side = t.get("side", "BUY")

        if side == "BUY" and equity > 0 and trade_value > 0:
            pct = trade_value / equity
            if 0.005 <= pct <= 0.05:
                good += 1
            total += 1

        if side == "BUY":
            equity -= trade_value
        else:
            equity += trade_value
        equity = max(equity, trade_value)  # Floor to avoid negative

    return good / total if total > 0 else 0.0


# ── Liquidity Score ──────────────────────────────────────────────────────────

def compute_liquidity_score(
    trades: list[dict],
    market_liquidity: dict[str, float],
    threshold: float = 50_000.0,
) -> float:
    """Fraction of trades in markets with liquidity > threshold."""
    if not trades:
        return 0.0
    liquid_count = 0
    total = 0
    for t in trades:
        cid = t.get("condition_id") or t.get("conditionId")
        if not cid:
            continue
        total += 1
        liq = market_liquidity.get(cid, 0)
        if liq >= threshold:
            liquid_count += 1
    return liquid_count / total if total > 0 else 0.0


# ── Net Profit ───────────────────────────────────────────────────────────────

def compute_net_profit(closed_positions: list[dict]) -> float:
    return sum(float(p.get("realizedPnl") or p.get("realized_pnl") or 0) for p in closed_positions)


def compute_capital_deployed(
    closed_positions: list[dict],
    open_positions: list[dict] | None = None,
) -> float:
    capital = sum(
        float(p.get("totalBought") or p.get("total_bought") or 0) * float(p.get("avgPrice") or p.get("avg_price") or 0)
        for p in closed_positions
    )
    if open_positions:
        capital += sum(float(p.get("initialValue") or p.get("initial_value") or 0) for p in open_positions)
    return capital
