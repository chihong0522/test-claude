"""Step 5 — Monitor: WebSocket-based fill monitoring + position management.

Listens for:
  - orderbookUpdate: detect when our orders may need repricing
  - positions: detect fills on our orders

On fill:
  - If BOTH sides filled → profit realized, record it
  - If ONE side filled → we hold exposure, may need to reprice the other side
  - If orders go stale → cancel and reprice
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field

from limitless_sdk.websocket import WebSocketClient, WebSocketConfig

from bot.config import Config
from bot.executor import ExecutionResult

log = logging.getLogger(__name__)


@dataclass
class TrackedPosition:
    """An active dual-ask position being monitored."""
    slug: str
    yes_order_id: str | None
    no_order_id: str | None
    split_amount: float
    yes_filled: bool = False
    no_filled: bool = False
    created_at: float = field(default_factory=time.time)


class Monitor:
    """WebSocket-based fill monitor and position manager."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.positions: dict[str, TrackedPosition] = {}  # slug -> position
        self.ws: WebSocketClient | None = None
        self._running = False

    def track(self, result: ExecutionResult, split_amount: float):
        """Start tracking a new executed position."""
        pos = TrackedPosition(
            slug=result.slug,
            yes_order_id=result.yes_order_id,
            no_order_id=result.no_order_id,
            split_amount=split_amount,
        )
        self.positions[result.slug] = pos
        log.info("[%s] Tracking: YES=%s  NO=%s", result.slug,
                 result.yes_order_id, result.no_order_id)

    @property
    def open_count(self) -> int:
        return len(self.positions)

    @property
    def total_exposure(self) -> float:
        return sum(p.split_amount for p in self.positions.values())

    async def start(self):
        """Start WebSocket monitoring loop."""
        config = WebSocketConfig(
            url=self.cfg.ws_url,
            auto_reconnect=True,
            reconnect_delay=5,
        )
        self.ws = WebSocketClient(config)
        self._running = True

        @self.ws.on("connect")
        async def on_connect():
            log.info("WebSocket connected")
            slugs = list(self.positions.keys())
            if slugs:
                await self.ws.subscribe(
                    "subscribe_market_prices",
                    {"marketSlugs": slugs},
                )
                await self.ws.subscribe(
                    "subscribe_positions",
                    {"marketSlugs": slugs},
                )
                log.info("Subscribed to %d markets", len(slugs))

        @self.ws.on("orderbookUpdate")
        async def on_orderbook(data):
            slug = data.get("marketSlug", "")
            if slug in self.positions:
                log.debug("[%s] Orderbook update received", slug)
                # Could trigger repricing logic here

        @self.ws.on("positions")
        async def on_positions(data):
            slug = data.get("marketSlug", "")
            if slug not in self.positions:
                return

            pos = self.positions[slug]
            positions_data = data.get("positions", [])

            for p in positions_data:
                token_id = p.get("tokenId", "")
                balance = int(p.get("ctfBalance", "0"))

                # If balance dropped, our sell order was (partially) filled
                if balance == 0:
                    log.info("[%s] Token %s fully sold!", slug, token_id[:12])

            self._check_completion(slug)

        await self.ws.connect()

    def _check_completion(self, slug: str):
        """Check if both sides of a position are filled."""
        pos = self.positions.get(slug)
        if not pos:
            return

        if pos.yes_filled and pos.no_filled:
            log.info(
                "[%s] BOTH SIDES FILLED — profit realized on $%.2f split",
                slug, pos.split_amount,
            )
            del self.positions[slug]

    async def check_stale_orders(self):
        """Cancel and reprice orders that have been open too long."""
        now = time.time()
        stale = [
            slug for slug, pos in self.positions.items()
            if now - pos.created_at > self.cfg.reprice_interval
            and not (pos.yes_filled and pos.no_filled)
        ]
        for slug in stale:
            log.info("[%s] Order stale (>%ds), needs repricing", slug,
                     self.cfg.reprice_interval)
            # Repricing would be done by the main loop:
            # 1. Cancel old orders via API
            # 2. Re-scan, re-analyze, re-execute

    async def subscribe_market(self, slug: str):
        """Add a new market to WebSocket subscriptions."""
        if self.ws:
            all_slugs = list(self.positions.keys())
            if slug not in all_slugs:
                all_slugs.append(slug)
            # Subscriptions replace previous — must send all at once
            await self.ws.subscribe(
                "subscribe_market_prices",
                {"marketSlugs": all_slugs},
            )
            await self.ws.subscribe(
                "subscribe_positions",
                {"marketSlugs": all_slugs},
            )

    def stop(self):
        self._running = False
