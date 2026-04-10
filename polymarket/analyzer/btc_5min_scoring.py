"""BTC 5-min specific scoring — computes per-wallet metrics and ranks them.

Unlike the general trader scoring, this focuses only on trades in the
`btc-updown-5m` market series. Metrics are computed from aggregated market
trades, not from per-wallet trade history.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime


def _outcome_index_from_trade(trade: dict) -> int:
    """Infer the outcome index from the trade dict."""
    idx = trade.get("outcomeIndex")
    if idx is not None:
        try:
            return int(idx)
        except (TypeError, ValueError):
            pass
    # Fallback: map from outcome string
    outcome = (trade.get("outcome") or "").strip().lower()
    if outcome in ("up", "yes"):
        return 0
    if outcome in ("down", "no"):
        return 1
    return 0


def compute_wallet_btc5m_metrics(
    wallet: str,
    wallet_trades_by_market: dict[str, list[dict]],
    market_info: dict[str, dict],
) -> dict:
    """Compute BTC 5-min specific metrics for one wallet.

    Args:
        wallet: The proxyWallet address.
        wallet_trades_by_market: {condition_id: [trades for this wallet]}
        market_info: {condition_id: {winning_index, resolved, ...}}

    Returns:
        Dict of metrics.
    """
    total_trades = 0
    total_volume = 0.0
    total_pnl = 0.0
    per_market_pnl: dict[str, float] = {}
    markets_participated: set[str] = set()
    markets_resolved: set[str] = set()
    first_ts: int | None = None
    last_ts: int | None = None
    position_sizes_usd: list[float] = []
    active_hours: set[int] = set()
    display_name: str | None = None

    for cid, trades in wallet_trades_by_market.items():
        info = market_info.get(cid, {})
        winning_index = info.get("winning_index")
        resolved = info.get("resolved", False)

        markets_participated.add(cid)
        if resolved:
            markets_resolved.add(cid)

        market_pnl = 0.0

        for t in trades:
            total_trades += 1
            size = float(t.get("size") or 0)
            price = float(t.get("price") or 0)
            side = (t.get("side") or "BUY").upper()
            ts = int(t.get("timestamp") or 0)
            trade_idx = _outcome_index_from_trade(t)

            trade_usdc = size * price
            total_volume += trade_usdc
            position_sizes_usd.append(trade_usdc)

            if ts > 0:
                if first_ts is None or ts < first_ts:
                    first_ts = ts
                if last_ts is None or ts > last_ts:
                    last_ts = ts
                active_hours.add(ts // 3600)

            if not display_name:
                display_name = t.get("name") or t.get("pseudonym")

            # P&L for THIS trade on the resolved market
            if resolved and winning_index is not None:
                is_winning_token = trade_idx == winning_index
                if side == "BUY":
                    # Paid `price` for a share; collects 1 if winner else 0
                    if is_winning_token:
                        market_pnl += size * (1.0 - price)
                    else:
                        market_pnl -= size * price
                elif side == "SELL":
                    # Received `price`; loses out on the 1 it would have been worth
                    if is_winning_token:
                        market_pnl -= size * (1.0 - price)
                    else:
                        market_pnl += size * price

        per_market_pnl[cid] = market_pnl
        total_pnl += market_pnl

    # Win rate computed at market-level (did they net profit on each market?)
    resolved_market_pnls = [per_market_pnl[cid] for cid in markets_resolved]
    wins = sum(1 for p in resolved_market_pnls if p > 0)
    losses = sum(1 for p in resolved_market_pnls if p < 0)
    decided = wins + losses
    win_rate = wins / decided if decided > 0 else 0.0

    avg_position = sum(position_sizes_usd) / len(position_sizes_usd) if position_sizes_usd else 0.0
    roi = (total_pnl / total_volume) if total_volume > 0 else 0.0

    now = int(datetime.utcnow().timestamp())
    hours_since_last = (now - last_ts) / 3600 if last_ts else 9999
    minutes_since_last = (now - last_ts) / 60 if last_ts else 9999

    return {
        "wallet": wallet,
        "name": display_name or wallet[:10],
        "btc5m_trades": total_trades,
        "btc5m_markets": len(markets_participated),
        "btc5m_markets_resolved": len(markets_resolved),
        "btc5m_volume": round(total_volume, 2),
        "btc5m_pnl": round(total_pnl, 2),
        "btc5m_roi": round(roi, 4),
        "btc5m_win_rate": round(win_rate, 4),
        "btc5m_wins": wins,
        "btc5m_losses": losses,
        "btc5m_avg_position": round(avg_position, 2),
        "btc5m_first_ts": first_ts,
        "btc5m_last_ts": last_ts,
        "btc5m_hours_since_last": round(hours_since_last, 1),
        "btc5m_minutes_since_last": round(minutes_since_last, 1),
        "btc5m_active_hours": len(active_hours),
        "per_market_pnl": per_market_pnl,
    }


def aggregate_by_wallet(
    market_trades: dict[str, list[dict]],
) -> dict[str, dict[str, list[dict]]]:
    """Transform {condition_id: [trades]} -> {wallet: {condition_id: [trades]}}."""
    by_wallet: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for cid, trades in market_trades.items():
        for t in trades:
            w = t.get("proxyWallet")
            if w:
                by_wallet[w][cid].append(t)
    # Convert inner defaultdicts to regular dicts
    return {w: dict(mkts) for w, mkts in by_wallet.items()}


def passes_btc5m_checklist(metrics: dict, max_hours_inactive: float = 24.0) -> bool:
    """Check if a wallet passes the BTC 5-min specific checklist."""
    if metrics["btc5m_trades"] < 20:
        return False
    if metrics["btc5m_markets"] < 10:
        return False
    if metrics["btc5m_pnl"] <= 0:
        return False
    if metrics["btc5m_win_rate"] < 0.55:
        return False
    if metrics["btc5m_roi"] < 0.01:
        return False
    if metrics["btc5m_hours_since_last"] > max_hours_inactive:
        return False
    if metrics["btc5m_avg_position"] > 10_000:
        # Whale — likely has market impact we can't replicate
        return False
    return True


def score_btc5m_wallet(metrics: dict) -> float:
    """Composite score 0-100 based on BTC 5-min metrics.

    Weights:
      - ROI: 30% (most important — profitable per $ traded)
      - Win rate: 25%
      - Trade count: 15%
      - Volume (log-scaled): 15%
      - Consistency (markets won / markets played): 15%
    """
    if metrics["btc5m_trades"] == 0:
        return 0.0

    # ROI — sigmoid centered at 2%
    roi = metrics["btc5m_roi"]
    roi_score = _sigmoid(roi, center=0.02, steepness=80)  # sensitive around 1-5%

    # Win rate — linear in [0.50, 0.70]
    wr = metrics["btc5m_win_rate"]
    wr_score = _linear_scale(wr, low=0.50, high=0.70)

    # Trade count bonus — log scale, capped
    tc = metrics["btc5m_trades"]
    tc_score = min(1.0, math.log(max(tc, 1)) / math.log(500))

    # Volume — log scale
    vol = metrics["btc5m_volume"]
    vol_score = min(1.0, math.log(max(vol, 1)) / math.log(1_000_000))  # cap at $1M

    # Consistency — % of resolved markets profitable
    consistency = wr  # same as win rate for this use case, reuse

    score = 0.0
    score += 30 * roi_score
    score += 25 * wr_score
    score += 15 * tc_score
    score += 15 * vol_score
    score += 15 * consistency

    # Penalties
    if metrics["btc5m_hours_since_last"] > 3:
        score -= 10  # Not actively trading
    if metrics["btc5m_avg_position"] > 5000:
        score -= 10  # Whale — harder to copy
    if metrics["btc5m_markets"] < 20:
        score -= 5  # Low diversity
    if metrics["btc5m_pnl"] < 0:
        score -= 20  # Net loser

    return max(0.0, min(100.0, round(score, 1)))


def _sigmoid(x: float, center: float = 0.0, steepness: float = 1.0) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-steepness * (x - center)))
    except OverflowError:
        return 0.0 if x < center else 1.0


def _linear_scale(x: float, low: float, high: float) -> float:
    if high <= low:
        return 0.0
    return max(0.0, min(1.0, (x - low) / (high - low)))
