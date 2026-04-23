"""JSON-file-based position persistence for crash recovery.

If the bot crashes mid-position, it needs to know what it owns on
restart. This store writes to a JSON file after every state change
(entry, exit, flip) so recovery is just "read the file".

The file is kept small (one active position max) and is atomically
written (write-to-temp + rename) to avoid corruption on crash.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_STORE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "live_position.json"


@dataclass
class LivePosition:
    market_slug: str
    condition_id: str
    token_id: str  # the token we hold (up or down)
    side: str  # "YES" or "NO"
    entry_price: float
    size: float  # shares
    cost_usd: float
    order_id: str = ""
    order_status: str = ""
    entered_at: str = ""  # ISO timestamp
    entry_bucket: int = 0
    sizing_tier: str = ""


@dataclass
class PositionStoreState:
    active_position: LivePosition | None = None
    session_realized_pnl: float = 0.0
    session_trade_count: int = 0
    session_started_at: str = ""
    daily_pnl: float = 0.0
    daily_trade_count: int = 0
    daily_date: str = ""  # YYYY-MM-DD, reset at midnight UTC
    exited_positions: list[dict] = field(default_factory=list)


class PositionStore:
    """Persistent position tracker with atomic file writes."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or DEFAULT_STORE_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.state = self._load()

    def _load(self) -> PositionStoreState:
        if not self.path.exists():
            return PositionStoreState(
                session_started_at=datetime.utcnow().isoformat() + "Z"
            )
        try:
            with open(self.path) as f:
                data = json.load(f)
            state = PositionStoreState()
            state.session_realized_pnl = data.get("session_realized_pnl", 0.0)
            state.session_trade_count = data.get("session_trade_count", 0)
            state.session_started_at = data.get("session_started_at", "")
            state.daily_pnl = data.get("daily_pnl", 0.0)
            state.daily_trade_count = data.get("daily_trade_count", 0)
            state.daily_date = data.get("daily_date", "")
            state.exited_positions = data.get("exited_positions", [])
            pos_data = data.get("active_position")
            if pos_data:
                state.active_position = LivePosition(**pos_data)
            return state
        except Exception as e:
            logger.error("Failed to load position store: %s", e)
            return PositionStoreState(
                session_started_at=datetime.utcnow().isoformat() + "Z"
            )

    def _save(self) -> None:
        data = {
            "active_position": asdict(self.state.active_position) if self.state.active_position else None,
            "session_realized_pnl": round(self.state.session_realized_pnl, 2),
            "session_trade_count": self.state.session_trade_count,
            "session_started_at": self.state.session_started_at,
            "daily_pnl": round(self.state.daily_pnl, 2),
            "daily_trade_count": self.state.daily_trade_count,
            "daily_date": self.state.daily_date,
            "exited_positions": self.state.exited_positions[-50:],  # keep last 50
            "saved_at": datetime.utcnow().isoformat() + "Z",
        }
        # Atomic write: temp file + rename
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, str(self.path))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _check_daily_reset(self) -> None:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        if self.state.daily_date != today:
            self.state.daily_pnl = 0.0
            self.state.daily_trade_count = 0
            self.state.daily_date = today

    def record_entry(self, pos: LivePosition) -> None:
        self._check_daily_reset()
        self.state.active_position = pos
        self.state.session_trade_count += 1
        self.state.daily_trade_count += 1
        self._save()
        logger.info("Position stored: %s %s @ %.4f", pos.side, pos.market_slug, pos.entry_price)

    def record_exit(self, realized_pnl: float, reason: str) -> None:
        self._check_daily_reset()
        pos = self.state.active_position
        exit_record = {
            "market_slug": pos.market_slug if pos else "?",
            "side": pos.side if pos else "?",
            "entry_price": pos.entry_price if pos else 0,
            "realized_pnl": round(realized_pnl, 2),
            "reason": reason,
            "exited_at": datetime.utcnow().isoformat() + "Z",
        }
        self.state.exited_positions.append(exit_record)
        self.state.session_realized_pnl += realized_pnl
        self.state.daily_pnl += realized_pnl
        self.state.active_position = None
        self._save()
        logger.info("Position exited: %s (PnL=$%.2f)", reason, realized_pnl)

    def has_open_position(self) -> bool:
        return self.state.active_position is not None

    def get_daily_pnl(self) -> float:
        self._check_daily_reset()
        return self.state.daily_pnl

    def get_session_pnl(self) -> float:
        return self.state.session_realized_pnl

    def clear(self) -> None:
        self.state.active_position = None
        self._save()
