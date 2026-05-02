"""Helpers for split-capital direct wallet-copy backtests."""

from __future__ import annotations

from typing import Iterable


def derive_wallet_tier_weight(wallet_row: dict) -> float:
    """Mirror the live bot's 2.0 / 1.0 / 0.5 tier logic."""
    oos_n = wallet_row.get("oos_participations", 0)
    oos_acc = wallet_row.get("oos_accuracy", 0.0)
    train_acc = wallet_row.get("signal_time_accuracy", 0.0)
    acc = oos_acc if oos_n >= 5 else train_acc
    if acc >= 0.80:
        return 2.0
    if acc < 0.60:
        return 0.5
    return 1.0


def select_wallet_rows(pool_data: dict, wallet_set: str = "elite", top_n: int = 10) -> list[dict]:
    """Select wallet rows from a refreshed smart-wallet pool.

    wallet_set:
      - elite: only wallets in the derived 2.0x tier
      - top: top-N wallets by the file's existing rank order
    """
    rows: list[dict] = []
    for rank, wallet_row in enumerate(pool_data.get("wallets", []), start=1):
        annotated = dict(wallet_row)
        annotated["rank"] = rank
        annotated["derived_weight"] = derive_wallet_tier_weight(wallet_row)
        rows.append(annotated)

    if wallet_set == "elite":
        selected = [row for row in rows if row["derived_weight"] == 2.0]
    elif wallet_set == "top":
        selected = rows
    else:
        raise ValueError(f"Unsupported wallet_set: {wallet_set}")

    if top_n > 0:
        selected = selected[:top_n]
    if not selected:
        raise ValueError("No wallets matched the requested selection")
    return selected


def allocate_capital(wallet_rows: Iterable[dict], total_capital: float, weighting: str = "equal") -> dict[str, float]:
    """Allocate total capital across selected wallets.

    Returns a wallet -> sleeve capital mapping rounded to cents and summing to
    the input total capital.
    """
    rows = list(wallet_rows)
    if not rows:
        raise ValueError("wallet_rows must not be empty")
    if total_capital <= 0:
        raise ValueError("total_capital must be positive")

    if weighting == "equal":
        raw_weights = [1.0] * len(rows)
    elif weighting == "tiered":
        raw_weights = [float(row.get("derived_weight", derive_wallet_tier_weight(row))) for row in rows]
    else:
        raise ValueError(f"Unsupported weighting: {weighting}")

    weight_sum = sum(raw_weights)
    if weight_sum <= 0:
        raise ValueError("Total allocation weight must be positive")

    allocations: dict[str, float] = {}
    remaining = round(total_capital, 2)
    for row, weight in zip(rows[:-1], raw_weights[:-1]):
        sleeve = round(total_capital * weight / weight_sum, 2)
        allocations[row["wallet"]] = sleeve
        remaining = round(remaining - sleeve, 2)

    allocations[rows[-1]["wallet"]] = remaining
    return allocations


def filter_trades_to_market_window(
    trades: Iterable[dict],
    condition_ids: set[str],
    start_ts: int,
    end_ts: int,
) -> list[dict]:
    """Keep only trades that belong to the selected market/time window."""
    filtered: list[dict] = []
    for trade in trades:
        condition_id = trade.get("conditionId") or trade.get("condition_id") or ""
        timestamp = int(trade.get("timestamp") or 0)
        if condition_id in condition_ids and start_ts <= timestamp <= end_ts:
            filtered.append(trade)
    return filtered


def summarize_portfolio_results(wallet_results: list[dict], total_capital: float) -> dict:
    """Aggregate wallet sleeve results into a portfolio summary."""
    final_capital = round(sum(float(r.get("final_capital", 0.0)) for r in wallet_results), 2)
    return {
        "wallet_count": len(wallet_results),
        "wallets_with_trades": sum(1 for r in wallet_results if r.get("filtered_trades", 0) > 0),
        "total_filtered_trades": sum(int(r.get("filtered_trades", 0)) for r in wallet_results),
        "total_copied_events": sum(int(r.get("copied_events", 0)) for r in wallet_results),
        "portfolio_final_capital": final_capital,
        "portfolio_pnl": round(final_capital - total_capital, 2),
        "portfolio_return_pct": round((final_capital - total_capital) / total_capital * 100, 2),
    }
