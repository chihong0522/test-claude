from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TOP5_FILE = REPO_ROOT / "data" / "passive_tail_ladder_top5_portfolios.json"


def test_passive_tail_ladder_top5_portfolios_file_exists_and_has_expected_shape():
    assert TOP5_FILE.exists()

    payload = json.loads(TOP5_FILE.read_text())
    assert payload["initial_capital"] == 5000.0
    assert payload["market_count"] == 2016
    assert len(payload["top5_portfolios"]) == 5

    top1 = payload["top5_portfolios"][0]
    assert top1["rank"] == 1
    assert top1["risk_level"] == "low"
    assert top1["strategies"] == [
        "micro_1235_tp020_t20",
        "hybrid_5_8_10_13_tp015_t20",
        "merged_07_23_tp03_t20",
    ]
    assert top1["expected_final"] == 11758.8

    top5 = payload["top5_portfolios"][4]
    assert top5["rank"] == 5
    assert top5["risk_level"] == "high"
    assert top5["strategies"] == [
        "micro_1235_tp015_t20",
        "bounce_5810_tp012_t20",
        "band_13_25_tp03_t20",
    ]
