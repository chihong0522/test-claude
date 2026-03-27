"""Step 5 — Monitor: WebSocket-based fill monitoring + position management.

Listens for:
  - orderbookUpdate: detect midpoint drift → reprice if orders outside LP zone
  - positions: detect fills on our orders

Reprice triggers:
  1. Midpoint drift: midpoint moved > threshold → orders no longer in LP reward zone
  2. Time-based: orders older than reprice_interval without fill
  3. One-side fill: if one side fills, reprice the other side more aggressively

On fill:
  - If BOTH sides filled → profit realized, record it
  - If ONE side filled → we hold exposure, may reprice other side
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field

from limitless_sdk.websocket import WebSocketClient, WebSocketConfig

from bot.config import Config

log = logging.getLogger(__name__)


@dataclass
class TrackedPosition:
    """An active dual-ask position being monitored."""
    slug: str
    yes_token: str
    no_token: str
    yes_order_id: str | None
    no_order_id: str | None
    exchange_address: str
    split_amount: float

    # Prices at which orders were placed
    yes_ask_price: float = 0.0
    no_ask_price: float = 0.0

    # Midpoint at time of order placement
    placed_midpoint: float = 0.0

    # Fill tracking
    yes_filled: bool = False
    no_filled: bool = False

    created_at: float = field(default_factory=time.time)
    last_reprice_at: float = field(default_factory=time.time)


class Monitor:
    """WebSocket-based fill monitor with midpoint drift repricing."""

    def __init__(self, cfg: Config, executor=None):
        self.cfg = cfg
        self.executor = executor  # Set after construction to avoid circular import
        self.positions: dict[str, TrackedPosition] = {}  # slug -> position
        self.ws: WebSocketClient | None = None
        self._running = False
        self._reprice_lock = asyncio.Lock()

    def set_executor(self, executor):
        """Set executor reference (avoids circular import)."""
        self.executor = executor

    def track(self, result, split_amount: float, opportunity=None):
        """Start tracking a new executed position."""
        pos = TrackedPosition(
            slug=result.slug,
            yes_token=opportunity.yes_token if opportunity else "",
            no_token=opportunity.no_token if opportunity else "",
            yes_order_id=result.yes_order_id,
            no_order_id=result.no_order_id,
            exchange_address=opportunity.exchange_address if opportunity else "",
            split_amount=split_amount,
            yes_ask_price=opportunity.yes_ask_price if opportunity else 0,
            no_ask_price=opportunity.no_ask_price if opportunity else 0,
            placed_midpoint=opportunity.midpoint if opportunity else 0,
        )
        self.positions[result.slug] = pos
        log.info(
            "[%s] Tracking: YES=%s @%.3f  NO=%s @%.3f  mid=%.3f",
            result.slug,
            result.yes_order_id, pos.yes_ask_price,
            result.no_order_id, pos.no_ask_price,
            pos.placed_midpoint,
        )

    @property
    def open_count(self) -> int:
        return len(self.positions)

    @property
    def total_exposure(self) -> float:
        return sum(p.split_amount for p in self.positions.values())

    # ── WebSocket lifecycle ────────────────────────────────────────────

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
            await self._resubscribe_all()

        @self.ws.on("orderbookUpdate")
        async def on_orderbook(data):
            await self._handle_orderbook_update(data)

        @self.ws.on("positions")
        async def on_positions(data):
            self._handle_position_update(data)

        await self.ws.connect()

    async def _resubscribe_all(self):
        """(Re)subscribe to all tracked market slugs."""
        slugs = list(self.positions.keys())
        if not slugs:
            return
        await self.ws.subscribe(
            "subscribe_market_prices",
            {"marketSlugs": slugs},
        )
        await self.ws.subscribe(
            "subscribe_positions",
            {"marketSlugs": slugs},
        )
        log.info("Subscribed to %d markets", len(slugs))

    async def subscribe_market(self, slug: str):
        """Add a new market to WebSocket subscriptions."""
        if self.ws:
            all_slugs = list(self.positions.keys())
            if slug not in all_slugs:
                all_slugs.append(slug)
            await self.ws.subscribe(
                "subscribe_market_prices",
                {"marketSlugs": all_slugs},
            )
            await self.ws.subscribe(
                "subscribe_positions",
                {"marketSlugs": all_slugs},
            )

    # ── Orderbook update → midpoint drift detection ────────────────────

    async def _handle_orderbook_update(self, data: dict):
        """
        On each orderbook update, check if midpoint has drifted enough
        to push our orders outside the LP reward zone.

        If so, cancel + reprice at new optimal prices.
        """
        slug = data.get("marketSlug", "")
        if slug not in self.positions:
            return

        pos = self.positions[slug]

        # Parse new orderbook
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        if not bids or not asks:
            return

        best_bid = float(bids[0]["price"]) if bids else 0
        best_ask = float(asks[0]["price"]) if asks else 1
        new_midpoint = (best_bid + best_ask) / 2

        drift = abs(new_midpoint - pos.placed_midpoint)

        if drift < self.cfg.midpoint_drift_threshold:
            return  # Within tolerance, no action needed

        # ── Check if our orders are still within LP spread limit ──

        # YES ask distance from new midpoint
        yes_distance = abs(pos.yes_ask_price - new_midpoint)
        # NO ask distance from new NO midpoint (1 - midpoint)
        no_midpoint = 1.0 - new_midpoint
        no_distance = abs(pos.no_ask_price - no_midpoint)

        yes_out_of_zone = yes_distance > self.cfg.lp_spread_limit
        no_out_of_zone = no_distance > self.cfg.lp_spread_limit

        if not yes_out_of_zone and not no_out_of_zone:
            return  # Still in LP zone despite drift

        log.info(
            "[%s] MIDPOINT DRIFT: %.3f → %.3f (Δ%.3f) | "
            "YES out=%s (dist=%.3f) NO out=%s (dist=%.3f) → REPRICE",
            slug, pos.placed_midpoint, new_midpoint, drift,
            yes_out_of_zone, yes_distance,
            no_out_of_zone, no_distance,
        )

        await self._reprice_position(slug, new_midpoint, best_bid, best_ask)

    async def _reprice_position(
        self, slug: str, new_midpoint: float, best_bid: float, best_ask: float
    ):
        """Cancel old orders and place new ones near the new midpoint."""
        if not self.executor:
            log.warning("[%s] No executor set, cannot reprice", slug)
            return

        async with self._reprice_lock:
            pos = self.positions.get(slug)
            if not pos:
                return

            # Skip if already filled
            if pos.yes_filled and pos.no_filled:
                return

            # Calculate new prices: place asks at best_bid to be maker
            # YES ask: at the current best bid (sits on book as maker)
            new_yes_price = best_bid
            # NO ask: mirror from YES side. NO_best_bid = 1 - YES_best_ask
            new_no_price = round(1.0 - best_ask, 4)

            # Sanity: combined must still be > 1.0
            if new_yes_price + new_no_price <= 1.0:
                log.info(
                    "[%s] New prices not profitable (%.3f + %.3f = %.3f), skip reprice",
                    slug, new_yes_price, new_no_price,
                    new_yes_price + new_no_price,
                )
                return

            # Only cancel+reprice unfilled sides
            cancel_yes = pos.yes_order_id if not pos.yes_filled else None
            cancel_no = pos.no_order_id if not pos.no_filled else None

            new_yes_id, new_no_id = await self.executor.cancel_and_reprice(
                slug=slug,
                old_yes_order_id=cancel_yes,
                old_no_order_id=cancel_no,
                yes_token=pos.yes_token,
                no_token=pos.no_token,
                new_yes_price=new_yes_price,
                new_no_price=new_no_price,
                size=pos.split_amount,
                exchange_address=pos.exchange_address,
            )

            # Update tracked position
            if not pos.yes_filled:
                pos.yes_order_id = new_yes_id
                pos.yes_ask_price = new_yes_price
            if not pos.no_filled:
                pos.no_order_id = new_no_id
                pos.no_ask_price = new_no_price
            pos.placed_midpoint = new_midpoint
            pos.last_reprice_at = time.time()

            log.info(
                "[%s] Repriced: YES=%.3f  NO=%.3f  new_mid=%.3f  combined=%.3f",
                slug, pos.yes_ask_price, pos.no_ask_price,
                new_midpoint, pos.yes_ask_price + pos.no_ask_price,
            )

    # ── Position update → fill detection ───────────────────────────────

    def _handle_position_update(self, data: dict):
        """Detect fills from position balance changes."""
        slug = data.get("marketSlug", "")
        if slug not in self.positions:
            return

        pos = self.positions[slug]
        positions_data = data.get("positions", [])

        for p in positions_data:
            token_id = str(p.get("tokenId", ""))
            balance = int(p.get("ctfBalance", "0"))

            if token_id == pos.yes_token and balance == 0 and not pos.yes_filled:
                pos.yes_filled = True
                log.info("[%s] YES side FILLED!", slug)

            if token_id == pos.no_token and balance == 0 and not pos.no_filled:
                pos.no_filled = True
                log.info("[%s] NO side FILLED!", slug)

        self._check_completion(slug)

    def _check_completion(self, slug: str):
        """Check if both sides of a position are filled."""
        pos = self.positions.get(slug)
        if not pos:
            return

        if pos.yes_filled and pos.no_filled:
            profit = pos.yes_ask_price + pos.no_ask_price - 1.0
            log.info(
                "[%s] BOTH SIDES FILLED! Profit=%.4f per share on $%.2f split "
                "(est $%.2f gross)",
                slug, profit, pos.split_amount, profit * pos.split_amount,
            )
            del self.positions[slug]
        elif pos.yes_filled != pos.no_filled:
            filled_side = "YES" if pos.yes_filled else "NO"
            open_side = "NO" if pos.yes_filled else "YES"
            log.info(
                "[%s] %s filled, %s still open — holding exposure",
                slug, filled_side, open_side,
            )

    # ── Time-based stale order check ───────────────────────────────────

    async def check_stale_orders(self, snapshots: list | None = None):
        """
        Cancel and reprice orders that have been sitting too long.

        Called from the main loop. If snapshots are provided, uses fresh
        orderbook data for repricing; otherwise just logs.
        """
        now = time.time()
        stale_slugs = [
            slug for slug, pos in self.positions.items()
            if now - pos.last_reprice_at > self.cfg.reprice_interval
            and not (pos.yes_filled and pos.no_filled)
        ]

        if not stale_slugs:
            return

        log.info("Found %d stale positions to reprice", len(stale_slugs))

        if not snapshots or not self.executor:
            for slug in stale_slugs:
                log.info("[%s] Stale (>%ds), will reprice on next orderbook update",
                         slug, self.cfg.reprice_interval)
            return

        # Use fresh snapshot data to reprice
        snap_map = {s.slug: s for s in snapshots}
        for slug in stale_slugs:
            snap = snap_map.get(slug)
            if not snap or not snap.bids or not snap.asks:
                continue
            best_bid = snap.bids[0]["price"]
            best_ask = snap.asks[0]["price"]
            new_mid = (best_bid + best_ask) / 2
            await self._reprice_position(slug, new_mid, best_bid, best_ask)

    def stop(self):
        self._running = False
