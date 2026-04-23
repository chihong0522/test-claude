"""Risk management for live trading.

Kill-switch, balance checks, and position limits. All checks return
(ok: bool, reason: str) so the caller can log why a trade was blocked.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

KILL_SWITCH_FILE = Path(__file__).resolve().parent.parent.parent / "KILL"


@dataclass
class RiskLimits:
    max_daily_loss_usd: float = 20.0
    max_session_loss_usd: float = 50.0
    max_trades_per_hour: int = 6
    min_balance_usd: float = 5.0
    max_position_count: int = 1


class RiskManager:
    """Pre-trade risk gate. All checks must pass before order submission."""

    def __init__(self, limits: RiskLimits | None = None):
        self.limits = limits or RiskLimits()
        self._trades_this_hour: list[float] = []

    def check_kill_switch(self) -> tuple[bool, str]:
        """Check for the manual kill-switch file (touch KILL to halt)."""
        if KILL_SWITCH_FILE.exists():
            return False, f"KILL file exists at {KILL_SWITCH_FILE}"
        env_kill = os.getenv("POLYMARKET_KILL", "").lower()
        if env_kill in ("1", "true", "yes"):
            return False, "POLYMARKET_KILL env var is set"
        return True, "ok"

    def check_daily_loss(self, daily_pnl: float) -> tuple[bool, str]:
        """Block trading if daily loss exceeds limit."""
        if daily_pnl <= -self.limits.max_daily_loss_usd:
            return False, f"daily loss ${daily_pnl:.2f} exceeds -${self.limits.max_daily_loss_usd:.2f}"
        return True, "ok"

    def check_session_loss(self, session_pnl: float) -> tuple[bool, str]:
        """Block trading if session loss exceeds limit."""
        if session_pnl <= -self.limits.max_session_loss_usd:
            return False, f"session loss ${session_pnl:.2f} exceeds -${self.limits.max_session_loss_usd:.2f}"
        return True, "ok"

    def check_balance(self, balance_usd: float, stake_usd: float) -> tuple[bool, str]:
        """Verify sufficient USDC balance for the proposed trade."""
        if balance_usd < self.limits.min_balance_usd:
            return False, f"balance ${balance_usd:.2f} below minimum ${self.limits.min_balance_usd:.2f}"
        if balance_usd < stake_usd * 1.05:  # 5% margin for fees
            return False, f"balance ${balance_usd:.2f} insufficient for ${stake_usd:.2f} stake + fees"
        return True, "ok"

    def check_position_count(self, open_count: int) -> tuple[bool, str]:
        """Limit concurrent open positions."""
        if open_count >= self.limits.max_position_count:
            return False, f"{open_count} positions open, max is {self.limits.max_position_count}"
        return True, "ok"

    def check_trade_rate(self) -> tuple[bool, str]:
        """Rate-limit trades per hour to catch runaway behavior."""
        import time
        now = time.time()
        cutoff = now - 3600
        self._trades_this_hour = [t for t in self._trades_this_hour if t > cutoff]
        if len(self._trades_this_hour) >= self.limits.max_trades_per_hour:
            return False, f"{len(self._trades_this_hour)} trades in last hour, max is {self.limits.max_trades_per_hour}"
        return True, "ok"

    def record_trade(self) -> None:
        """Call after each successful trade to update rate tracking."""
        import time
        self._trades_this_hour.append(time.time())

    def pre_trade_check(
        self,
        daily_pnl: float,
        session_pnl: float,
        balance_usd: float,
        stake_usd: float,
        open_positions: int,
    ) -> tuple[bool, str]:
        """Run ALL risk checks. Returns (ok, first_failing_reason)."""
        checks = [
            self.check_kill_switch(),
            self.check_daily_loss(daily_pnl),
            self.check_session_loss(session_pnl),
            self.check_balance(balance_usd, stake_usd),
            self.check_position_count(open_positions),
            self.check_trade_rate(),
        ]
        for ok, reason in checks:
            if not ok:
                logger.warning("Risk check FAILED: %s", reason)
                return False, reason
        return True, "all checks passed"
