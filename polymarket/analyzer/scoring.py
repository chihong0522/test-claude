"""Composite scoring engine — combines metrics into a 0-100 score with tier assignment."""

from __future__ import annotations

import math

from polymarket.analyzer import metrics as m
from polymarket.analyzer.red_flags import RED_FLAG_PENALTIES, detect_red_flags


def score_trader(
    trades: list[dict],
    open_positions: list[dict],
    closed_positions: list[dict],
    market_liquidity: dict[str, float] | None = None,
) -> dict:
    """Compute all metrics and composite score for a trader.

    Returns a dict with all metric values, composite_score, tier, red_flags,
    and passes_checklist.
    """
    market_liq = market_liquidity or {}

    # ── Compute individual metrics ──────────────────────────────────────
    tc = m.trade_count(trades)
    ts_days = m.time_span_days(trades)
    ad = m.active_days(trades)
    days_inactive = m.days_since_last_trade(trades)
    um = m.unique_markets(trades)
    tv = m.total_volume(trades)
    net_profit = m.compute_net_profit(closed_positions)
    capital = m.compute_capital_deployed(closed_positions, open_positions)

    roi = m.compute_roi(closed_positions, open_positions)
    win_rate = m.compute_win_rate(closed_positions)
    profit_factor = m.compute_profit_factor(closed_positions)
    sharpe = m.compute_sharpe_ratio(trades)
    max_dd = m.compute_max_drawdown(trades, initial_equity=capital)
    recovery = m.compute_recovery_factor(net_profit, max_dd * capital if capital > 0 else 0)
    calmar = m.compute_calmar_ratio(net_profit, capital, ts_days, max_dd)
    consistency = m.compute_consistency(trades)
    diversity = m.compute_market_diversity(trades)
    pos_sizing = m.compute_position_sizing_score(trades)
    liq_score = m.compute_liquidity_score(trades, market_liq)

    raw_metrics = {
        "trade_count": tc,
        "active_days": ad,
        "time_span_days": ts_days,
        "days_since_last_trade": days_inactive,
        "total_volume": tv,
        "unique_markets": um,
        "net_profit": net_profit,
        "roi": roi,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "sharpe_ratio": sharpe,
        "max_drawdown": max_dd,
        "recovery_factor": recovery,
        "calmar_ratio": calmar,
        "consistency_score": consistency,
        "market_diversity": diversity,
        "position_sizing_score": pos_sizing,
        "liquidity_score": liq_score,
    }

    # ── Detect red flags ────────────────────────────────────────────────
    red_flags = detect_red_flags(trades, closed_positions, raw_metrics, market_liq)

    # ── Compute weighted composite score ────────────────────────────────
    composite = _compute_composite(raw_metrics, red_flags)

    # ── Check minimum checklist ─────────────────────────────────────────
    passes = _passes_checklist(raw_metrics, red_flags)

    # ── Assign tier ─────────────────────────────────────────────────────
    tier = _assign_tier(composite, red_flags, passes)

    return {
        **raw_metrics,
        "composite_score": composite,
        "tier": tier,
        "red_flags": red_flags,
        "passes_checklist": passes,
    }


def _compute_composite(metrics: dict, red_flags: list[str]) -> float:
    """Weighted composite score 0-100."""
    score = 0.0

    # ROI (20%) — sigmoid normalized around 10% ROI
    roi = metrics["roi"]
    score += 20 * _sigmoid(roi, center=0.10, steepness=10)

    # Win rate (15%) — linear in [0.45, 0.75], capped
    wr = metrics["win_rate"]
    score += 15 * _linear_scale(wr, low=0.45, high=0.75)

    # Profit factor (15%) — linear in [0.5, 3.0]
    pf = metrics["profit_factor"]
    score += 15 * _linear_scale(pf, low=0.5, high=3.0)

    # Sharpe ratio (15%) — linear in [0, 2.5], penalized above 3.0
    sharpe = metrics["sharpe_ratio"]
    if sharpe > 3.0:
        score += 15 * 0.5  # Penalized
    else:
        score += 15 * _linear_scale(sharpe, low=0.0, high=2.5)

    # Consistency (10%) — direct score
    score += 10 * min(1.0, max(0.0, metrics["consistency_score"]))

    # Market diversity (10%) — direct score
    score += 10 * min(1.0, max(0.0, metrics["market_diversity"]))

    # Recovery factor (5%) — linear in [0, 5.0]
    score += 5 * _linear_scale(metrics["recovery_factor"], low=0.0, high=5.0)

    # Position sizing (5%) — direct score
    score += 5 * min(1.0, max(0.0, metrics["position_sizing_score"]))

    # Liquidity score (5%) — direct score
    score += 5 * min(1.0, max(0.0, metrics["liquidity_score"]))

    # Trade count bonus (5%) — min(count/500, 1.0)
    score += 5 * min(1.0, metrics["trade_count"] / 500)

    # ── Apply red flag penalties ────────────────────────────────────────
    for flag in red_flags:
        penalty = RED_FLAG_PENALTIES.get(flag, 5)
        score -= penalty

    return max(0.0, min(100.0, score))


def _passes_checklist(metrics: dict, red_flags: list[str]) -> bool:
    """Check if trader passes all minimum thresholds."""
    if metrics["trade_count"] < 200:
        return False
    if metrics["time_span_days"] < 180:
        return False
    if metrics["roi"] <= 0:
        return False
    if metrics["profit_factor"] <= 1.0:
        return False
    if not (0.40 <= metrics["win_rate"] <= 0.90):
        return False
    if not (0.0 <= metrics["sharpe_ratio"] <= 3.0):
        return False
    if metrics["unique_markets"] < 5:
        return False
    if metrics["liquidity_score"] < 0.70:
        return False
    if metrics.get("days_since_last_trade", 9999) > 3:
        return False
    # Critical red flags fail the checklist
    critical = {"WIN_RATE_SUSPICIOUS", "SHARPE_OVERFITTING"}
    if critical & set(red_flags):
        return False
    return True


def _assign_tier(composite: float, red_flags: list[str], passes: bool) -> str:
    if composite >= 80 and not red_flags and passes:
        return "S"
    if composite >= 65 and passes:
        return "A"
    if composite >= 50:
        return "B"
    if composite >= 35:
        return "C"
    return "F"


# ── Normalization helpers ────────────────────────────────────────────────────

def _sigmoid(x: float, center: float = 0.0, steepness: float = 1.0) -> float:
    """Sigmoid function scaled to [0, 1]."""
    try:
        return 1.0 / (1.0 + math.exp(-steepness * (x - center)))
    except OverflowError:
        return 0.0 if x < center else 1.0


def _linear_scale(x: float, low: float, high: float) -> float:
    """Linear interpolation in [low, high] clamped to [0, 1]."""
    if high <= low:
        return 0.0
    return max(0.0, min(1.0, (x - low) / (high - low)))
