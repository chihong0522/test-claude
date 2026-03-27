"""Step 3 — Strategy: entry decision and position sizing.

Decides which opportunities to execute based on:
  - Profit threshold
  - Risk limits (max positions, max exposure)
  - LP reward eligibility bonus
"""

import logging
from dataclasses import dataclass

from bot.analyzer import DualAskOpportunity
from bot.config import Config

log = logging.getLogger(__name__)


@dataclass
class TradeDecision:
    """Approved trade to execute."""
    opportunity: DualAskOpportunity
    split_amount: float      # USDC to split
    priority_score: float    # Higher = better


def score_opportunity(opp: DualAskOpportunity, cfg: Config) -> float:
    """
    Score an opportunity. Higher = more attractive.

    Factors:
      1. Gross profit per share (primary)
      2. LP reward eligibility (bonus)
      3. Spread tightness (bonus for closer to midpoint)
    """
    score = opp.gross_profit_per_share * 100  # basis points

    # LP reward bonus: +10 if both sides eligible, +5 if one side
    if opp.yes_within_spread and opp.no_within_spread:
        score += 10
    elif opp.yes_within_spread or opp.no_within_spread:
        score += 5

    return score


def decide(
    opportunities: list[DualAskOpportunity],
    open_position_count: int,
    total_exposure: float,
    cfg: Config,
) -> list[TradeDecision]:
    """
    Filter and rank opportunities → return approved trades.

    Respects:
      - max_open_positions
      - max_exposure_usdc
    """
    available_slots = cfg.max_open_positions - open_position_count
    available_usd = cfg.max_exposure_usdc - total_exposure

    if available_slots <= 0:
        log.info("Max positions reached (%d), skipping", cfg.max_open_positions)
        return []
    if available_usd <= 0:
        log.info("Max exposure reached ($%.2f), skipping", cfg.max_exposure_usdc)
        return []

    scored = []
    for opp in opportunities:
        s = score_opportunity(opp, cfg)
        if s > 0:
            scored.append((s, opp))

    # Sort by score descending
    scored.sort(key=lambda x: x[0], reverse=True)

    decisions = []
    for score, opp in scored:
        if len(decisions) >= available_slots:
            break
        amount = min(cfg.split_amount_usdc, available_usd)
        if amount <= 0:
            break

        decisions.append(TradeDecision(
            opportunity=opp,
            split_amount=amount,
            priority_score=score,
        ))
        available_usd -= amount

        log.info(
            "[%s] APPROVED: score=%.1f  split=$%.2f",
            opp.slug, score, amount,
        )

    return decisions
