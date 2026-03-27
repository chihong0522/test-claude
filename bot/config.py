"""Bot configuration — loads from .env and provides defaults."""

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # RPC
    base_rpc_url: str = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")
    base_ws_url: str = os.getenv("BASE_WS_URL", "")

    # Wallet
    private_key: str = os.getenv("PRIVATE_KEY", "")
    wallet_address: str = os.getenv("WALLET_ADDRESS", "")

    # Limitless contracts (placeholder — fill after research)
    router_address: str = os.getenv("LIMITLESS_ROUTER_ADDRESS", "")
    orderbook_address: str = os.getenv("LIMITLESS_ORDERBOOK_ADDRESS", "")
    ctf_address: str = os.getenv("LIMITLESS_CTF_ADDRESS", "")

    # USDC on Base
    usdc_address: str = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

    # Strategy
    collateral_amount: int = int(os.getenv("COLLATERAL_AMOUNT_USDC", "10"))
    min_spread_bps: int = int(os.getenv("MIN_SPREAD_BPS", "50"))
    max_position: int = int(os.getenv("MAX_POSITION_USDC", "100"))
    gas_limit: int = int(os.getenv("GAS_LIMIT", "500000"))

    # Polling
    poll_interval: int = int(os.getenv("POLL_INTERVAL_SECONDS", "5"))
