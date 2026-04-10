# Agent Team Findings — Ensemble Voting Strategy Investigation

**Date:** 2026-04-10
**Trigger:** 1-hour paper trading showed −$137.59 (50% accuracy). User asked why earlier test looked better, and requested 10-cycle validation + parallel investigation via agent team.

## Live 10-Cycle Test Result

12 markets traded, 4 signals fired (33% signal rate):

| # | Signal | Entry | Votes | Outcome | Result | P&L |
|---|--------|:-----:|:-----:|:-------:|:------:|----:|
| 3 | NO | 0.32 | 1Y/8N | UP | LOSS ✗ | −$61.20 |
| 5 | YES | 0.27 | 8Y/4N | DOWN | LOSS ✗ | −$61.20 |
| 6 | YES | 0.85 | 7Y/0N | UP | WIN ✓ | +$9.39 |
| 9 | NO | 0.57 | 1Y/17N | DOWN | WIN ✓ | +$44.06 |
| Others (8) | — | — | — | — | FLAT | $0 |

**Total: 2W/2L, 50% accuracy, −$68.95 P&L**

## Why The First Test Looked Good (Root Cause)

The original `paper_trade.py` had a bug: entry prices used **historical bucket prices** from when the signal fired, not the current market price. Example:
- Smart wallets traded at $0.27 in bucket 4
- By the time we'd poll and act at bucket 18, current price was ~$0.55
- Old code "filled" us at $0.27 (inflated paper gain)
- New code uses live WebSocket price ($0.55) = realistic execution

**The "+$223" of the first test was ~3× inflated** by the stale-price bug.

## Agent A: Signal Loss Analysis (142 signals, 500 markets)

**Core finding: The strategy is fundamentally broken, not a latency/entry problem.**

### Quantitative evidence

| Metric | Value |
|--------|------:|
| Signal accuracy | **49.3%** (statistically = coin flip) |
| Mean drift t+10s after signal | +0.055 |
| Mean drift t+30s | +0.032 |
| **Mean final drift on losers** | **−0.324** |
| Mean final drift on winners | +0.36 |
| Smart wallets flip within 30s | **49% of losers** |
| Losers that eventually resolve in signal direction | **8%** |
| "Perfect co-entry" P&L at smart wallet avg fill price | −$0.031/$ |
| Current execution P&L | −$0.005/$ |
| Counter-trade accuracy | 50.7% |

### Pattern observed in 72 losing signals

```
Time:       t-10s    signal   t+10s    t+30s    final
Price:      0.480    0.480    0.511    0.512    0.040
                      ↑        ↑                  ↑
                      fires   pop     revert    reversal
```

**What's happening:** Smart wallets arrive as a **burst** and move the mid-price ~5 cents in their direction over 10-30s, then the price reverts and settles opposite ~50% of the time. The "signal" is detecting the smart wallets' own liquidity shove, not a prediction of the BTC resolution.

### Tests of our hypotheses

- **H1 ("Smart wallets chase local extremes"):** Weak. Only 14% of losers are strict local extremes.
- **H2 ("Smart wallets right, we enter late"):** **FALSE.** Executing at their avg fill price is ACTUALLY WORSE than ours.
- **H3 ("Counter-trade is free lunch"):** **FALSE.** 50.7% = mirror of the same noise; fees kill it.
- **H4 ("Signal marks liquidity extremes"):** **TRUE.** +0.06 mean pop collapses to -0.32 on losers.

### Agent A's recommendation

**Don't trade the signal direction. Trade the reversion of the pop.**

Enter opposite when: signal fires AND price has popped ≥0.04 in 10s. Exit at t+30s or mean-reversion target. This is a **scalp trade** on real microstructure, not a directional bet.

## Agent B: Counter-Trade Variant Design

Designed "Config CT" with strict filters:
- `min_signal_strength = 10` (ensure late wave)
- Trade OPPOSITE side
- **Extreme-price filter:** only fire when crowd's side ≥ 0.70 (paying premium)
- No flips — hold to resolution

**Issue:** Agent A's data showed raw CT is only 50.7% accurate. Agent B's filters might isolate better signals but:
- Sample becomes very thin
- Still based on the near-zero-alpha signal
- Holding to resolution defeats the microstructure thesis

**Verdict:** Worth a quick backtest but not a magic fix.

## Agent C: 1-Hour Market Variant (⭐ Highest Expected Value)

**Discovery:** Polymarket has real 1-hour BTC markets!

**Slug pattern:** `bitcoin-up-or-down-<month>-<day>-<year>-<hour>am|pm-et`
- Example: `bitcoin-up-or-down-april-9-2026-9am-et`
- NOT a unix-timestamp slug like 5-min markets

**Volume feasibility (4 actual samples tested):**

| Market | Volume | Trades | Smart Trades | 60s-buckets ≥7 votes |
|--------|-------:|-------:|-------------:|:-----:|
| Apr 9 9am ET | $179k | 2,916 | 112 | 6 |
| Apr 8 11am ET | $267k | 1,801 | 238 | 11 |
| Apr 8 9am ET | $220k | 3,243 | 271 | **18** |
| Apr 7 1pm ET | $199k | 1,701 | 208 | 9 |

With **60-second buckets** (6× bigger than 5m's 10s), each 1-hour market has 5-18 signal-eligible buckets. **Enough density for the strategy.**

### Why 1-hour markets should work better

| Factor | 5-min | 1-hour |
|--------|:-----:|:------:|
| Window length | 300s | 3,600s |
| Latency pressure | Critical | Relaxed |
| HFT dominance | High | Lower |
| Bucket granularity | 10s | 60s |
| Position hold time | ~150s | ~30min |
| Smart wallets have time to be right? | No | **Yes** |

### Implementation plan

1. **New file:** `polymarket/collector/btc_1h_discovery.py`
   - Enumerate by ET date/hour strings (not timestamps)
   - Use `zoneinfo("America/New_York")`
   - Parse window from `event.endDate - 3600s`

2. **New file:** `scripts/live_bot_1h.py` (clone + adapt live_bot_ws.py)
   - `bucket_sec`: 10 → 60
   - `max_offset`: 300 → 3600
   - `BASELINE_POLL_INTERVAL`: 1s → 5s
   - `BURST_THRESHOLD`: 15 → 30 (denser markets)
   - HTTP pagination: 5 → 10 pages
   - `min_remaining`: 60s → 600s
   - Derive start/end from `endDate`, not slug math

3. **Rebuild smart wallet pool** from 1-hour market history (not 5-min)

### Risk

We don't know if the smart-wallet signal is directional on 1-hour markets. It might still be liquidity noise from the same bots. **The only way to know is to backtest** on a month of 1-hour market data.

## Synthesized Recommendation

### Ranked next steps

| Rank | Approach | Rationale |
|:----:|----------|-----------|
| 🥇 | **Build 1-hour bot + backtest** | Real markets exist; latency irrelevant; longer windows may reveal real directional signal |
| 🥈 | Mean-reversion scalp (Agent A's idea) | Real +0.06 pop exists, but thin edge vs fees |
| 🥉 | Counter-trade backtest | Quick validation (~1 hour of work) |
| ❌ | Continue refining 5-min follow strategy | Dead — 49.3% on 142 trades is conclusive |

### What NOT to do

1. ❌ Don't add more filters to the 5-min follow strategy — it's noise
2. ❌ Don't assume latency is fixable by more optimization — Agent A showed "perfect co-entry" is WORSE
3. ❌ Don't trust small-sample paper trading results — always validate on 100+ trade samples
4. ❌ Don't deploy real money until we have a strategy with >55% accuracy on >200 trades

## Files referenced

- `scripts/live_bot_ws.py` — current (broken) strategy
- `scripts/ensemble_backtest.py` — backtest infrastructure
- `polymarket/collector/btc_5min_discovery.py` — needs 1h variant clone
- `data/smart_wallets_latest.json` — needs 1h rebuild
- `/tmp/btc5m_backtest_cache.pkl` — Agent A's analysis data
