"""Limitless SDK wrapper — market data, orders, and WebSocket.

Uses the official `limitless-sdk` package plus raw EIP-712 signing for
the split-dual-ask strategy.

Contract addresses (Base mainnet):
  USDC:          0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
  CTF (ERC-1155): 0xC9c98965297Bc527861c898329Ee280632B76e18
  Exchange v3:   0x05c748E2f4DcDe0ec9Fa8DDc40DE6b867f923fa5
"""

from limitless_sdk.api import HttpClient
from limitless_sdk.markets import MarketFetcher

from bot.config import Config


# ── Contracts on Base ──────────────────────────────────────────────────
USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
CTF_ADDRESS = "0xC9c98965297Bc527861c898329Ee280632B76e18"

# All three exchange versions — query all for complete data
EXCHANGES = {
    "v1": "0xa4409D988CA2218d956BeEFD3874100F444f0DC3",
    "v2": "0xF1De958F8641448A5ba78c01f434085385Af096D",
    "v3": "0x05c748E2f4DcDe0ec9Fa8DDc40DE6b867f923fa5",
}


async def create_http_client(cfg: Config) -> HttpClient:
    """Create an authenticated HTTP client."""
    return HttpClient(api_key=cfg.api_key)


async def create_market_fetcher(cfg: Config) -> MarketFetcher:
    """Create a MarketFetcher (caches venue addresses for order signing)."""
    client = await create_http_client(cfg)
    return MarketFetcher(client)
