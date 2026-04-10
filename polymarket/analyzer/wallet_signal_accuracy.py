"""Signal-time accuracy analysis for smart wallet selection.

Unlike raw PnL (which can be driven by market making or success in unrelated
markets), signal-time accuracy measures: "when this wallet participates in a
bucket where the ensemble would fire a signal, how often does that signal
turn out to be correct?"

This directly addresses the failure mode found in agent analysis: a few
wallets with high lifetime PnL were dragging ensemble accuracy from ~60% down
to ~54% because they had money from other strategies but were terrible
predictors of BTC 5-min direction. Removing them (plus market makers with
near-zero per-trade edge) lifted accuracy +16 percentage points.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from math import erf, log, sqrt
from typing import Any


@dataclass
class WalletSignalMetrics:
    wallet: str
    signal_participations: int = 0  # buckets where wallet voted AND bucket fired a signal
    signal_wins: int = 0
    signal_time_accuracy: float = 0.0
    p_value: float = 1.0  # binomial test vs 50% null
    trade_count: int = 0
    pnl: float = 0.0
    edge_per_trade: float = 0.0  # |pnl| / (trade_count * 100)
    is_market_maker: bool = False
    is_blacklisted: bool = False
    blacklist_reason: str = ""
    oos_wins: int = 0
    oos_participations: int = 0
    oos_accuracy: float = 0.0


def _binomial_p_value(wins: int, n: int) -> float:
    """Two-sided binomial p-value vs 50% null hypothesis.

    Uses scipy if available, otherwise normal approximation which is close
    enough for n >= 30.
    """
    if n == 0:
        return 1.0
    try:
        from scipy.stats import binomtest  # type: ignore

        return float(binomtest(wins, n, p=0.5, alternative="two-sided").pvalue)
    except ImportError:
        mean = n * 0.5
        sd = sqrt(n * 0.25)
        if sd == 0:
            return 1.0
        z = abs(wins - mean) / sd
        return 2 * (1 - 0.5 * (1 + erf(z / sqrt(2))))


def _market_start_ts(m: dict) -> int:
    """Parse market start timestamp from end_date (5-min window)."""
    end_raw = m.get("end_date")
    if not end_raw:
        return 0
    try:
        end_ts = datetime.fromisoformat(end_raw.replace("Z", "+00:00")).timestamp()
        return int(end_ts - 300)
    except (ValueError, AttributeError):
        return 0


def compute_wallet_signal_metrics(
    markets: list[dict],
    trades_by_market: dict[str, list[dict]],
    candidate_wallets: set[str],
    min_distinct_wallets: int = 7,
    signal_dominance: float = 2.0,
    bucket_sec: int = 10,
    min_seconds_remaining: int = 0,
) -> dict[str, WalletSignalMetrics]:
    """Replay voting logic and compute per-wallet signal-time accuracy.

    For each resolved market:
      1. Accumulate per-wallet PnL and trade count (for MM detection).
      2. Bucket trades by 10s offset from market start.
      3. For each bucket, collect BUY votes from candidate wallets, split by
         outcomeIndex (YES=0 / NO=1).
      4. If a signal fires (distinct-wallet count >= min_distinct_wallets AND
         dominance satisfied), record which wallets were on the majority side.
      5. For each majority-side wallet: +1 participation, +1 win if the
         bucket's signal direction matched the market's winning outcome.

    The `min_seconds_remaining` filter mirrors the live bot's time gate:
    only buckets that fire early in the 5-min window count. Pass 0 to
    measure accuracy across all buckets.
    """
    metrics: dict[str, WalletSignalMetrics] = {}

    def _get(w: str) -> WalletSignalMetrics:
        m = metrics.get(w)
        if m is None:
            m = WalletSignalMetrics(wallet=w)
            metrics[w] = m
        return m

    for m in markets:
        if not m.get("resolved") or m.get("winning_index") is None:
            continue
        winning_idx = m["winning_index"]
        cid = m["condition_id"]
        trades = trades_by_market.get(cid, [])
        if not trades:
            continue

        start_ts = _market_start_ts(m)
        if start_ts == 0:
            continue
        end_ts = start_ts + 300

        # Accumulate PnL + trade count for every wallet (needed for MM detection).
        for t in trades:
            w = t.get("proxyWallet")
            if not w:
                continue
            entry = _get(w)
            entry.trade_count += 1

            size = float(t.get("size") or 0)
            price = float(t.get("price") or 0)
            side = (t.get("side") or "BUY").upper()
            outcome_idx = t.get("outcomeIndex") or 0
            is_winning = outcome_idx == winning_idx

            if side == "BUY":
                if is_winning:
                    entry.pnl += size * (1.0 - price)
                else:
                    entry.pnl -= size * price
            else:  # SELL
                if is_winning:
                    entry.pnl -= size * (1.0 - price)
                else:
                    entry.pnl += size * price

        # Bucket trades by offset for signal replay
        buckets: dict[int, list[dict]] = defaultdict(list)
        for t in trades:
            ts = int(t.get("timestamp") or 0)
            offset = ts - start_ts
            if 0 <= offset <= 300:
                buckets[int(offset // bucket_sec)].append(t)

        for bi, bucket in buckets.items():
            # Time gate: seconds remaining when bucket starts
            bucket_start_ts = start_ts + bi * bucket_sec
            if (end_ts - bucket_start_ts) < min_seconds_remaining:
                continue

            cand = [t for t in bucket if t.get("proxyWallet") in candidate_wallets]
            if not cand:
                continue

            yes_wallets: set[str] = set()
            no_wallets: set[str] = set()
            for t in cand:
                side = (t.get("side") or "BUY").upper()
                if side != "BUY":
                    continue
                w = t.get("proxyWallet")
                if not w:
                    continue
                oi = t.get("outcomeIndex") or 0
                if oi == 0:
                    yes_wallets.add(w)
                else:
                    no_wallets.add(w)

            signal_side: int | None = None
            signal_wallets: set[str] = set()
            if (
                len(yes_wallets) >= min_distinct_wallets
                and len(yes_wallets) >= signal_dominance * max(len(no_wallets), 1)
            ):
                signal_side = 0
                signal_wallets = yes_wallets
            elif (
                len(no_wallets) >= min_distinct_wallets
                and len(no_wallets) >= signal_dominance * max(len(yes_wallets), 1)
            ):
                signal_side = 1
                signal_wallets = no_wallets

            if signal_side is None:
                continue

            correct = signal_side == winning_idx
            for w in signal_wallets:
                entry = _get(w)
                entry.signal_participations += 1
                if correct:
                    entry.signal_wins += 1

    # Finalize derived fields
    for entry in metrics.values():
        if entry.signal_participations > 0:
            entry.signal_time_accuracy = entry.signal_wins / entry.signal_participations
            entry.p_value = _binomial_p_value(entry.signal_wins, entry.signal_participations)
        if entry.trade_count > 0:
            entry.edge_per_trade = abs(entry.pnl) / (entry.trade_count * 100)
            entry.is_market_maker = entry.trade_count >= 100 and entry.edge_per_trade < 0.01

    return metrics


def apply_blacklist_filters(
    metrics: dict[str, WalletSignalMetrics],
    min_participations_for_accuracy_filter: int = 100,
    max_bad_accuracy: float = 0.50,
) -> list[tuple[str, str]]:
    """Mark wallets blacklisted for: market-making, or bad signal-time accuracy.

    Mutates `metrics` in place. Returns a list of (wallet, reason) for logging.
    """
    dropped: list[tuple[str, str]] = []
    for w, m in metrics.items():
        if m.is_market_maker:
            m.is_blacklisted = True
            m.blacklist_reason = f"market_maker (edge=${m.edge_per_trade:.4f}/$100)"
            dropped.append((w, m.blacklist_reason))
            continue
        if (
            m.signal_participations >= min_participations_for_accuracy_filter
            and m.signal_time_accuracy < max_bad_accuracy
        ):
            m.is_blacklisted = True
            m.blacklist_reason = (
                f"bad_signal_accuracy "
                f"({m.signal_wins}/{m.signal_participations}={m.signal_time_accuracy:.1%})"
            )
            dropped.append((w, m.blacklist_reason))
    return dropped


def rank_wallets(
    metrics: dict[str, WalletSignalMetrics],
    min_participations: int = 30,
    min_accuracy: float = 0.52,
    max_p_value: float | None = None,
) -> list[WalletSignalMetrics]:
    """Return non-blacklisted wallets ranked by accuracy * log(participations+1).

    Applies:
      - not blacklisted
      - signal_participations >= min_participations
      - signal_time_accuracy >= min_accuracy (above chance)
      - optional statistical significance: p_value <= max_p_value
    """
    eligible = [
        m
        for m in metrics.values()
        if not m.is_blacklisted
        and m.signal_participations >= min_participations
        and m.signal_time_accuracy >= min_accuracy
        and (max_p_value is None or m.p_value <= max_p_value)
    ]
    eligible.sort(
        key=lambda m: m.signal_time_accuracy * log(m.signal_participations + 1),
        reverse=True,
    )
    return eligible


def validate_oos(
    candidates: list[WalletSignalMetrics],
    validate_markets: list[dict],
    trades_by_market: dict[str, list[dict]],
    min_distinct_wallets: int = 7,
    signal_dominance: float = 2.0,
    bucket_sec: int = 10,
    min_seconds_remaining: int = 180,
    min_oos_accuracy: float = 0.52,
    min_oos_participations: int = 5,
) -> list[WalletSignalMetrics]:
    """Re-run signal replay on the held-out validation set and drop wallets
    whose OOS accuracy collapses. Only uses the candidate pool for voting
    (i.e. the post-train-filter list).

    Mutates each candidate's oos_* fields and returns the surviving list.
    """
    if not candidates:
        return []
    candidate_set = {m.wallet for m in candidates}
    oos_metrics = compute_wallet_signal_metrics(
        validate_markets,
        trades_by_market,
        candidate_wallets=candidate_set,
        min_distinct_wallets=min_distinct_wallets,
        signal_dominance=signal_dominance,
        bucket_sec=bucket_sec,
        min_seconds_remaining=min_seconds_remaining,
    )
    survivors: list[WalletSignalMetrics] = []
    for c in candidates:
        om = oos_metrics.get(c.wallet)
        if om is None:
            # Wallet didn't trade in validation window — keep with 0 OOS data
            continue
        c.oos_wins = om.signal_wins
        c.oos_participations = om.signal_participations
        c.oos_accuracy = om.signal_time_accuracy
        if (
            c.oos_participations >= min_oos_participations
            and c.oos_accuracy < min_oos_accuracy
        ):
            continue
        survivors.append(c)
    return survivors
