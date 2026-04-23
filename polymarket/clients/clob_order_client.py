"""Polymarket CLOB order-submission client for live trading.

Wraps the official `py_clob_client` SDK for async-compatible order
placement, cancellation, and balance queries. All EIP-712 signing is
handled by the SDK internally — you just provide the private key and
CLOB API credentials.

INSTALL DEPS (not included in base requirements):
    pip install py-clob-client eth-account python-dotenv

CREDENTIALS (in .env):
    POLYMARKET_PRIVATE_KEY=0x...
    POLYMARKET_API_KEY=...
    POLYMARKET_API_SECRET=...
    POLYMARKET_API_PASSPHRASE=...
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)

CLOB_HOST = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137

_MISSING_DEPS_MSG = (
    "Live trading requires: pip install py-clob-client eth-account python-dotenv\n"
    "Then set POLYMARKET_PRIVATE_KEY, POLYMARKET_API_KEY, POLYMARKET_API_SECRET, "
    "POLYMARKET_API_PASSPHRASE in .env or environment."
)


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class OrderResult:
    order_id: str
    status: str  # "LIVE", "MATCHED", "MINED", "CONFIRMED", "FAILED"
    filled_size: float = 0.0
    avg_price: float = 0.0
    raw: dict | None = None


class ClobOrderClient:
    """Async-friendly wrapper around py_clob_client for order submission."""

    def __init__(
        self,
        private_key: str | None = None,
        api_key: str | None = None,
        api_secret: str | None = None,
        api_passphrase: str | None = None,
    ):
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass

        self._private_key = private_key or os.getenv("POLYMARKET_PRIVATE_KEY", "")
        self._api_key = api_key or os.getenv("POLYMARKET_API_KEY", "")
        self._api_secret = api_secret or os.getenv("POLYMARKET_API_SECRET", "")
        self._api_passphrase = api_passphrase or os.getenv("POLYMARKET_API_PASSPHRASE", "")

        if not self._private_key:
            raise RuntimeError(
                "POLYMARKET_PRIVATE_KEY not set. " + _MISSING_DEPS_MSG
            )

        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
        except ImportError as e:
            raise ImportError(_MISSING_DEPS_MSG) from e

        creds = ApiCreds(
            api_key=self._api_key,
            api_secret=self._api_secret,
            api_passphrase=self._api_passphrase,
        )
        self._client = ClobClient(
            host=CLOB_HOST,
            key=self._private_key,
            chain_id=POLYGON_CHAIN_ID,
            creds=creds,
        )
        logger.info("ClobOrderClient initialized (chain=%d)", POLYGON_CHAIN_ID)

    async def place_market_buy(
        self,
        token_id: str,
        amount_usd: float,
        price: float,
    ) -> OrderResult:
        """Place a limit order at `price` for `amount_usd` worth of `token_id`.

        Polymarket has no market orders — a "market buy" is a limit order at
        the current best ask. The bot should pass the WS best_ask as `price`.

        Returns an OrderResult immediately (order may not be filled yet).
        Use `wait_for_fill()` to poll until CONFIRMED or timeout.
        """
        from py_clob_client.order import OrderArgs
        from py_clob_client.constants import BUY

        size = round(amount_usd / price, 2)
        order_args = OrderArgs(
            price=price,
            size=size,
            side=BUY,
            token_id=token_id,
        )

        resp = await asyncio.to_thread(
            self._client.create_and_post_order, order_args
        )

        order_id = resp.get("orderID") or resp.get("id") or ""
        status = resp.get("status", "UNKNOWN")
        logger.info(
            "Order placed: %s %s @ %.4f (size=%.2f) → %s %s",
            "BUY", token_id[:12], price, size, status, order_id,
        )
        return OrderResult(
            order_id=order_id,
            status=status,
            filled_size=0.0,
            avg_price=price,
            raw=resp,
        )

    async def place_market_sell(
        self,
        token_id: str,
        size: float,
        price: float,
    ) -> OrderResult:
        """Place a limit sell at `price` for `size` shares of `token_id`.

        For exits / profit-takes, pass the WS best_bid as `price`.
        """
        from py_clob_client.order import OrderArgs
        from py_clob_client.constants import SELL

        order_args = OrderArgs(
            price=price,
            size=round(size, 2),
            side=SELL,
            token_id=token_id,
        )

        resp = await asyncio.to_thread(
            self._client.create_and_post_order, order_args
        )

        order_id = resp.get("orderID") or resp.get("id") or ""
        status = resp.get("status", "UNKNOWN")
        logger.info(
            "Order placed: SELL %s @ %.4f (size=%.2f) → %s %s",
            token_id[:12], price, size, status, order_id,
        )
        return OrderResult(
            order_id=order_id,
            status=status,
            filled_size=0.0,
            avg_price=price,
            raw=resp,
        )

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order. Returns True if cancellation succeeded."""
        try:
            resp = await asyncio.to_thread(
                self._client.cancel, order_id
            )
            logger.info("Order cancelled: %s → %s", order_id, resp)
            return True
        except Exception as e:
            logger.error("Cancel failed for %s: %s", order_id, e)
            return False

    async def get_order(self, order_id: str) -> OrderResult:
        """Fetch current status of an order."""
        resp = await asyncio.to_thread(
            self._client.get_order, order_id
        )
        return OrderResult(
            order_id=order_id,
            status=resp.get("status", "UNKNOWN"),
            filled_size=float(resp.get("size_matched", 0)),
            avg_price=float(resp.get("price", 0)),
            raw=resp,
        )

    async def wait_for_fill(
        self,
        order_id: str,
        timeout_sec: float = 15.0,
        poll_interval: float = 1.0,
    ) -> OrderResult:
        """Poll until the order reaches MATCHED/CONFIRMED or times out."""
        import time
        deadline = time.time() + timeout_sec
        last_result = OrderResult(order_id=order_id, status="UNKNOWN")
        while time.time() < deadline:
            last_result = await self.get_order(order_id)
            if last_result.status in ("MATCHED", "MINED", "CONFIRMED"):
                return last_result
            if last_result.status in ("FAILED", "CANCELLED"):
                return last_result
            await asyncio.sleep(poll_interval)
        logger.warning("Order %s timed out (last status: %s)", order_id, last_result.status)
        return last_result

    async def get_usdc_balance(self) -> float:
        """Get the USDC balance available for trading."""
        try:
            resp = await asyncio.to_thread(
                self._client.get_balance_allowance
            )
            # py_clob_client returns balance in USDC (6 decimals)
            balance = float(resp.get("balance", 0)) / 1e6
            return balance
        except Exception as e:
            logger.error("Balance fetch failed: %s", e)
            return 0.0

    async def get_positions(self) -> list[dict]:
        """Get current open positions."""
        try:
            resp = await asyncio.to_thread(
                self._client.get_positions
            )
            return resp if isinstance(resp, list) else []
        except Exception as e:
            logger.error("Positions fetch failed: %s", e)
            return []
