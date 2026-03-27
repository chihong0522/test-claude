"""Bot configuration — loads from .env and provides defaults."""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # ── API ────────────────────────────────────────────────────────────
    api_key: str = os.getenv("LIMITLESS_API_KEY", "")
    api_base: str = "https://api.limitless.exchange"
    ws_url: str = "wss://ws.limitless.exchange"

    # ── Wallet ─────────────────────────────────────────────────────────
    private_key: str = os.getenv("PRIVATE_KEY", "")

    # ── Markets to trade (slugs) ───────────────────────────────────────
    # Comma-separated in env, e.g. "btc-above-100k,eth-above-4k"
    market_slugs: list[str] = field(default_factory=lambda: [
        s.strip()
        for s in os.getenv("MARKET_SLUGS", "").split(",")
        if s.strip()
    ])

    # ── Strategy params ────────────────────────────────────────────────
    # How many USDC to split per round (6 decimals on-chain, but we use float here)
    split_amount_usdc: float = float(os.getenv("SPLIT_AMOUNT_USDC", "10"))

    # Minimum combined ask price (YES_ask + NO_ask) to enter.
    # Must be > 1.0 to be profitable after split cost of $1.
    min_combined_ask: float = float(os.getenv("MIN_COMBINED_ASK", "1.02"))

    # Max spread from midpoint for LP reward eligibility (in cents)
    lp_spread_limit: float = float(os.getenv("LP_SPREAD_LIMIT", "0.03"))

    # Minimum order size for LP rewards
    min_lp_size: float = float(os.getenv("MIN_LP_SIZE", "100"))

    # How long to wait before repricing stale orders (seconds)
    reprice_interval: int = int(os.getenv("REPRICE_INTERVAL", "60"))

    # ── Risk ───────────────────────────────────────────────────────────
    max_open_positions: int = int(os.getenv("MAX_OPEN_POSITIONS", "5"))
    max_exposure_usdc: float = float(os.getenv("MAX_EXPOSURE_USDC", "100"))
