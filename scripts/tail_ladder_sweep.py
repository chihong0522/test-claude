#!/usr/bin/env python3
from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path

ROOT = Path("/mnt/vol-1/test-claude")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket.backtester.tail_ladder import TailLadderConfig, backtest_tail_ladder
CACHE = Path("/tmp/btc5m_backtest_cache.pkl")
REPORT_DIR = ROOT / ".hermes-team-cheapbounce" / "reports"
JSON_OUT = REPORT_DIR / "tail_ladder_sweep_results.json"


def _summary_dict(summary, stake_per_level_usd: float) -> dict:
    capital_deployed = round(summary.trades_taken * stake_per_level_usd, 2)
    pnl_on_deployed_pct = round(summary.total_pnl / capital_deployed * 100, 2) if capital_deployed else 0.0
    return {
        "markets_evaluated": summary.markets_evaluated,
        "markets_with_fills": summary.markets_with_fills,
        "markets_with_both_sides_filled": summary.markets_with_both_sides_filled,
        "trades_taken": summary.trades_taken,
        "wins": summary.wins,
        "losses": summary.losses,
        "win_rate": summary.win_rate,
        "total_pnl": summary.total_pnl,
        "avg_pnl": summary.avg_pnl,
        "avg_entry_price": summary.avg_entry_price,
        "median_entry_price": summary.median_entry_price,
        "avg_entry_offset_sec": summary.avg_entry_offset_sec,
        "median_entry_offset_sec": summary.median_entry_offset_sec,
        "best_trade_pnl": summary.best_trade_pnl,
        "worst_trade_pnl": summary.worst_trade_pnl,
        "capital_deployed_usd": capital_deployed,
        "pnl_on_deployed_pct": pnl_on_deployed_pct,
    }


def _build_configs() -> list[TailLadderConfig]:
    ladders = {
        "micro_1235": (0.01, 0.02, 0.03, 0.05),
        "micro_235": (0.02, 0.03, 0.05),
        "tail_358": (0.03, 0.05, 0.08),
        "bounce_5810": (0.05, 0.08, 0.10),
        "bounce_8_10_13": (0.08, 0.10, 0.13),
        "hybrid_5_8_10_13": (0.05, 0.08, 0.10, 0.13),
    }
    targets = [0.10, 0.12, 0.15, 0.20, 0.25]
    timeouts = [20, 40, 60, 120]
    window_ends = [60, 90, 120]
    configs: list[TailLadderConfig] = []

    for ladder_name, entry_levels in ladders.items():
        for max_elapsed_sec in window_ends:
            configs.append(
                TailLadderConfig(
                    name=f"{ladder_name}__resolve__end{max_elapsed_sec}",
                    entry_levels=entry_levels,
                    target_price_abs=None,
                    timeout_sec=120,
                    max_elapsed_sec=max_elapsed_sec,
                    stake_per_level_usd=20.0,
                    exit_mode="resolve",
                )
            )
            for target in targets:
                for timeout in timeouts:
                    configs.append(
                        TailLadderConfig(
                            name=f"{ladder_name}__tp{target:.2f}__t{timeout}__end{max_elapsed_sec}",
                            entry_levels=entry_levels,
                            target_price_abs=target,
                            timeout_sec=timeout,
                            max_elapsed_sec=max_elapsed_sec,
                            stake_per_level_usd=20.0,
                            exit_mode="target_abs",
                        )
                    )
    return configs


def _rank_key(row: dict) -> tuple:
    test = row["test"]
    train = row["train"]
    qualifies = int(test["trades_taken"] >= 8 and test["total_pnl"] > 0)
    return (
        qualifies,
        test["total_pnl"],
        test["pnl_on_deployed_pct"],
        test["win_rate"],
        test["trades_taken"],
        train["total_pnl"],
        train["pnl_on_deployed_pct"],
        row["name"],
    )


def main() -> None:
    if not CACHE.exists():
        raise RuntimeError(f"Missing cache: {CACHE}")

    markets, trades_by_market = pickle.load(open(CACHE, "rb"))
    markets = sorted(markets, key=lambda market: market["end_date"])
    split = int(len(markets) * 0.7)
    train_markets = markets[:split]
    test_markets = markets[split:]

    results = []
    for config in _build_configs():
        train_summary = backtest_tail_ladder(train_markets, trades_by_market, config)
        test_summary = backtest_tail_ladder(test_markets, trades_by_market, config)
        results.append(
            {
                "name": config.name,
                "params": {
                    "entry_levels": list(config.entry_levels),
                    "target_price_abs": config.target_price_abs,
                    "timeout_sec": config.timeout_sec,
                    "min_elapsed_sec": config.min_elapsed_sec,
                    "max_elapsed_sec": config.max_elapsed_sec,
                    "stake_per_level_usd": config.stake_per_level_usd,
                    "fee_pct": config.fee_pct,
                    "exit_mode": config.exit_mode,
                },
                "train": _summary_dict(train_summary, config.stake_per_level_usd),
                "test": _summary_dict(test_summary, config.stake_per_level_usd),
                "train_sample": [trade.__dict__ for trade in train_summary.trade_log[:3]],
                "test_sample": [trade.__dict__ for trade in test_summary.trade_log[:3]],
            }
        )

    ranked = sorted(results, key=_rank_key, reverse=True)
    top_test_win_rate = sorted(
        results,
        key=lambda row: (
            row["test"]["trades_taken"] >= 8,
            row["test"]["win_rate"],
            row["test"]["total_pnl"],
            row["test"]["trades_taken"],
        ),
        reverse=True,
    )
    top_test_roi = sorted(
        results,
        key=lambda row: (
            row["test"]["trades_taken"] >= 8,
            row["test"]["pnl_on_deployed_pct"],
            row["test"]["total_pnl"],
            row["test"]["win_rate"],
        ),
        reverse=True,
    )
    qualified = [row for row in ranked if row["test"]["trades_taken"] >= 8 and row["test"]["total_pnl"] > 0]

    payload = {
        "strategy_family": "btc5m_tail_ladder_passive_touch",
        "cache": str(CACHE),
        "train_markets": len(train_markets),
        "test_markets": len(test_markets),
        "config_count": len(results),
        "ranking_rule": "test_total_pnl, then test_roi, then test_win_rate; test_trades>=8 preferred",
        "qualified_rule": {
            "min_test_trades": 8,
            "require_positive_test_pnl": True,
        },
        "ranked": ranked,
        "top_by_test_win_rate": top_test_win_rate[:10],
        "top_by_test_roi": top_test_roi[:10],
        "qualified": qualified,
    }

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    JSON_OUT.write_text(json.dumps(payload, indent=2))

    print(
        json.dumps(
            {
                "train_markets": len(train_markets),
                "test_markets": len(test_markets),
                "config_count": len(results),
                "qualified_count": len(qualified),
                "top5": [
                    {
                        "name": row["name"],
                        "params": row["params"],
                        "test": row["test"],
                    }
                    for row in ranked[:5]
                ],
                "json_out": str(JSON_OUT),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
