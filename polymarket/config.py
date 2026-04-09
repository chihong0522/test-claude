from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="PM_")

    # Database
    database_url: str = "sqlite+aiosqlite:///./polymarket_research.db"

    # API base URLs
    gamma_api_url: str = "https://gamma-api.polymarket.com"
    data_api_url: str = "https://data-api.polymarket.com"
    clob_api_url: str = "https://clob.polymarket.com"
    oddpool_api_url: str = "https://api.oddpool.com"
    polymarketscan_api_url: str = (
        "https://gzydspfquuaudqeztorw.supabase.co/functions/v1/public-api"
    )

    # Rate limiting (requests per 10-second window)
    gamma_rate_limit: int = 2000
    data_rate_limit: int = 2000
    clob_rate_limit: int = 7500

    # Pipeline config
    pipeline_schedule_hour: int = 6
    pipeline_schedule_minute: int = 0
    max_concurrent_fetches: int = 5
    leaderboard_top_n: int = 500
    min_trades_for_scoring: int = 50

    # Backtest defaults
    default_initial_capital: float = 3000.0
    default_slippage_bps: int = 30
    default_delay_seconds: int = 30
    default_position_pct: float = 0.02

    # Notifications
    discord_webhook_url: str | None = None

    # Server
    host: str = "0.0.0.0"
    port: int = 8000


settings = Settings()
