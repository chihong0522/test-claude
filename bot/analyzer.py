"""Step 2 — Analyzer: merge YES/NO orderbooks, compute dual-ask profitability.

Core insight:
  Split 1 USDC → 1 YES + 1 NO
  If we can sell YES @ P_yes and NO @ P_no where P_yes + P_no > 1.0,
  then profit = P_yes + P_no - 1.0 (minus fees/gas).

The analyzer finds the best ask prices we can place on BOTH sides
that are likely to fill (near/inside the current book) while still
being profitable.
"""

import logging
from dataclasses import dataclass

from bot.config import Config
from bot.scanner import OrderbookSnapshot

log = logging.getLogger(__name__)


@dataclass
class DualAskOpportunity:
    """A profitable split + dual-ask opportunity."""
    slug: str
    yes_token: str
    no_token: str
    exchange_address: str

    # Our planned ask prices
    yes_ask_price: float
    no_ask_price: float

    # Sizes we can place
    yes_ask_size: float
    no_ask_size: float

    # Metrics
    combined_ask: float        # yes_ask + no_ask (must be > 1.0)
    gross_profit_per_share: float  # combined_ask - 1.0
    midpoint: float

    # LP reward eligibility
    yes_within_spread: bool
    no_within_spread: bool


def _best_bid(book: list[dict]) -> float:
    """Best (highest) bid price, or 0 if empty."""
    return book[0]["price"] if book else 0.0


def _total_bid_size(book: list[dict], min_price: float) -> float:
    """Sum of bid sizes at or above min_price."""
    return sum(b["size"] for b in book if b["price"] >= min_price)


def analyze_market(snap: OrderbookSnapshot, cfg: Config) -> DualAskOpportunity | None:
    """
    Analyze one market for dual-ask profitability.

    Strategy:
      - Place YES ask just above the best YES bid (so we're maker, not taker)
      - Place NO ask just above the best NO bid
      - Check combined > 1.0 for profitability
      - Check within LP spread limit for reward eligibility

    NOTE: The orderbook from Limitless returns the YES side book.
    The NO side is the mirror: NO_bid @ price P = YES_ask @ (1-P).
    So from a single orderbook we derive both sides.
    """
    yes_bids = snap.bids
    yes_asks = snap.asks

    # YES side: we want to SELL YES tokens → place an ask
    # Best strategy: price our ask slightly above best bid to be maker
    yes_best_bid = _best_bid(yes_bids)
    if yes_best_bid <= 0:
        log.debug("[%s] No YES bids, skip", snap.slug)
        return None

    # Our YES ask: at the best bid price (will sit on book as maker)
    # or 1 cent above to ensure we're providing liquidity
    yes_ask_price = yes_best_bid

    # NO side mirror: NO_best_bid = 1 - YES_best_ask
    # If YES best ask = 0.60, then NO best bid = 0.40
    yes_best_ask = yes_asks[0]["price"] if yes_asks else 1.0
    no_best_bid = round(1.0 - yes_best_ask, 4)
    if no_best_bid <= 0:
        log.debug("[%s] No NO bids (derived), skip", snap.slug)
        return None

    no_ask_price = no_best_bid

    # Combined ask check
    combined = yes_ask_price + no_ask_price
    gross_profit = combined - 1.0

    if combined < cfg.min_combined_ask:
        log.debug(
            "[%s] Combined %.4f < min %.4f, skip",
            snap.slug, combined, cfg.min_combined_ask,
        )
        return None

    # Size: limited by how much we want to split
    size = cfg.split_amount_usdc

    # LP reward eligibility: ask within spread_limit of midpoint
    mid = snap.midpoint
    yes_within = abs(yes_ask_price - mid) <= cfg.lp_spread_limit
    no_within = abs(no_ask_price - (1 - mid)) <= cfg.lp_spread_limit

    opp = DualAskOpportunity(
        slug=snap.slug,
        yes_token=snap.yes_token,
        no_token=snap.no_token,
        exchange_address=snap.exchange_address,
        yes_ask_price=yes_ask_price,
        no_ask_price=no_ask_price,
        yes_ask_size=size,
        no_ask_size=size,
        combined_ask=combined,
        gross_profit_per_share=gross_profit,
        midpoint=mid,
        yes_within_spread=yes_within,
        no_within_spread=no_within,
    )

    log.info(
        "[%s] OPPORTUNITY: YES_ask=%.3f  NO_ask=%.3f  combined=%.4f  "
        "profit/share=%.4f  LP_eligible=%s/%s",
        snap.slug,
        yes_ask_price, no_ask_price, combined,
        gross_profit,
        yes_within, no_within,
    )
    return opp


def analyze_all(
    snapshots: list[OrderbookSnapshot], cfg: Config
) -> list[DualAskOpportunity]:
    """Analyze all scanned markets. Returns profitable opportunities."""
    opps = []
    for snap in snapshots:
        opp = analyze_market(snap, cfg)
        if opp:
            opps.append(opp)
    return opps
