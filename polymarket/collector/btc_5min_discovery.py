"""Discover BTC 5-min markets and collect all trades per market.

These are high-frequency binary markets ('btc-updown-5m-<ts>') resolved via
Chainlink BTC/USD price feed. One market every 5 minutes, 288/day.

The slug timestamp is the start of the 5-minute resolution window. Markets
open ~24 hours before their resolution window.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any

from polymarket.clients.data_api import DataAPIClient
from polymarket.clients.gamma import GammaClient

logger = logging.getLogger(__name__)

BTC_5MIN_TICKER = "btc-updown-5m"
FIVE_MIN = 300  # seconds


def _parse_outcome_prices(raw: Any) -> list[float]:
    """Parse outcomePrices — API sometimes returns a JSON-encoded string."""
    if raw is None:
        return []
    if isinstance(raw, list):
        try:
            return [float(x) for x in raw]
        except (TypeError, ValueError):
            return []
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return [float(x) for x in parsed]
        except (json.JSONDecodeError, TypeError, ValueError):
            return []
    return []


def _extract_market_info(event: dict) -> dict | None:
    """Extract {condition_id, slug, end_date, winning_index, resolved, volume} from an event."""
    markets = event.get("markets", [])
    if not markets:
        return None
    market = markets[0]
    cid = market.get("conditionId")
    if not cid:
        return None

    raw_prices = market.get("outcomePrices")
    prices = _parse_outcome_prices(raw_prices)

    winning_index: int | None = None
    resolved = False
    if len(prices) >= 2 and all(p in (0.0, 1.0) for p in prices):
        resolved = True
        winning_index = 0 if prices[0] == 1.0 else 1

    return {
        "condition_id": cid,
        "slug": market.get("slug") or event.get("slug"),
        "title": event.get("title"),
        "end_date": event.get("endDate") or market.get("endDate"),
        "closed": market.get("closed", False) or event.get("closed", False),
        "resolved": resolved,
        "winning_index": winning_index,
        "outcome_prices": prices,
        "volume": float(market.get("volume") or event.get("volume") or 0),
    }


async def _fetch_event_by_slug(gamma: GammaClient, slug: str) -> dict | None:
    """Fetch a single event by exact slug."""
    try:
        data = await gamma.get("/events", {"slug": slug})
        if isinstance(data, list) and data:
            return data[0]
    except Exception as e:
        logger.debug("slug %s failed: %s", slug, e)
    return None


async def discover_btc_5min_markets(
    gamma: GammaClient,
    n_markets: int = 500,
    skip_unresolved: bool = True,
    concurrency: int = 20,
) -> list[dict]:
    """Discover the last N BTC 5-min markets by enumerating slugs.

    We generate `btc-updown-5m-<ts>` slugs for 5-minute windows going back in
    time, then fetch each one concurrently. The ticker filter on the API is
    unreliable (partial match), so slug-based enumeration is the correct path.

    Args:
        n_markets: target number of resolved markets to collect
        skip_unresolved: exclude markets that haven't resolved yet
        concurrency: max concurrent slug fetches
    """
    now_ts = int(time.time())
    # Resolution finalizes a few minutes after window end; start from 10 min ago
    latest_ts = (now_ts - 600) - ((now_ts - 600) % FIVE_MIN)

    # Generate more candidates than needed because some may not exist yet
    candidates_needed = int(n_markets * 1.3)
    timestamps = [latest_ts - i * FIVE_MIN for i in range(candidates_needed)]

    sem = asyncio.Semaphore(concurrency)
    markets: list[dict] = []
    seen_cids: set[str] = set()

    async def _fetch_one(ts: int):
        async with sem:
            slug = f"btc-updown-5m-{ts}"
            event = await _fetch_event_by_slug(gamma, slug)
            if not event:
                return None
            info = _extract_market_info(event)
            if not info:
                return None
            if skip_unresolved and not info["resolved"]:
                return None
            return info

    results = await asyncio.gather(*[_fetch_one(ts) for ts in timestamps])
    for info in results:
        if info and info["condition_id"] not in seen_cids:
            seen_cids.add(info["condition_id"])
            markets.append(info)
        if len(markets) >= n_markets:
            break

    # Sort newest to oldest by end_date
    markets.sort(key=lambda m: m.get("end_date") or "", reverse=True)
    logger.info("Discovered %d BTC 5-min markets (resolved=%s)", len(markets), skip_unresolved)
    return markets[:n_markets]


async def _fetch_all_trades_for_market(
    data_api: DataAPIClient,
    condition_id: str,
    page_size: int = 500,
    max_pages: int = 7,  # max offset ~3000 (API rejects offset > 3500)
) -> list[dict]:
    """Paginated trade fetch for one market.

    Polymarket's data API caps offset around 3500 per market, so we stop
    early to avoid HTTP 400s.
    """
    all_trades: list[dict] = []
    for page in range(max_pages):
        offset = page * page_size
        try:
            batch = await data_api.get_trades(market=condition_id, limit=page_size, offset=offset)
        except Exception:
            # Hit offset cap — return what we have
            break
        if not batch:
            break
        all_trades.extend(batch)
        if len(batch) < page_size:
            break
    return all_trades


async def collect_market_trades(
    data_api: DataAPIClient,
    markets: list[dict],
    concurrency: int = 10,
) -> dict[str, list[dict]]:
    """For each market, fetch all trades. Returns {condition_id: [trades, ...]}."""
    sem = asyncio.Semaphore(concurrency)
    result: dict[str, list[dict]] = {}

    async def _fetch_one(m: dict):
        async with sem:
            cid = m["condition_id"]
            try:
                trades = await _fetch_all_trades_for_market(data_api, cid)
                result[cid] = trades
            except Exception as e:
                logger.warning("Failed to fetch trades for %s: %s", cid[:10], e)
                result[cid] = []

    await asyncio.gather(*[_fetch_one(m) for m in markets])

    total_trades = sum(len(v) for v in result.values())
    logger.info("Collected %d trades across %d markets", total_trades, len(markets))
    return result
