"""Slippage estimation model — optimized for $1-5k capital ($20-100 per trade)."""

from __future__ import annotations


def estimate_slippage_bps(
    trade_value_usd: float,
    market_liquidity: float | None = None,
    base_bps: int = 30,
) -> float:
    """Estimate slippage in basis points based on trade size and liquidity.

    Optimized for small capital ($1-5k) where individual trades are $20-$200.

    Returns slippage as a fraction (e.g., 0.003 for 30 bps).
    """
    # Size-based slippage tiers
    if trade_value_usd < 50:
        bps = 15  # 10-20 bps range, use midpoint
    elif trade_value_usd < 200:
        bps = 30  # Typical range for our capital
    elif trade_value_usd < 500:
        bps = 55  # 40-75 bps range
    else:
        bps = 100  # 75-150 bps range

    # Illiquidity multiplier
    if market_liquidity is not None:
        if market_liquidity < 10_000:
            bps = int(bps * 3.0)  # Very illiquid — 3x
        elif market_liquidity < 50_000:
            bps = int(bps * 1.5)  # Low liquidity — 1.5x
        # > $50k: no multiplier

    # Use configured base_bps as a floor
    bps = max(bps, base_bps)

    return bps / 10_000


def apply_slippage(
    price: float,
    side: str,
    slippage: float,
) -> float:
    """Apply slippage to a price. BUY = price goes up, SELL = price goes down."""
    if side == "BUY":
        return price * (1 + slippage)
    else:
        return price * (1 - slippage)
