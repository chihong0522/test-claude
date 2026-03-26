#!/usr/bin/env python3
"""
Monitor Limitless Exchange Market Maker limit orders.
Sends a Discord notification when a Limit Buy or Limit Sell order is filled
within the last 60 seconds.
"""
import datetime
import json
import logging
import os
import signal
import sys
import time
import requests
# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_URL = "https://api.limitless.exchange"
API_KEY = os.environ.get("LIMITLESS_API_KEY", "")
DISCORD_WEBHOOK_URL = os.environ.get(
    "DISCORD_WEBHOOK_URL",
    "https://discord.com/api/webhooks/1486619070626271302/BXd_uQKMfaNlTIJVruKkfS0qEhz_VFHM_9eB53cIUtVTaqewILrjm6WnS3cA-WMtYVMi",
)
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "10"))
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOOKBACK_SECONDS = 60  # only consider fills from the last 60s
SEEN_TTL = 300         # prune seen_tx_hashes entries older than 5 minutes
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LIMIT_STRATEGIES = {"Limit Sell", "Limit Buy"}
# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
            if state.get("schema_version") == 1:
                return state
            logging.warning("Unrecognised state schema, resetting.")
        except (json.JSONDecodeError, OSError) as e:
            logging.warning("Could not load state file (%s), starting fresh.", e)
    return {"schema_version": 1, "seen_tx_hashes": {}}
def save_state(state: dict) -> None:
    # Prune entries older than SEEN_TTL to keep the file small
    cutoff = int(time.time()) - SEEN_TTL
    state["seen_tx_hashes"] = {
        tx: ts
        for tx, ts in state["seen_tx_hashes"].items()
        if ts >= cutoff
    }
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except OSError as e:
        logging.error("Failed to save state: %s", e)
# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def api_get(path: str, params: dict | None = None) -> dict | None:
    url = f"{BASE_URL}{path}"
    headers = {"X-API-Key": API_KEY}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError as e:
        logging.error("HTTP %s for %s: %s", resp.status_code, url, e)
    except requests.exceptions.RequestException as e:
        logging.error("Network error for %s: %s", url, e)
    return None
# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def is_limit_order(event: dict) -> bool:
    return event.get("strategy") in LIMIT_STRATEGIES
def format_ts(ts) -> str:
    try:
        return datetime.datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (TypeError, ValueError):
        return str(ts)
def truncate_hash(tx: str) -> str:
    if len(tx) > 12:
        return tx[:6] + "..." + tx[-4:]
    return tx
# ---------------------------------------------------------------------------
# Discord notification
# ---------------------------------------------------------------------------
def send_discord_notification(event: dict) -> None:
    strategy = event.get("strategy", "Limit Order")
    market = event.get("market") or {}
    market_title = market.get("title") or market.get("slug") or "Unknown market"
    outcome_index = event.get("outcomeIndex")
    outcome_label = "YES" if outcome_index == 0 else ("NO" if outcome_index == 1 else str(outcome_index))
    price = event.get("outcomeTokenPrice", "?")
    amount = event.get("outcomeTokenAmount", "?")
    collateral = event.get("collateralAmount", "?")
    collateral_symbol = (market.get("collateral") or {}).get("symbol", "USDC")
    tx_hash = event.get("transactionHash", "")
    ts = event.get("blockTimestamp", "")
    color = 0x00C853 if strategy == "Limit Buy" else 0xD50000  # green / red
    embed = {
        "title": f"{strategy} Filled",
        "color": color,
        "fields": [
            {"name": "Market", "value": market_title, "inline": False},
            {"name": "Outcome", "value": outcome_label, "inline": True},
            {"name": "Fill Price", "value": str(price), "inline": True},
            {"name": "Token Amount", "value": str(amount), "inline": True},
            {"name": f"{'Received' if strategy == 'Limit Sell' else 'Spent'} ({collateral_symbol})",
             "value": str(collateral), "inline": True},
            {"name": "Tx Hash", "value": f"`{truncate_hash(tx_hash)}`", "inline": True},
        ],
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "footer": {"text": f"Limitless MM Monitor  •  Filled at {format_ts(ts)}"},
    }
    payload = {"embeds": [embed]}
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        resp.raise_for_status()
        logging.info(
            "Notified: %s | %s | price=%s amount=%s tx=%s",
            strategy, market_title, price, amount, truncate_hash(tx_hash),
        )
    except requests.exceptions.RequestException as e:
        logging.error("Discord notification failed: %s", e)
# ---------------------------------------------------------------------------
# Poll cycle
# ---------------------------------------------------------------------------
def run_poll_cycle(state: dict) -> None:
    cutoff = int(time.time()) - LOOKBACK_SECONDS
    seen = state["seen_tx_hashes"]
    data = api_get("/portfolio/history", {"page": 1, "limit": 50})
    if data is None:
        return
    events = data.get("data", [])
    new_fills = []
    for event in events:
        tx_hash = event.get("transactionHash")
        if not tx_hash:
            continue  # resolution event (won/loss), no tx hash
        if not is_limit_order(event):
            continue
        try:
            block_ts = int(event["blockTimestamp"])
        except (KeyError, TypeError, ValueError):
            continue
        if block_ts < cutoff:
            continue  # older than 60s
        if tx_hash in seen:
            continue  # already notified
        new_fills.append(event)
    if not new_fills:
        return
    # Sort oldest-first so notifications appear in chronological order
    new_fills.sort(key=lambda e: int(e.get("blockTimestamp", 0)))
    for event in new_fills:
        tx_hash = event["transactionHash"]
        block_ts = int(event["blockTimestamp"])
        seen[tx_hash] = block_ts
        send_discord_notification(event)
    save_state(state)
# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    if not API_KEY:
        logging.critical("LIMITLESS_API_KEY environment variable is required.")
        sys.exit(1)
    state = load_state()
    def _handle_signal(sig, frame):
        logging.info("Shutdown signal received, exiting cleanly.")
        save_state(state)
        sys.exit(0)
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    logging.info(
        "MM Order Monitor started. Poll interval: %ds | Lookback: %ds",
        POLL_INTERVAL, LOOKBACK_SECONDS,
    )
    while True:
        try:
            run_poll_cycle(state)
        except Exception as e:
            logging.error("Unhandled error in poll cycle: %s", e, exc_info=True)
        time.sleep(POLL_INTERVAL)
if __name__ == "__main__":
    main()
