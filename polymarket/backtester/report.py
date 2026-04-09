"""Format backtest results for API responses and display."""

from __future__ import annotations

from polymarket.backtester.simulator import BacktestResult


def format_backtest_summary(result: BacktestResult) -> dict:
    """Format a BacktestResult into a JSON-friendly summary."""
    return {
        "initial_capital": result.initial_capital,
        "final_capital": result.final_capital,
        "total_return_pct": result.total_return,
        "max_drawdown_pct": result.max_drawdown,
        "sharpe_ratio": result.sharpe_ratio,
        "total_trades_copied": result.total_trades_copied,
        "profitable_trades": result.profitable_trades,
        "losing_trades": result.losing_trades,
        "skipped_trades": result.skipped_trades,
        "win_rate_pct": result.win_rate,
        "avg_trade_pnl": result.avg_trade_pnl,
        "best_trade_pnl": result.best_trade_pnl,
        "worst_trade_pnl": result.worst_trade_pnl,
        "config": {
            "position_pct": result.config.position_pct,
            "slippage_bps": result.config.slippage_bps,
            "delay_seconds": result.config.delay_seconds,
            "fee_rate": result.config.fee_rate,
        },
    }


def format_equity_curve(result: BacktestResult) -> list[dict]:
    """Extract equity curve for charting."""
    return result.equity_curve
