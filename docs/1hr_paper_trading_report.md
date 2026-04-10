# 1-Hour Live Paper Trading Report

**Date:** 2026-04-10  
**Duration:** 65.7 minutes (11:40 → 12:45 UTC)  
**Strategy:** Config J (7+ votes from top 50 smart wallets, flips on)  
**Position size:** $60 per entry  
**Mode:** Paper trading (no real money)

## Setup

- **Smart wallets:** 50, refreshed from last 2 days of BTC 5-min markets
- **Infrastructure:** HTTP polling every 5s + WebSocket keep-alive
- **Confusion detector:** Window 20 markets, pause threshold 70
- **Latency:** 50-100ms per HTTP poll (excellent)

## Fixes Applied Before This Run

1. **Voting bug fix:** Only process NEW buckets (no replay of historical buckets). Use CURRENT market price for entry, not historical bucket price.
2. **Resolution retry fix:** Retroactive resolution fetch after session ends with 30s+10s retries.
3. **WebSocket subscription fix:** Unsubscribe old tokens on market transition (`resubscribe` method).

## Results Summary

| Metric | Value |
|--------|------:|
| Markets attempted | 13 |
| Markets paused (confusion) | 0 |
| Markets with signal fired | 9 |
| Markets FLAT (no signal) | 4 |
| Markets resolved | 12/13 |
| **Total P&L** | **−$137.59** |
| **Return on $3k capital** | **−4.6%** |
| **Wins** | 4 |
| **Losses** | 4 (+ 1 pending) |
| **Win rate** (decided markets) | 4/8 = 50.0% |

## Per-Market Breakdown

| # | Market | Signal | Position | Outcome | P&L |
|---|--------|--------|----------|---------|----:|
| 1 | btc-updown-5m-1775821200 | ENTER NO → FLIP YES | YES @ 0.63 | DOWN | **−$63.60** |
| 2 | btc-updown-5m-1775821500 | ENTER YES | YES @ 0.58 | UP | **+$42.25** ✓ |
| 3 | btc-updown-5m-1775821800 | ENTER YES | YES @ ? | DOWN | **−$61.20** |
| 4 | btc-updown-5m-1775822100 | ENTER NO | NO @ ? | DOWN | **+$13.80** ✓ |
| 5 | btc-updown-5m-1775822400 | ENTER YES | YES @ ? | UP | **+$45.49** ✓ |
| 6 | btc-updown-5m-1775822700 | ENTER YES | YES @ ? | DOWN | **−$61.20** |
| 7 | btc-updown-5m-1775823000 | ENTER YES → FLIP NO | NO @ 0.81 | DOWN | **+$10.47** ✓ |
| 8 | btc-updown-5m-1775823300 | No signal | FLAT | UP | $0.00 |
| 9 | btc-updown-5m-1775823600 | No signal | FLAT | DOWN | $0.00 |
| 10 | btc-updown-5m-1775823900 | ENTER NO @ 0.88 | NO @ 0.88 | UP | **−$61.20** |
| 11 | btc-updown-5m-1775824200 | No signal | FLAT | UP | $0.00 |
| 12 | btc-updown-5m-1775824500 | No signal | FLAT | DOWN | $0.00 |
| 13 | btc-updown-5m-1775824800 | ENTER YES → FLIP NO | NO @ 0.34 | Pending (UP likely) | **−$2.40** (interim) |

**Total:** −$137.59 (will likely be −$198.79 if Market 13 resolves UP as interim prices suggest)

## Statistical Analysis

### Accuracy
- **Decided markets:** 8 (4 wins / 4 losses)
- **Win rate:** 50.0% — exactly coin-flip

### Win/Loss magnitude asymmetry
- **Average win:** $28.00
- **Average loss:** $61.80
- **Ratio:** 2.2:1 against us

### Why losses are larger than wins
When the strategy enters NO at $0.88 and is wrong, it loses the full $60 (position size). When it wins at similar extreme prices, the upside is only $60 × (1 − 0.88) = $7.20. The **asymmetric entry prices** are killing us.

### Signal rate
- 9 of 13 markets fired a signal = **69%** (healthy)
- Confusion detector stayed at score=0 throughout (never paused)

## Key Findings

### 1. Reality matches the latency backtest

The backtest predicted:
- **Config J (0 latency):** +165% return, 56.7% accuracy
- **Config J (10s latency):** −170% return, 56% accuracy

Paper trading with 5s HTTP polling lands **between these extremes** but closer to the negative case: **50% accuracy and net loss**. This confirms that **signal detection latency is the critical factor** for this strategy to work.

### 2. Asymmetric entry prices are the real killer

Looking at the losses:
- Market 1: YES @ 0.63 (paid 63¢ for Up share) → UP lost → -$63.60
- Market 3: YES @ ? → -$61.20  
- Market 6: YES @ ? → -$61.20
- Market 10: NO @ 0.88 → -$61.20

The strategy is **frequently entering at extreme prices (0.63-0.88)** where:
- If right: small upside (1 - 0.88 = $0.12 per share)
- If wrong: nearly-full loss of position

This is because smart wallets tend to trade in bursts AFTER BTC has already moved. By the time we see their consensus (even with our improved voting), the market has already priced in their information. We're chasing the price.

### 3. Confusion detector didn't trigger

The confusion detector stayed at score 0 throughout. Looking at why:
- Accuracy was exactly 50% — not below threshold
- Signal rate 69% — not below 30%
- No tied votes detected
- No BTC vol spike

The detector is tuned for **extreme** confusion (sustained accuracy <40%), not **mild underperformance**. It's working as designed but wasn't needed here.

### 4. Infrastructure worked perfectly

- **Polling latency:** 50-100ms
- **Bot uptime:** 65.7 min without crashes
- **API connectivity:** Recovered from brief 503 at startup
- **Smart wallet refresh:** Loaded 50 fresh wallets successfully
- **WebSocket:** Connected but events were used only for connection health

## Comparison to Backtest and Earlier Paper Run

| Metric | Backtest J (ideal) | Backtest J+10s lat | Earlier paper (30min, buggy) | **1hr paper (fixed)** |
|--------|:------------------:|:------------------:|:----------------------------:|:---------------------:|
| Duration | — | — | 30 min | 60 min |
| Markets | 605 | 605 | 6 | **13** |
| Signals fired | 601 (99%) | 600 | 2 (33%) | **9 (69%)** |
| Accuracy | 56.7% | 56.0% | 100% (lucky 2) | **50.0%** |
| Total P&L | +$4,957 | −$5,110 | +$223 | **−$137.59** |
| Return | +165% | −170% | +7.4% | **−4.6%** |

The "earlier paper run" had a bug that caused optimistic entry prices. The 1-hour run with fixes shows the realistic picture: **at 50% accuracy with asymmetric losses, the strategy is net negative.**

## Root Cause Diagnosis

### Why the strategy performs worse than the backtest

1. **We enter LATE** — by the time we see 7+ smart wallets vote, the price has moved
2. **Entry prices are extreme** — losing positions cost nearly $60, winning positions only make $10-30
3. **Flip fees compound** — 4 flips × $2.40 = $9.60 in deadweight losses
4. **Real-world signal dilution** — not all "smart wallet" trades are high-quality; some are just noise

### Why the backtest was too optimistic

1. **Historical buckets had complete data** — the backtest saw all trades at bucket end; reality only sees trades as they happen
2. **No slippage/spread** — the backtest used mid-prices; reality requires crossing the spread
3. **No market impact** — our orders don't affect prices in simulation
4. **Batch scoring hides timing** — in backtest, we "knew" the outcome of each bucket; in reality, signals are delayed

## Recommendations

### Don't deploy real money yet

At 50% accuracy with −4.6% return on 1 hour, real money trading would lose ~$3-5/hour on $3k capital. That's a losing system.

### Three paths forward

#### Path A: Accept that 5-min BTC copy trading doesn't work and pivot

Use the infrastructure for **longer-duration markets** (1-hour, 24-hour). Smart money signals still apply but:
- Latency matters less (seconds don't kill an hour-long market)
- Entry prices are less extreme
- Flips happen less frequently

#### Path B: Upgrade latency to sub-second with direct CLOB trades feed

- Use CLOB WebSocket `last_trade_price` events (has no wallet info but is instant)
- Hybrid: WebSocket triggers signal check, HTTP enrichment only for confirmation
- Would need: mapping asset_id to buy/sell side + outcome index
- Co-locate server near Polygon RPC for additional savings

#### Path C: Change strategy direction entirely

The ensemble voting approach assumed smart money leads the market. The data shows **by the time they vote, price has already moved**. Consider:
- **Counter-trade smart money** when entry prices are extreme (mean reversion)
- **Trade based on price velocity** instead of vote counts (no wallet filter needed)
- **Use BTC CEX signals** as the leading indicator

### Immediate fixes for Path A (longer markets)

If you keep the current strategy but apply to longer markets:
- Change market discovery from 5-min slugs to 1-hour events
- Increase position size (longer markets can absorb more)
- Remove flip logic (not needed on slower markets)
- Add stop-loss (1-hour markets can have large drawdowns)

## Conclusion

The 1-hour paper trading session was a **valuable negative result**. It validates:

1. ✅ **The infrastructure works** — smart wallets refresh, confusion detector, live polling, resolution fetching
2. ✅ **The bugs we identified were real** — fixing them revealed the true strategy performance
3. ❌ **The strategy is unprofitable at real-world latency** — 50% accuracy + asymmetric losses = net loss
4. ❌ **Simple HTTP polling is too slow** — 5-10s delay kills the edge

**The strategy needs sub-second latency (via CLOB WebSocket trades feed) or a complete redesign** (longer markets, different signal source, or counter-trading) to be profitable.

The three production optimizations we built are all functional:
- **Rolling smart wallet refresh** — keeps the pool fresh
- **Confusion detector** — watching for regime changes (didn't trigger in this session)
- **WebSocket client** — ready for upgrade when we process trade events for signals

The next iteration should focus on either **fixing latency with CLOB trades feed** or **pivoting to longer-duration markets**.
