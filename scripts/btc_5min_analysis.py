#!/usr/bin/env python3
"""
BTC 5-Minute Up/Down — Live Copy Trade Research

Scans the last N BTC 5-min markets on Polymarket, aggregates trades by wallet,
computes BTC-5-min-specific metrics, and ranks the top worthwhile copy targets.
"""
import argparse
import asyncio
import logging
from datetime import datetime

from polymarket.analyzer.btc_5min_scoring import (
    aggregate_by_wallet,
    compute_wallet_btc5m_metrics,
    passes_btc5m_checklist,
    score_btc5m_wallet,
)
from polymarket.clients.data_api import DataAPIClient
from polymarket.clients.gamma import GammaClient
from polymarket.collector.btc_5min_discovery import (
    collect_market_trades,
    discover_btc_5min_markets,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _fmt_ts(ts: int | None) -> str:
    if not ts:
        return "never"
    return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _fmt_minutes_ago(mins: float) -> str:
    if mins < 1:
        return "<1m ago"
    if mins < 60:
        return f"{int(mins)}m ago"
    if mins < 1440:
        return f"{mins/60:.1f}h ago"
    return f"{mins/1440:.1f}d ago"


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--markets", type=int, default=500, help="Number of BTC 5-min markets to scan")
    parser.add_argument("--min-trades", type=int, default=20, help="Minimum BTC 5-min trades to consider a wallet")
    parser.add_argument("--max-hours-inactive", type=float, default=24.0,
                        help="Max hours since last trade to be considered active")
    parser.add_argument("--top", type=int, default=10, help="Number of traders to show")
    args = parser.parse_args()

    gamma_client = GammaClient()
    data_client = DataAPIClient()

    try:
        print("\n" + "=" * 90)
        print("  BTC 5-MINUTE UP/DOWN — COPY TRADE RESEARCH")
        print("=" * 90)

        # ── Step 1: Discover BTC 5-min markets ─────────────────────────────
        print(f"\n[{datetime.utcnow().strftime('%H:%M:%S')}] Step 1: Discovering BTC 5-min markets...")
        markets = await discover_btc_5min_markets(gamma_client, n_markets=args.markets)
        print(f"  Found {len(markets)} unique BTC 5-min markets")
        resolved = [m for m in markets if m["resolved"]]
        unresolved = [m for m in markets if not m["resolved"]]
        total_volume = sum(m.get("volume", 0) for m in markets)
        print(f"  Resolved: {len(resolved)} | Still open: {len(unresolved)}")
        print(f"  Total volume across scan window: ${total_volume:,.0f}")

        if resolved:
            earliest = min(m.get("end_date") or "" for m in resolved)
            latest = max(m.get("end_date") or "" for m in resolved)
            print(f"  Coverage: {earliest[:16]} -> {latest[:16]}")

        # ── Step 2: Fetch trades per market ────────────────────────────────
        print(f"\n[{datetime.utcnow().strftime('%H:%M:%S')}] Step 2: Fetching trades for each market...")
        market_trades = await collect_market_trades(data_client, markets, concurrency=10)
        total_trades = sum(len(v) for v in market_trades.values())
        print(f"  Total trades fetched: {total_trades:,}")

        # ── Step 3: Aggregate by wallet ────────────────────────────────────
        print(f"\n[{datetime.utcnow().strftime('%H:%M:%S')}] Step 3: Aggregating trades by wallet...")
        wallet_trades = aggregate_by_wallet(market_trades)
        print(f"  Unique wallets: {len(wallet_trades):,}")

        # ── Step 4: Compute metrics ────────────────────────────────────────
        print(f"\n[{datetime.utcnow().strftime('%H:%M:%S')}] Step 4: Computing BTC 5-min metrics...")
        market_info = {m["condition_id"]: m for m in markets}

        scored_wallets = []
        for wallet, trades_by_market in wallet_trades.items():
            metrics = compute_wallet_btc5m_metrics(wallet, trades_by_market, market_info)
            if metrics["btc5m_trades"] < args.min_trades:
                continue
            metrics["score"] = score_btc5m_wallet(metrics)
            metrics["passes_checklist"] = passes_btc5m_checklist(
                metrics, max_hours_inactive=args.max_hours_inactive
            )
            scored_wallets.append(metrics)

        print(f"  Wallets with >= {args.min_trades} BTC 5-min trades: {len(scored_wallets)}")

        # ── Step 5: Filter and rank ────────────────────────────────────────
        active = [w for w in scored_wallets if w["btc5m_hours_since_last"] <= args.max_hours_inactive]
        dormant = [w for w in scored_wallets if w["btc5m_hours_since_last"] > args.max_hours_inactive]
        passing = [w for w in active if w["passes_checklist"]]
        profitable = [w for w in active if w["btc5m_pnl"] > 0]

        print(f"  Active (last {args.max_hours_inactive}h): {len(active)}")
        print(f"  Active & profitable: {len(profitable)}")
        print(f"  Active & passing checklist: {len(passing)}")
        print(f"  Dormant (skipped): {len(dormant)}")

        # Sort: prefer passing checklist > profitable > by score
        passing.sort(key=lambda w: w["score"], reverse=True)
        profitable.sort(key=lambda w: w["score"], reverse=True)
        active.sort(key=lambda w: w["score"], reverse=True)

        if len(passing) >= args.top:
            top_n = passing[: args.top]
            group = "passing full checklist"
        elif len(profitable) >= args.top:
            top_n = profitable[: args.top]
            group = "active + profitable"
        else:
            top_n = active[: args.top]
            group = "active"

        # ── Step 6: Display top-N ──────────────────────────────────────────
        print("\n" + "=" * 90)
        print(f"  TOP-{args.top} BTC 5-MIN TRADERS ({group})")
        print("=" * 90)

        if not top_n:
            print("\n  No wallets met the minimum criteria.")
            return

        header = f"{'#':>3} {'Name':<22} {'Score':>6} {'Trades':>7} {'Mkts':>5} {'PnL':>10} {'ROI':>7} {'WR':>6} {'AvgPos':>8} {'LastTx':>9}"
        print("\n" + header)
        print("-" * len(header))
        for i, w in enumerate(top_n):
            check = "✓" if w["passes_checklist"] else " "
            pnl_color = "+" if w["btc5m_pnl"] > 0 else ""
            name = (w["name"] or w["wallet"][:10])[:20]
            print(
                f"{i+1:>3} {check}{name:<21s} "
                f"{w['score']:>6.1f} "
                f"{w['btc5m_trades']:>7} "
                f"{w['btc5m_markets']:>5} "
                f"{pnl_color}{w['btc5m_pnl']:>8.2f}  "
                f"{w['btc5m_roi']*100:>5.2f}% "
                f"{w['btc5m_win_rate']*100:>5.1f}% "
                f"${w['btc5m_avg_position']:>6.0f} "
                f"{_fmt_minutes_ago(w['btc5m_minutes_since_last']):>9}"
            )

        # ── Step 7: Detailed analysis of top 5 ─────────────────────────────
        print("\n" + "=" * 90)
        print("  DETAILED ANALYSIS — TOP 5")
        print("=" * 90)
        for i, w in enumerate(top_n[:5]):
            print(f"\n--- #{i+1} {w['name']} ---")
            print(f"  Wallet:              {w['wallet']}")
            print(f"  Composite Score:     {w['score']}/100")
            print(f"  Passes Checklist:    {'YES' if w['passes_checklist'] else 'NO'}")
            print(f"  ---")
            print(f"  BTC 5-min Trades:    {w['btc5m_trades']}")
            print(f"  Unique Markets:      {w['btc5m_markets']} ({w['btc5m_markets_resolved']} resolved)")
            print(f"  Net P&L:             ${w['btc5m_pnl']:+,.2f}")
            print(f"  Total Volume:        ${w['btc5m_volume']:,.2f}")
            print(f"  ROI:                 {w['btc5m_roi']*100:+.2f}%")
            print(f"  Win Rate:            {w['btc5m_win_rate']*100:.1f}% ({w['btc5m_wins']} wins / {w['btc5m_losses']} losses)")
            print(f"  Avg Position Size:   ${w['btc5m_avg_position']:,.2f}")
            print(f"  Active Hours:        {w['btc5m_active_hours']}")
            print(f"  First Trade:         {_fmt_ts(w['btc5m_first_ts'])} UTC")
            print(f"  Last Trade:          {_fmt_ts(w['btc5m_last_ts'])} UTC ({_fmt_minutes_ago(w['btc5m_minutes_since_last'])})")

        # ── Step 8: Summary for Polycop ────────────────────────────────────
        print("\n" + "=" * 90)
        print("  SUMMARY — BTC 5-MIN WALLETS FOR POLYCOP")
        print("=" * 90)
        print()
        for i, w in enumerate(top_n):
            check = "✓" if w["passes_checklist"] else "?"
            pnl_str = f"{w['btc5m_pnl']:+,.0f}"
            print(
                f"  {check} #{i+1} {w['wallet']}  "
                f"Score {w['score']:.0f} | "
                f"${pnl_str} over {w['btc5m_trades']} trades | "
                f"WR {w['btc5m_win_rate']*100:.0f}% | "
                f"{_fmt_minutes_ago(w['btc5m_minutes_since_last'])}"
            )

        print(f"\n  Analysis completed at {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print("=" * 90)

    finally:
        await gamma_client.close()
        await data_client.close()


if __name__ == "__main__":
    asyncio.run(main())
