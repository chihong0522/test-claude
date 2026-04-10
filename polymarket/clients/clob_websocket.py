"""Polymarket CLOB WebSocket client (market channel).

Endpoint: wss://ws-subscriptions-clob.polymarket.com/ws/market

Subscribes to real-time events for specific token (asset) IDs:
- book: orderbook snapshot (sent once on subscription + on trade impact)
- price_change: new order placed or cancelled
- last_trade_price: a trade was matched (what we use as signal)
- tick_size_change: minimum tick changed
- best_bid_ask: best bid/ask updated (requires custom_feature_enabled)

IMPORTANT: The market channel does NOT include wallet addresses. It only
tells us WHAT happened (price, size, side), not WHO did it. For wallet-based
voting logic, combine this with HTTP polling of /trades.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

WS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


@dataclass
class WSEvent:
    """Normalized WebSocket event from the market channel."""

    event_type: str  # 'book', 'price_change', 'last_trade_price', ...
    asset_id: str
    market: str  # condition_id
    raw: dict
    # Populated for 'last_trade_price' events
    price: float = 0.0
    size: float = 0.0
    side: str = ""  # BUY or SELL
    timestamp: int = 0
    # Populated for 'book' events
    best_bid: float = 0.0
    best_ask: float = 0.0


def _parse_event(data: dict) -> WSEvent | None:
    """Convert raw WS message dict into normalized WSEvent."""
    event_type = data.get("event_type", "")
    if not event_type:
        return None

    ev = WSEvent(
        event_type=event_type,
        asset_id=data.get("asset_id") or "",
        market=data.get("market") or data.get("condition_id") or "",
        raw=data,
    )

    if event_type == "last_trade_price":
        try:
            ev.price = float(data.get("price", 0))
            ev.size = float(data.get("size", 0))
            ev.side = (data.get("side") or "").upper()
            ev.timestamp = int(data.get("timestamp", 0))
        except (TypeError, ValueError):
            pass
    elif event_type == "book":
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        try:
            if bids:
                ev.best_bid = max(float(b.get("price", 0)) for b in bids)
            if asks:
                ev.best_ask = min(float(a.get("price", 0)) for a in asks)
        except (TypeError, ValueError):
            pass
    elif event_type == "best_bid_ask":
        try:
            ev.best_bid = float(data.get("best_bid", 0))
            ev.best_ask = float(data.get("best_ask", 0))
        except (TypeError, ValueError):
            pass
    elif event_type == "price_change":
        # price_change has a list in `price_changes` — normalize the first
        changes = data.get("price_changes") or []
        if changes:
            first = changes[0]
            try:
                ev.asset_id = first.get("asset_id", "") or ev.asset_id
                ev.price = float(first.get("price", 0))
                ev.size = float(first.get("size", 0))
                ev.side = (first.get("side") or "").upper()
            except (TypeError, ValueError):
                pass

    return ev


class MarketWebSocketClient:
    """Subscribes to Polymarket CLOB market WS and yields normalized events.

    Handles reconnection automatically. If the connection drops, it
    reconnects and re-subscribes to all previously subscribed assets.
    """

    def __init__(
        self,
        url: str = WS_MARKET_URL,
        reconnect_delay: float = 2.0,
        ping_interval: float = 10.0,
    ):
        self.url = url
        self.reconnect_delay = reconnect_delay
        self.ping_interval = ping_interval
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._subscribed_assets: set[str] = set()
        self._running = False
        self._stop_event = asyncio.Event()

    async def _connect_and_subscribe(self):
        """Establish WS connection and re-subscribe."""
        self._ws = await websockets.connect(
            self.url,
            ping_interval=self.ping_interval,
            ping_timeout=20,
            close_timeout=5,
        )
        logger.info("Connected to %s", self.url)
        if self._subscribed_assets:
            await self._send_subscribe(list(self._subscribed_assets))

    async def _send_subscribe(self, asset_ids: list[str]):
        """Send subscribe message."""
        msg = {
            "assets_ids": asset_ids,
            "type": "market",
            "custom_feature_enabled": True,
        }
        if self._ws:
            await self._ws.send(json.dumps(msg))
            logger.info("Subscribed to %d assets", len(asset_ids))

    def _ws_is_open(self) -> bool:
        """Check if the WS connection is currently open (version-agnostic)."""
        if self._ws is None:
            return False
        # websockets v16: .state is a State enum; State.OPEN has .name == 'OPEN'
        state = getattr(self._ws, "state", None)
        if state is not None:
            name = getattr(state, "name", str(state))
            return name == "OPEN"
        # Older websockets: .closed attribute
        closed = getattr(self._ws, "closed", None)
        if closed is not None:
            return not closed
        return True  # assume open if can't determine

    async def subscribe(self, asset_ids: list[str]):
        """Add asset IDs to the subscription (and send if connected)."""
        new = [a for a in asset_ids if a not in self._subscribed_assets]
        if not new:
            return
        self._subscribed_assets.update(new)
        if self._ws is not None and self._ws_is_open():
            await self._send_subscribe(new)

    async def unsubscribe_all(self):
        """Clear subscriptions and disconnect. Reconnect will have empty subs."""
        self._subscribed_assets.clear()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def resubscribe(self, asset_ids: list[str]):
        """Replace the current subscription with a new set of asset IDs.

        This closes the existing connection (which flushes the old
        subscription set on the server) and reconnects with only the new IDs.
        Use this on market transitions to avoid accumulating stale subs.
        """
        self._subscribed_assets = set(asset_ids)
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        # Next iteration of events() will reconnect with the new set

    async def events(self) -> AsyncIterator[WSEvent]:
        """Async iterator yielding normalized WSEvents.

        Auto-reconnects on disconnect. The consumer should call subscribe()
        before iterating to set initial asset subscriptions.
        """
        self._running = True
        while self._running and not self._stop_event.is_set():
            try:
                if self._ws is None or not self._ws_is_open():
                    await self._connect_and_subscribe()

                assert self._ws is not None
                async for message in self._ws:
                    try:
                        data = json.loads(message)
                    except json.JSONDecodeError:
                        logger.warning("Non-JSON message: %r", message[:200])
                        continue

                    # Polymarket sometimes sends array, sometimes object
                    if isinstance(data, list):
                        for item in data:
                            ev = _parse_event(item)
                            if ev is not None:
                                yield ev
                    elif isinstance(data, dict):
                        ev = _parse_event(data)
                        if ev is not None:
                            yield ev

            except ConnectionClosed as e:
                logger.warning("WS closed: %s — reconnecting in %.1fs", e, self.reconnect_delay)
                self._ws = None
                await asyncio.sleep(self.reconnect_delay)
            except Exception as e:
                logger.error("WS error: %s — reconnecting in %.1fs", e, self.reconnect_delay)
                self._ws = None
                await asyncio.sleep(self.reconnect_delay)

    async def close(self):
        """Stop listening and close the connection."""
        self._running = False
        self._stop_event.set()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
