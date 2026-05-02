from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_mean_reversion_backtest_cli_help_runs_from_repo_root():
    result = subprocess.run(
        [sys.executable, "scripts/mean_reversion_backtest.py", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Search BTC 5m mean-reversion backtest configs" in result.stdout
