"""Confusion detector — detect regime changes and auto-pause trading.

Monitors rolling statistics from recent market outcomes and flags when
trading should be suspended due to uncertainty.

The key insight: instead of monitoring external events (CPI, wars, news),
monitor the EFFECT of those events on smart money behavior. When smart
wallets get confused (tied votes, low signal rate, falling accuracy),
the market is in an uncertain regime and we should step aside.

Usage:
    detector = ConfusionDetector(window=20, pause_threshold=70.0)

    for each market:
        should_pause, reason = detector.should_pause()
        if should_pause:
            print(f"PAUSED: {reason}")
            continue  # skip this market

        # ... run voting logic, determine signal and outcome ...

        detector.record_market(MarketOutcome(
            timestamp=ts,
            had_signal=True,
            was_correct=True,
            signal_strength=9,
            was_tied=False,
            yes_votes=9,
            no_votes=3,
            btc_vol_estimate=0.002,
        ))
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass


@dataclass
class MarketOutcome:
    """Record of a single market's outcome for confusion tracking."""

    timestamp: int
    had_signal: bool
    was_correct: bool | None  # None if no signal
    signal_strength: int
    was_tied: bool
    yes_votes: int
    no_votes: int
    btc_vol_estimate: float = 0.0  # implied volatility of BTC during window


@dataclass
class ConfusionDiagnostics:
    """Breakdown of the confusion score for debugging."""

    total_score: float
    accuracy: float | None
    accuracy_score: float
    tied_rate: float
    tied_score: float
    signal_rate: float
    signal_rate_score: float
    btc_vol_ratio: float | None
    vol_score: float
    history_size: int


class ConfusionDetector:
    """Rolling-window confusion detector.

    Scores confusion on 0-100 scale:
      - 0-30: normal operation
      - 30-50: elevated uncertainty
      - 50-70: high uncertainty (approaching pause)
      - 70+: pause trading
    """

    def __init__(
        self,
        window: int = 20,
        pause_threshold: float = 70.0,
        pause_duration: int = 5,
        min_history_for_scoring: int = 5,
    ):
        self.window = window
        self.pause_threshold = pause_threshold
        self.pause_duration = pause_duration
        self.min_history = min_history_for_scoring

        self.history: deque[MarketOutcome] = deque(maxlen=window)
        self._current_market_idx: int = 0
        self._paused_until_idx: int = 0
        self._pause_reason: str = ""

    # ── Public API ──────────────────────────────────────────────────────

    def record_market(self, outcome: MarketOutcome) -> None:
        """Add a market outcome to the rolling history."""
        self.history.append(outcome)
        self._current_market_idx += 1

    def should_pause(self) -> tuple[bool, str]:
        """Check whether trading should be paused right now.

        Returns:
            (should_pause, reason_string)
        """
        # Active pause from previous confusion
        if self._current_market_idx < self._paused_until_idx:
            remaining = self._paused_until_idx - self._current_market_idx
            return True, f"{self._pause_reason} ({remaining} markets remaining)"

        # Compute fresh confusion
        diag = self.compute_confusion()
        if diag.total_score >= self.pause_threshold:
            self._paused_until_idx = self._current_market_idx + self.pause_duration
            reason = (
                f"CONFUSION score={diag.total_score:.0f} "
                f"(acc={diag.accuracy}, tied_rate={diag.tied_rate:.0%}, "
                f"signal_rate={diag.signal_rate:.0%}, vol_ratio={diag.btc_vol_ratio})"
            )
            self._pause_reason = reason
            return True, reason

        return False, ""

    def compute_confusion(self) -> ConfusionDiagnostics:
        """Compute current confusion score (0-100) with diagnostics."""
        if len(self.history) < self.min_history:
            return ConfusionDiagnostics(
                total_score=0.0,
                accuracy=None,
                accuracy_score=0.0,
                tied_rate=0.0,
                tied_score=0.0,
                signal_rate=0.0,
                signal_rate_score=0.0,
                btc_vol_ratio=None,
                vol_score=0.0,
                history_size=len(self.history),
            )

        # 1) Recent accuracy (last 10 decided markets)
        decided = [
            h for h in list(self.history)[-10:]
            if h.had_signal and h.was_correct is not None
        ]
        if len(decided) >= 3:
            accuracy = sum(1 for h in decided if h.was_correct) / len(decided)
            # Penalize accuracy below 50%, severely below 40%
            if accuracy < 0.40:
                accuracy_score = 100.0  # max penalty
            elif accuracy < 0.50:
                accuracy_score = (0.50 - accuracy) * 400  # 0-40 pts
            else:
                accuracy_score = 0.0
        else:
            accuracy = None
            accuracy_score = 0.0

        # 2) Tied vote rate
        tied_count = sum(1 for h in self.history if h.was_tied)
        tied_rate = tied_count / len(self.history)
        # Penalize if more than 40% of recent markets are tied
        if tied_rate > 0.40:
            tied_score = (tied_rate - 0.40) * 100 * 2  # 0-120 pts
        else:
            tied_score = 0.0

        # 3) Signal rate (how often we fire a signal)
        signal_count = sum(1 for h in self.history if h.had_signal)
        signal_rate = signal_count / len(self.history)
        # Penalize if signal rate drops below 30% (market too quiet/confused)
        if signal_rate < 0.30:
            signal_rate_score = (0.30 - signal_rate) * 100
        else:
            signal_rate_score = 0.0

        # 4) BTC volatility anomaly
        vols = [h.btc_vol_estimate for h in self.history if h.btc_vol_estimate > 0]
        if len(vols) >= 5:
            avg_vol = statistics.mean(vols)
            recent_vol = statistics.mean(vols[-3:])
            if avg_vol > 0:
                vol_ratio: float | None = recent_vol / avg_vol
                if vol_ratio is not None and vol_ratio > 2.0:
                    vol_score = (vol_ratio - 2.0) * 30
                else:
                    vol_score = 0.0
            else:
                vol_ratio = None
                vol_score = 0.0
        else:
            vol_ratio = None
            vol_score = 0.0

        # Weighted total (accuracy is most important).
        # Weights sum > 1.0 so any SINGLE extreme component can trigger pause,
        # and combinations amplify. Capped at 100.
        total = (
            accuracy_score * 0.80
            + tied_score * 0.60
            + signal_rate_score * 0.30
            + vol_score * 0.40
        )
        total = min(100.0, max(0.0, total))

        return ConfusionDiagnostics(
            total_score=round(total, 1),
            accuracy=round(accuracy, 3) if accuracy is not None else None,
            accuracy_score=round(accuracy_score, 1),
            tied_rate=round(tied_rate, 3),
            tied_score=round(tied_score, 1),
            signal_rate=round(signal_rate, 3),
            signal_rate_score=round(signal_rate_score, 1),
            btc_vol_ratio=round(vol_ratio, 2) if vol_ratio is not None else None,
            vol_score=round(vol_score, 1),
            history_size=len(self.history),
        )

    def reset(self) -> None:
        """Clear all state (for testing or fresh session)."""
        self.history.clear()
        self._current_market_idx = 0
        self._paused_until_idx = 0
        self._pause_reason = ""

    @property
    def is_paused(self) -> bool:
        return self._current_market_idx < self._paused_until_idx

    def status(self) -> str:
        """Human-readable status for logging."""
        diag = self.compute_confusion()
        if self.is_paused:
            remaining = self._paused_until_idx - self._current_market_idx
            return f"PAUSED ({remaining} remaining): {self._pause_reason}"
        if len(self.history) < self.min_history:
            return f"WARMING_UP ({len(self.history)}/{self.min_history})"
        return (
            f"OK  score={diag.total_score:.0f}  "
            f"acc={diag.accuracy}  tied={diag.tied_rate:.0%}  "
            f"signal={diag.signal_rate:.0%}"
        )
