#!/usr/bin/env python3
"""Search 5-minute BTC smart-wallet mean-reversion configs on cached historical data."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from polymarket.backtester.mean_reversion import (
    MeanReversionConfig,
    search_mean_reversion_configs,
)
from scripts.ensemble_backtest import load_data

SMART_WALLETS_FILE = REPO_ROOT / "data" / "smart_wallets_latest.json"


def _parse_csv_ints(raw: str) -> list[int]:
    return [int(part.strip()) for part in raw.split(",") if part.strip()]


def _parse_csv_floats(raw: str) -> list[float]:
    return [float(part.strip()) for part in raw.split(",") if part.strip()]


def _parse_optional_float_csv(raw: str) -> list[float | None]:
    values: list[float | None] = []
    for part in raw.split(","):
        token = part.strip().lower()
        if not token:
            continue
        values.append(None if token in {"none", "null"} else float(token))
    return values


def _build_wallet_sets(pool_data: dict, top_n_values: list[int]) -> dict[str, set[str]]:
    wallets = [row["wallet"] for row in pool_data.get("wallets", []) if row.get("wallet")]
    wallet_sets: dict[str, set[str]] = {}
    for top_n in top_n_values:
        clipped = wallets[:top_n]
        if not clipped:
            continue
        wallet_sets[f"top{len(clipped)}"] = set(clipped)
    return wallet_sets


async def main() -> None:
    parser = argparse.ArgumentParser(description="Search BTC 5m mean-reversion backtest configs")
    parser.add_argument("--markets", type=int, default=288, help="Requested market count for cache/API load")
    parser.add_argument("--force-refresh", action="store_true", help="Ignore /tmp cache and fetch fresh data")
    parser.add_argument("--top-n-values", default="10,20,42")
    parser.add_argument("--bucket-sec-values", default="10")
    parser.add_argument("--lookback-sec-values", default="10,20,30")
    parser.add_argument("--min-signal-strength-values", default="3,4")
    parser.add_argument("--dominance-values", default="2,3")
    parser.add_argument("--pop-threshold-values", default="0.04,0.05,0.06,0.07,0.08")
    parser.add_argument("--hold-sec-values", default="30,40,50")
    parser.add_argument("--latency-sec-values", default="0,10")
    parser.add_argument("--entry-cap-values", default="0.25,0.30,0.35,none")
    parser.add_argument("--position-size", type=float, default=60.0)
    parser.add_argument("--fee-pct", type=float, default=0.02)
    parser.add_argument("--min-trades", type=int, default=12)
    parser.add_argument("--min-win-rate", type=float, default=60.0)
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()

    if not SMART_WALLETS_FILE.exists():
        raise RuntimeError(f"Missing smart-wallet pool: {SMART_WALLETS_FILE}")

    print("[progress] Loading cached/fresh BTC 5m data...")
    markets, trades_by_market = await load_data(n_markets=args.markets, force_refresh=args.force_refresh)
    if args.markets > 0:
        markets = markets[: args.markets]
    market_ids = {market["condition_id"] for market in markets}
    trades_by_market = {cid: trades_by_market.get(cid, []) for cid in market_ids}
    print(f"[progress] Loaded {len(markets)} resolved markets with {sum(len(v) for v in trades_by_market.values()):,} trades")

    pool_data = json.loads(SMART_WALLETS_FILE.read_text())
    wallet_sets = _build_wallet_sets(pool_data, _parse_csv_ints(args.top_n_values))
    print(f"[progress] Built wallet universes: {', '.join(wallet_sets) if wallet_sets else 'none'}")

    configs: list[MeanReversionConfig] = []
    for bucket_sec in _parse_csv_ints(args.bucket_sec_values):
        for lookback_sec in _parse_csv_ints(args.lookback_sec_values):
            for min_strength in _parse_csv_ints(args.min_signal_strength_values):
                for dominance in _parse_csv_floats(args.dominance_values):
                    for pop_threshold in _parse_csv_floats(args.pop_threshold_values):
                        for hold_sec in _parse_csv_ints(args.hold_sec_values):
                            for latency_sec in _parse_csv_ints(args.latency_sec_values):
                                for entry_cap in _parse_optional_float_csv(args.entry_cap_values):
                                    cap_label = "none" if entry_cap is None else f"{entry_cap:.2f}"
                                    configs.append(
                                        MeanReversionConfig(
                                            name=(
                                                f"b{bucket_sec}-lb{lookback_sec}-ms{min_strength}-dom{dominance:g}-"
                                                f"pop{pop_threshold:.2f}-hold{hold_sec}-lat{latency_sec}-cap{cap_label}"
                                            ),
                                            bucket_sec=bucket_sec,
                                            lookback_sec=lookback_sec,
                                            min_signal_strength=min_strength,
                                            signal_dominance=dominance,
                                            pop_threshold=pop_threshold,
                                            hold_sec=hold_sec,
                                            latency_sec=latency_sec,
                                            entry_price_cap=entry_cap,
                                            position_size_usd=args.position_size,
                                            fee_pct=args.fee_pct,
                                        )
                                    )

    print(f"[progress] Searching {len(configs)} configs across {len(wallet_sets)} wallet universes...")
    ranked = search_mean_reversion_configs(
        markets,
        trades_by_market,
        configs=configs,
        wallet_sets=wallet_sets,
    )

    filtered = [
        result for result in ranked
        if result.trades_taken >= args.min_trades and result.win_rate >= args.min_win_rate
    ]
    top_results = filtered[: args.top_k]

    print("=" * 110)
    print("MEAN-REVERSION SEARCH RESULTS")
    print("=" * 110)
    print(f"markets={len(markets)}  candidate_results={len(filtered)}  requested_top_k={args.top_k}")
    print()
    print(
        "wallet_set | config | trades | wins | losses | win_rate% | total_pnl | avg_pnl | best | worst"
    )
    print("-" * 110)
    for result in top_results:
        print(
            f"{result.wallet_set_name:8} | {result.config.name:45.45} | {result.trades_taken:6d} | "
            f"{result.wins:4d} | {result.losses:6d} | {result.win_rate:9.2f} | "
            f"{result.total_pnl:9.2f} | {result.avg_pnl:7.2f} | {result.best_trade_pnl:5.2f} | {result.worst_trade_pnl:6.2f}"
        )

    if not top_results:
        print("No configs met the requested min-trades / min-win-rate filter.")

    if args.json_out:
        payload = [
            {
                "wallet_set": result.wallet_set_name,
                "config_name": result.config.name,
                "config": {
                    "bucket_sec": result.config.bucket_sec,
                    "lookback_sec": result.config.lookback_sec,
                    "min_signal_strength": result.config.min_signal_strength,
                    "signal_dominance": result.config.signal_dominance,
                    "pop_threshold": result.config.pop_threshold,
                    "hold_sec": result.config.hold_sec,
                    "latency_sec": result.config.latency_sec,
                    "entry_price_cap": result.config.entry_price_cap,
                    "position_size_usd": result.config.position_size_usd,
                    "fee_pct": result.config.fee_pct,
                },
                "markets_evaluated": result.markets_evaluated,
                "trades_taken": result.trades_taken,
                "wins": result.wins,
                "losses": result.losses,
                "win_rate": result.win_rate,
                "total_pnl": result.total_pnl,
                "avg_pnl": result.avg_pnl,
                "best_trade_pnl": result.best_trade_pnl,
                "worst_trade_pnl": result.worst_trade_pnl,
            }
            for result in top_results
        ]
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(payload, indent=2))
        print(f"\n[progress] Wrote JSON results to {args.json_out}")


if __name__ == "__main__":
    asyncio.run(main())
