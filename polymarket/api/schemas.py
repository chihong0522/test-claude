"""Pydantic request/response schemas for the API."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field


# ── Trader schemas ───────────────────────────────────────────────────────────

class TraderSummary(BaseModel):
    rank: int | None = None
    trader_id: int
    proxy_wallet: str
    name: str | None = None
    composite_score: float
    tier: str
    roi: float
    win_rate: float
    profit_factor: float = 0.0
    sharpe_ratio: float = 0.0
    trade_count: int = 0
    liquidity_score: float = 0.0
    red_flags: list[str] = []


class TraderDetail(TraderSummary):
    pseudonym: str | None = None
    bio: str | None = None
    profile_image: str | None = None
    net_profit: float = 0.0
    max_drawdown: float = 0.0
    recovery_factor: float = 0.0
    consistency_score: float = 0.0
    market_diversity: float = 0.0
    position_sizing_score: float = 0.0
    unique_markets: int = 0
    active_days: int = 0
    time_span_days: int = 0
    total_volume: float = 0.0
    passes_checklist: bool = False
    last_updated_at: datetime | None = None


class TradeRecord(BaseModel):
    side: str
    size: float
    price: float
    timestamp: int
    title: str | None = None
    outcome: str | None = None
    condition_id: str | None = None
    transaction_hash: str | None = None


class PositionRecord(BaseModel):
    condition_id: str
    size: float | None = None
    avg_price: float | None = None
    current_value: float | None = None
    cash_pnl: float | None = None
    realized_pnl: float | None = None
    title: str | None = None
    outcome: str | None = None
    is_closed: bool = False


# ── Backtest schemas ─────────────────────────────────────────────────────────

class BacktestRequest(BaseModel):
    wallet: str
    initial_capital: float = Field(default=3000.0, ge=100, le=1_000_000)
    position_pct: float = Field(default=0.02, ge=0.001, le=0.5)
    slippage_bps: int = Field(default=30, ge=0, le=500)
    delay_seconds: int = Field(default=30, ge=0, le=600)
    start_date: int | None = None
    end_date: int | None = None


class BacktestSummary(BaseModel):
    id: int
    trader_id: int
    created_at: datetime
    initial_capital: float
    final_capital: float
    total_return: float
    max_drawdown: float
    sharpe_ratio: float
    win_rate: float
    total_trades_copied: int
    profitable_trades: int
    losing_trades: int


class BacktestDetail(BacktestSummary):
    avg_trade_pnl: float = 0.0
    best_trade_pnl: float = 0.0
    worst_trade_pnl: float = 0.0
    config: dict = {}
    equity_curve: list[dict] = []


# ── Report schemas ───────────────────────────────────────────────────────────

class DailyReportSummary(BaseModel):
    id: int
    report_date: date
    traders_scanned: int
    traders_passing: int
    summary: str | None = None


class DailyReportDetail(DailyReportSummary):
    top_10: list[dict] = []
    created_at: datetime | None = None


# ── Pipeline schemas ─────────────────────────────────────────────────────────

class PipelineStatus(BaseModel):
    running: bool = False
    last_run: datetime | None = None
    last_result: dict | None = None
