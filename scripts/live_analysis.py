#!/usr/bin/env python3
"""
Live Top-10 Trader Analysis — Fetches real Polymarket data,
scores traders, backtests them, and displays results.
"""
import asyncio
import json
import logging
import sys
import time
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


async def main():
    from polymarket.clients.data_api import DataAPIClient
    from polymarket.clients.gamma import GammaClient
    from polymarket.analyzer.scoring import score_trader
    from polymarket.backtester.simulator import run_backtest, BacktestConfig

    data_client = DataAPIClient()
    gamma_client = GammaClient()

    try:
        # ══════════════════════════════════════════════════════════════════
        # Step 1: Discover top traders from leaderboard
        # ══════════════════════════════════════════════════════════════════
        print("\n" + "=" * 80)
        print("  POLYMARKET COPY TRADING RESEARCH — LIVE ANALYSIS")
        print("=" * 80)
        print(f"\n[{datetime.utcnow().strftime('%H:%M:%S')}] Step 1: Fetching leaderboard...")

        # Fetch top traders by all-time P&L
        leaderboard = await data_client.get_leaderboard(
            category="OVERALL", time_period="ALL", order_by="PNL", limit=50
        )
        print(f"  Found {len(leaderboard)} traders on all-time leaderboard")

        # Also fetch monthly top traders
        monthly = await data_client.get_leaderboard(
            category="OVERALL", time_period="MONTH", order_by="PNL", limit=50
        )
        print(f"  Found {len(monthly)} traders on monthly leaderboard")

        # Merge unique wallets
        wallets = {}
        for entry in leaderboard + monthly:
            w = entry.get("proxyWallet")
            if w and w not in wallets:
                wallets[w] = {
                    "wallet": w,
                    "name": entry.get("userName") or entry.get("pseudonym") or w[:10],
                    "pnl": float(entry.get("pnl", 0)),
                    "volume": float(entry.get("vol", 0)),
                    "profile_image": entry.get("profileImage"),
                }

        print(f"  {len(wallets)} unique wallets to analyze")

        # ══════════════════════════════════════════════════════════════════
        # Step 2: Fetch trades + positions for each trader
        # ══════════════════════════════════════════════════════════════════
        print(f"\n[{datetime.utcnow().strftime('%H:%M:%S')}] Step 2: Fetching trade data...")

        scored_traders = []
        total = len(wallets)
        sem = asyncio.Semaphore(3)  # 3 concurrent fetches

        async def analyze_trader(idx, wallet, info):
            async with sem:
                try:
                    # Fetch trades (up to 50 pages = 5000 trades)
                    trades = await data_client.get_all_trades(wallet, max_pages=50)
                    if len(trades) < 30:
                        return None

                    # Fetch positions
                    open_pos = await data_client.get_positions(wallet)
                    closed_pos = await data_client.get_closed_positions(wallet)

                    # Build market liquidity map from trades
                    market_liq = {}
                    unique_cids = set(t.get("conditionId", "") for t in trades if t.get("conditionId"))
                    for cid in list(unique_cids)[:10]:  # Check liquidity for up to 10 markets
                        try:
                            liq = await gamma_client.get_market_liquidity(cid)
                            if liq is not None:
                                market_liq[cid] = liq
                        except Exception:
                            pass

                    # Score the trader
                    result = score_trader(trades, open_pos, closed_pos, market_liq)
                    result["wallet"] = wallet
                    result["name"] = info["name"]
                    result["leaderboard_pnl"] = info["pnl"]
                    result["leaderboard_vol"] = info["volume"]
                    result["trades_fetched"] = len(trades)
                    result["open_positions"] = len(open_pos)
                    result["closed_positions"] = len(closed_pos)

                    status = "✓" if result["passes_checklist"] else "·"
                    print(f"  [{idx+1}/{total}] {status} {info['name'][:20]:20s} | "
                          f"Score: {result['composite_score']:5.1f} | "
                          f"Tier: {result['tier']} | "
                          f"ROI: {result['roi']*100:6.1f}% | "
                          f"WR: {result['win_rate']*100:5.1f}% | "
                          f"Trades: {len(trades)}")

                    return result

                except Exception as e:
                    logger.debug("Failed %s: %s", wallet[:10], e)
                    return None

        # Analyze all traders concurrently
        tasks = [
            analyze_trader(i, wallet, info)
            for i, (wallet, info) in enumerate(wallets.items())
        ]
        results = await asyncio.gather(*tasks)
        scored_traders = [r for r in results if r is not None]

        print(f"\n  Successfully scored {len(scored_traders)} traders")

        # ══════════════════════════════════════════════════════════════════
        # Step 3: Rank and display top-10
        # ══════════════════════════════════════════════════════════════════
        print(f"\n[{datetime.utcnow().strftime('%H:%M:%S')}] Step 3: Ranking top traders...")

        # Sort by composite score
        scored_traders.sort(key=lambda x: x["composite_score"], reverse=True)

        # Filter: must have traded within 3 days (active now)
        active_traders = [t for t in scored_traders if t.get("days_since_last_trade", 9999) <= 3]
        dormant_traders = [t for t in scored_traders if t.get("days_since_last_trade", 9999) > 3]
        print(f"  {len(active_traders)} traders active in last 3 days")
        print(f"  {len(dormant_traders)} traders dormant (skipped — can't copy future trades)")

        # Filter to passing checklist among active
        passing = [t for t in active_traders if t["passes_checklist"]]
        if len(passing) >= 5:
            top_10 = passing[:10]
            print(f"  {len(passing)} active traders pass full checklist")
        elif len(active_traders) >= 5:
            top_10 = active_traders[:10]
            print(f"  Only {len(passing)} pass full checklist — showing top 10 active by score")
        else:
            # Fall back to all scored if very few active
            top_10 = scored_traders[:10]
            print(f"  Few active traders — showing top 10 by score (check 'LastTx' column)")

        # ══════════════════════════════════════════════════════════════════
        # Step 4: Display top-10 results
        # ══════════════════════════════════════════════════════════════════
        print("\n" + "=" * 80)
        print("  TOP-10 TRADERS — RANKED BY COMPOSITE SCORE")
        print("=" * 80)

        print(f"\n{'#':>3} {'Tier':>4} {'Name':<22} {'Score':>6} {'ROI':>8} {'WR':>6} "
              f"{'PF':>6} {'Liq':>5} {'Trades':>7} {'LastTx':>7} {'Flags':>6}")
        print("-" * 98)

        for i, t in enumerate(top_10):
            flags = len(t.get("red_flags", []))
            flag_str = f"{flags}" if flags > 0 else "-"
            check = "✓" if t["passes_checklist"] else " "
            days_ago = t.get("days_since_last_trade", "?")
            last_tx = f"{days_ago}d" if isinstance(days_ago, int) else "?"

            print(f"{i+1:>3} [{t['tier']}] {check} {t['name'][:20]:<20s} "
                  f"{t['composite_score']:6.1f} "
                  f"{t['roi']*100:7.1f}% "
                  f"{t['win_rate']*100:5.1f}% "
                  f"{t['profit_factor']:5.2f} "
                  f"{t['liquidity_score']*100:4.0f}% "
                  f"{t['trade_count']:>6} "
                  f"{last_tx:>6} "
                  f"{flag_str:>5}")

        # ══════════════════════════════════════════════════════════════════
        # Step 5: Detailed breakdown for top-5
        # ══════════════════════════════════════════════════════════════════
        print("\n" + "=" * 80)
        print("  DETAILED ANALYSIS — TOP 5 TRADERS")
        print("=" * 80)

        for i, t in enumerate(top_10[:5]):
            print(f"\n--- #{i+1} {t['name']} [{t['tier']}] ---")
            print(f"  Wallet:           {t['wallet']}")
            print(f"  Composite Score:  {t['composite_score']:.1f}/100")
            print(f"  Leaderboard PnL:  ${t['leaderboard_pnl']:,.0f}")
            print(f"  Leaderboard Vol:  ${t['leaderboard_vol']:,.0f}")
            print(f"  ---")
            print(f"  ROI:              {t['roi']*100:.1f}%")
            print(f"  Win Rate:         {t['win_rate']*100:.1f}%")
            print(f"  Profit Factor:    {t['profit_factor']:.2f}")
            print(f"  Sharpe Ratio:     {t['sharpe_ratio']:.2f}")
            print(f"  Max Drawdown:     {t['max_drawdown']*100:.1f}%")
            print(f"  Recovery Factor:  {t['recovery_factor']:.2f}")
            print(f"  Consistency:      {t['consistency_score']:.2f}")
            print(f"  ---")
            print(f"  Trade Count:      {t['trade_count']}")
            print(f"  Active Days:      {t['active_days']}")
            print(f"  History Span:     {t['time_span_days']} days")
            print(f"  Last Trade:       {t.get('days_since_last_trade', '?')} days ago")
            print(f"  Unique Markets:   {t['unique_markets']}")
            print(f"  Liquidity Score:  {t['liquidity_score']*100:.0f}%")
            print(f"  Position Sizing:  {t['position_sizing_score']*100:.0f}%")
            print(f"  ---")
            print(f"  Red Flags:        {t['red_flags'] if t['red_flags'] else 'None'}")
            print(f"  Passes Checklist: {'YES' if t['passes_checklist'] else 'NO'}")

        # ══════════════════════════════════════════════════════════════════
        # Step 6: Run backtest for top-3
        # ══════════════════════════════════════════════════════════════════
        print("\n" + "=" * 80)
        print("  BACKTESTING — TOP 3 TRADERS (Copy-trade simulation)")
        print(f"  Config: $3,000 capital | 2% position | 30bps slippage | 0.2% fees")
        print("=" * 80)

        for i, t in enumerate(top_10[:3]):
            print(f"\n  Backtesting #{i+1} {t['name']}...")
            trades = await data_client.get_all_trades(t["wallet"], max_pages=20)

            config = BacktestConfig(
                initial_capital=3000.0,
                position_pct=0.02,
                slippage_bps=30,
            )
            bt = run_backtest(trades, config)

            color = "+" if bt.total_return >= 0 else ""
            print(f"  Result: {color}{bt.total_return:.2f}% return | "
                  f"${bt.initial_capital:.0f} -> ${bt.final_capital:.0f} | "
                  f"Max DD: {bt.max_drawdown:.1f}% | "
                  f"Sharpe: {bt.sharpe_ratio:.2f} | "
                  f"Win Rate: {bt.win_rate:.0f}% | "
                  f"{bt.total_trades_copied} trades copied | "
                  f"{bt.skipped_trades} skipped")

        # ══════════════════════════════════════════════════════════════════
        # Summary
        # ══════════════════════════════════════════════════════════════════
        print("\n" + "=" * 80)
        print("  SUMMARY — WALLETS READY FOR POLYCOP")
        print("=" * 80)
        print()
        for i, t in enumerate(top_10[:10]):
            check = "✓" if t["passes_checklist"] else "?"
            print(f"  {check} #{i+1} {t['wallet']}  — {t['name']} [Tier {t['tier']}, Score {t['composite_score']:.0f}]")

        print(f"\n  Analysis completed at {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print("=" * 80)

    finally:
        await data_client.close()
        await gamma_client.close()


if __name__ == "__main__":
    asyncio.run(main())
