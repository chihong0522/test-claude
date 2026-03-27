"""Step 1 — Scanner: fetch orderbook for each configured market.

Returns structured orderbook data for the analyzer.
"""

import logging
from dataclasses import dataclass

from limitless_sdk.markets import MarketFetcher

from bot.config import Config

log = logging.getLogger(__name__)


@dataclass
class OrderbookSnapshot:
    slug: str
    yes_token: str
    no_token: str
    bids: list[dict]   # [{"price": float, "size": float}, ...]
    asks: list[dict]
    midpoint: float
    exchange_address: str  # venue.exchange for EIP-712 signing


async def scan_market(fetcher: MarketFetcher, slug: str) -> OrderbookSnapshot | None:
    """Fetch market info + orderbook for a single slug."""
    try:
        # get_market caches venue info needed for order signing
        market = await fetcher.get_market(slug)
        book = await fetcher.get_orderbook(slug)

        bids = book.get("bids", [])
        asks = book.get("asks", [])

        best_bid = bids[0]["price"] if bids else 0.0
        best_ask = asks[0]["price"] if asks else 1.0
        midpoint = (best_bid + best_ask) / 2

        return OrderbookSnapshot(
            slug=slug,
            yes_token=market.tokens.yes,
            no_token=market.tokens.no,
            bids=bids,
            asks=asks,
            midpoint=midpoint,
            exchange_address=market.venue.exchange if hasattr(market, "venue") else "",
        )
    except Exception as e:
        log.warning("scan_market(%s) failed: %s", slug, e)
        return None


async def scan_all(fetcher: MarketFetcher, cfg: Config) -> list[OrderbookSnapshot]:
    """Scan all configured markets. Returns snapshots (skips failures)."""
    results = []
    for slug in cfg.market_slugs:
        snap = await scan_market(fetcher, slug)
        if snap:
            results.append(snap)
            log.info(
                "[%s] mid=%.3f  bids=%d  asks=%d",
                slug, snap.midpoint, len(snap.bids), len(snap.asks),
            )
    return results
