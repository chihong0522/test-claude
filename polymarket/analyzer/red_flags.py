"""Red flag detection — identifies suspicious or uncopyable trader patterns."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime


def detect_red_flags(
    trades: list[dict],
    closed_positions: list[dict],
    metrics: dict,
    market_liquidity: dict[str, float] | None = None,
) -> list[str]:
    """Return list of red flag strings for a trader. Each flag deducts from score."""
    flags: list[str] = []

    # 1. Win rate > 90% — suspicious
    if metrics.get("win_rate", 0) > 0.90:
        flags.append("WIN_RATE_SUSPICIOUS")

    # 2. Sharpe > 3.0 — overfitting
    if metrics.get("sharpe_ratio", 0) > 3.0:
        flags.append("SHARPE_OVERFITTING")

    # 3. < 100 trades — insufficient data
    if metrics.get("trade_count", 0) < 100:
        flags.append("LOW_TRADE_COUNT")

    # 4. < 90 days active — short history
    if metrics.get("time_span_days", 0) < 90:
        flags.append("SHORT_HISTORY")

    # 5. > 50% profit from single market — concentrated
    if _is_concentrated(closed_positions):
        flags.append("CONCENTRATED_PROFITS")

    # 6. Last 30 days = >80% of all-time profit — recent spike
    if _is_recent_spike(trades):
        flags.append("RECENT_SPIKE")

    # 7. > 30% trades in illiquid markets — uncopyable
    if market_liquidity and _is_illiquid_trader(trades, market_liquidity):
        flags.append("ILLIQUID_MARKETS")

    # 8. Avg position > 10% of portfolio — reckless sizing
    if metrics.get("position_sizing_score", 1.0) < 0.2:
        flags.append("LARGE_POSITION_SIZES")

    # 9. High spread markets
    if market_liquidity and _trades_in_wide_spread_markets(trades, market_liquidity):
        flags.append("HIGH_SPREAD_MARKETS")

    return flags


def _is_concentrated(closed_positions: list[dict]) -> bool:
    """More than 50% of total profit from a single market."""
    pnl_by_market: dict[str, float] = defaultdict(float)
    total_profit = 0.0

    for p in closed_positions:
        pnl = float(p.get("realizedPnl") or p.get("realized_pnl") or 0)
        slug = p.get("event_slug") or p.get("eventSlug") or p.get("slug") or p.get("condition_id") or p.get("conditionId")
        if slug and pnl > 0:
            pnl_by_market[slug] += pnl
            total_profit += pnl

    if total_profit <= 0 or not pnl_by_market:
        return False
    max_market_pnl = max(pnl_by_market.values())
    return max_market_pnl / total_profit > 0.50


def _is_recent_spike(trades: list[dict]) -> bool:
    """Last 30 days account for >80% of all-time P&L."""
    if not trades:
        return False
    now = int(datetime.utcnow().timestamp())
    cutoff = now - 30 * 86400

    total_pnl = 0.0
    recent_pnl = 0.0
    for t in trades:
        ts = int(t.get("timestamp", 0))
        size = float(t.get("size", 0))
        price = float(t.get("price", 0))
        side = t.get("side", "BUY")
        pnl = size * price if side == "SELL" else -(size * price)
        total_pnl += pnl
        if ts >= cutoff:
            recent_pnl += pnl

    if total_pnl <= 0:
        return False
    return recent_pnl / total_pnl > 0.80


def _is_illiquid_trader(
    trades: list[dict],
    market_liquidity: dict[str, float],
    threshold: float = 50_000,
) -> bool:
    """More than 30% of trades in markets with liquidity < threshold."""
    if not trades:
        return False
    illiquid = 0
    total = 0
    for t in trades:
        cid = t.get("condition_id") or t.get("conditionId")
        if not cid:
            continue
        total += 1
        liq = market_liquidity.get(cid, 0)
        if liq < threshold:
            illiquid += 1
    if total == 0:
        return False
    return illiquid / total > 0.30


def _trades_in_wide_spread_markets(
    trades: list[dict],
    market_liquidity: dict[str, float],
) -> bool:
    """Check if avg trade is in markets with spread > 5%. Uses liquidity as proxy."""
    # This is a simplified check — wide spread correlates with low liquidity
    if not trades:
        return False
    low_liq_count = 0
    total = 0
    for t in trades:
        cid = t.get("condition_id") or t.get("conditionId")
        if not cid:
            continue
        total += 1
        liq = market_liquidity.get(cid, 0)
        if liq < 10_000:  # Very low liquidity = likely wide spread
            low_liq_count += 1
    if total == 0:
        return False
    return low_liq_count / total > 0.20


# Red flag penalty points
RED_FLAG_PENALTIES: dict[str, int] = {
    "WIN_RATE_SUSPICIOUS": 15,
    "SHARPE_OVERFITTING": 10,
    "LOW_TRADE_COUNT": 10,
    "SHORT_HISTORY": 10,
    "CONCENTRATED_PROFITS": 10,
    "ILLIQUID_MARKETS": 10,
    "RECENT_SPIKE": 5,
    "LARGE_POSITION_SIZES": 5,
    "HIGH_SPREAD_MARKETS": 5,
}
