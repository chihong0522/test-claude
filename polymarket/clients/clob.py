"""CLOB API client — orderbook, prices, spreads (public read-only)."""

from __future__ import annotations

from typing import Any

from polymarket.clients.base import BaseClient
from polymarket.config import settings


class CLOBClient(BaseClient):
    def __init__(self) -> None:
        super().__init__(
            base_url=settings.clob_api_url,
            rate_limit=settings.clob_rate_limit,
        )

    async def get_orderbook(self, token_id: str) -> dict:
        return await self.get("/book", {"token_id": token_id})

    async def get_price(self, token_id: str, side: str = "buy") -> float | None:
        try:
            data = await self.get("/price", {"token_id": token_id, "side": side})
            return float(data.get("price", 0)) if isinstance(data, dict) else None
        except Exception:
            return None

    async def get_midpoint(self, token_id: str) -> float | None:
        try:
            data = await self.get("/midpoint", {"token_id": token_id})
            return float(data.get("mid", 0)) if isinstance(data, dict) else None
        except Exception:
            return None

    async def get_spread(self, token_id: str) -> dict | None:
        try:
            return await self.get("/spread", {"token_id": token_id})
        except Exception:
            return None

    async def get_price_history(
        self,
        token_id: str,
        interval: str = "max",
        fidelity: int = 60,
    ) -> list[dict]:
        """Get historical prices. Returns [{t: timestamp, p: price}, ...]."""
        try:
            data = await self.get(
                "/prices-history",
                {"market": token_id, "interval": interval, "fidelity": fidelity},
            )
            if isinstance(data, dict):
                return data.get("history", [])
            return data if isinstance(data, list) else []
        except Exception:
            return []

    async def get_spread_pct(self, token_id: str) -> float | None:
        """Compute bid-ask spread as a percentage."""
        book = await self.get_orderbook(token_id)
        if not book:
            return None
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if not bids or not asks:
            return None
        try:
            best_bid = float(bids[0]["price"])
            best_ask = float(asks[0]["price"])
            if best_bid <= 0:
                return None
            return (best_ask - best_bid) / best_bid * 100
        except (IndexError, KeyError, ValueError, ZeroDivisionError):
            return None

    async def get_orderbook_depth(self, token_id: str, levels: int = 5) -> dict[str, float]:
        """Get total $ depth on each side of the book (top N levels)."""
        book = await self.get_orderbook(token_id)
        result: dict[str, Any] = {"bid_depth": 0.0, "ask_depth": 0.0}
        if not book:
            return result
        for side_key, result_key in [("bids", "bid_depth"), ("asks", "ask_depth")]:
            for level in (book.get(side_key) or [])[:levels]:
                try:
                    result[result_key] += float(level["price"]) * float(level["size"])
                except (KeyError, ValueError):
                    continue
        return result
