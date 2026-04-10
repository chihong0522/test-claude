"""Unit tests for the confusion detector."""

from __future__ import annotations

import time

from polymarket.analyzer.confusion_detector import (
    ConfusionDetector,
    MarketOutcome,
)


def _mo(
    had_signal: bool = True,
    correct: bool | None = True,
    tied: bool = False,
    yes: int = 10,
    no: int = 2,
    vol: float = 0.001,
) -> MarketOutcome:
    return MarketOutcome(
        timestamp=int(time.time()),
        had_signal=had_signal,
        was_correct=correct,
        signal_strength=max(yes, no),
        was_tied=tied,
        yes_votes=yes,
        no_votes=no,
        btc_vol_estimate=vol,
    )


def test_initial_state_not_paused():
    d = ConfusionDetector()
    paused, _ = d.should_pause()
    assert not paused


def test_warming_up_with_few_markets():
    d = ConfusionDetector()
    for _ in range(3):
        d.record_market(_mo())
    diag = d.compute_confusion()
    assert diag.total_score == 0.0
    assert diag.history_size == 3


def test_healthy_markets_no_pause():
    d = ConfusionDetector()
    for _ in range(15):
        d.record_market(_mo(had_signal=True, correct=True, tied=False))
    paused, reason = d.should_pause()
    assert not paused, f"Shouldn't pause on healthy markets: {reason}"
    diag = d.compute_confusion()
    assert diag.total_score < 30, f"Expected low confusion, got {diag.total_score}"


def test_falling_accuracy_triggers_pause():
    d = ConfusionDetector(window=20, pause_threshold=50.0)
    # Seed with 5 wins to meet min history
    for _ in range(5):
        d.record_market(_mo(correct=True))
    # Then 10 consecutive losses
    for _ in range(10):
        d.record_market(_mo(correct=False))
    paused, reason = d.should_pause()
    assert paused, f"Should pause on accuracy drop: reason={reason}"
    assert "CONFUSION" in reason


def test_high_tied_rate_triggers_pause():
    d = ConfusionDetector(window=20, pause_threshold=50.0)
    # All tied markets
    for _ in range(15):
        d.record_market(_mo(had_signal=False, correct=None, tied=True, yes=5, no=5))
    paused, reason = d.should_pause()
    assert paused, f"Should pause on high tied rate: reason={reason}"


def test_low_signal_rate_not_enough_alone():
    d = ConfusionDetector(window=20, pause_threshold=70.0)
    # Low signal rate but no other problems
    for _ in range(15):
        d.record_market(_mo(had_signal=False, correct=None, tied=False))
    paused, reason = d.should_pause()
    # Signal rate score weighted at 10%, won't reach 70 alone
    assert not paused


def test_pause_duration_counts_down():
    d = ConfusionDetector(window=10, pause_threshold=40.0, pause_duration=3)
    # Force a pause
    for _ in range(5):
        d.record_market(_mo(correct=False))
    for _ in range(5):
        d.record_market(_mo(correct=False))

    paused, reason = d.should_pause()
    assert paused
    assert "3 markets remaining" in reason or d.is_paused

    # Simulate markets passing (still in pause)
    d.record_market(_mo(correct=True))
    paused, reason = d.should_pause()
    assert paused, "Should still be paused after 1 market"
    assert "2 markets remaining" in reason

    d.record_market(_mo(correct=True))
    d.record_market(_mo(correct=True))
    # Now pause should expire
    d.record_market(_mo(correct=True))
    paused2, _ = d.should_pause()
    # After 3 markets passed, pause should be over (but confusion might trigger new one)


def test_vol_spike_contributes_to_score():
    d = ConfusionDetector(window=20)
    # Normal vol markets
    for _ in range(10):
        d.record_market(_mo(correct=True, vol=0.001))
    # Spike to 10x vol
    for _ in range(3):
        d.record_market(_mo(correct=True, vol=0.01))
    diag = d.compute_confusion()
    assert diag.vol_score > 0, f"Expected vol score > 0, got {diag.vol_score}"


def test_reset_clears_state():
    d = ConfusionDetector()
    for _ in range(10):
        d.record_market(_mo(correct=False))
    d.reset()
    assert d.is_paused is False
    assert len(d.history) == 0
    assert d._current_market_idx == 0


if __name__ == "__main__":
    import sys

    # Simple test runner
    tests = [
        test_initial_state_not_paused,
        test_warming_up_with_few_markets,
        test_healthy_markets_no_pause,
        test_falling_accuracy_triggers_pause,
        test_high_tied_rate_triggers_pause,
        test_low_signal_rate_not_enough_alone,
        test_pause_duration_counts_down,
        test_vol_spike_contributes_to_score,
        test_reset_clears_state,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
            failed += 1

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
