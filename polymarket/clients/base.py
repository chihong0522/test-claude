"""Base async HTTP client with rate limiting, retry, and backoff."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Default retry config
MAX_RETRIES = 5
INITIAL_BACKOFF = 1.0
MAX_BACKOFF = 60.0
BACKOFF_FACTOR = 2.0


class RateLimiter:
    """Token-bucket rate limiter scoped to a base URL."""

    def __init__(self, max_requests: int, window_seconds: float = 10.0):
        self._max = max_requests
        self._window = window_seconds
        self._tokens = max_requests
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            refill = int(elapsed / self._window * self._max)
            if refill > 0:
                self._tokens = min(self._max, self._tokens + refill)
                self._last_refill = now
            if self._tokens <= 0:
                wait = self._window / self._max
                await asyncio.sleep(wait)
                self._tokens = 1
            self._tokens -= 1


class BaseClient:
    """Async HTTP client with rate limiting, retry, and exponential backoff."""

    def __init__(
        self,
        base_url: str,
        rate_limit: int = 2000,
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self._limiter = RateLimiter(rate_limit)
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            headers={"Accept": "application/json"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET with rate limiting and retry."""
        await self._limiter.acquire()
        backoff = INITIAL_BACKOFF
        last_exc: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = await self._client.get(path, params=params)
                if resp.status_code == 429:
                    retry_after_raw = resp.headers.get("Retry-After")
                    try:
                        retry_after = float(retry_after_raw) if retry_after_raw is not None else backoff
                    except (TypeError, ValueError):
                        retry_after = backoff
                    retry_after = max(retry_after, backoff, 0.5)
                    last_exc = RuntimeError(f"Rate limited on {path}")
                    logger.warning("Rate limited on %s, waiting %.1fs", path, retry_after)
                    await asyncio.sleep(retry_after)
                    backoff = min(backoff * BACKOFF_FACTOR, MAX_BACKOFF)
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code >= 500:
                    logger.warning(
                        "Server error %s on %s (attempt %d/%d)",
                        e.response.status_code, path, attempt, MAX_RETRIES,
                    )
                    last_exc = e
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * BACKOFF_FACTOR, MAX_BACKOFF)
                    continue
                raise
            except (httpx.ConnectError, httpx.ReadTimeout) as e:
                logger.warning("Connection error on %s (attempt %d/%d): %s", path, attempt, MAX_RETRIES, e)
                last_exc = e
                await asyncio.sleep(backoff)
                backoff = min(backoff * BACKOFF_FACTOR, MAX_BACKOFF)
                continue

        raise RuntimeError(f"Failed after {MAX_RETRIES} retries on {path}") from last_exc

    async def get_paginated(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        limit: int = 100,
        max_pages: int = 200,
    ) -> list[dict]:
        """Auto-paginate through offset-based results."""
        params = dict(params or {})
        params["limit"] = limit
        all_results: list[dict] = []

        for page in range(max_pages):
            params["offset"] = page * limit
            data = await self.get(path, params)
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("data", data.get("results", []))
                if isinstance(items, dict):
                    items = [items]
            else:
                break
            all_results.extend(items)
            if len(items) < limit:
                break

        return all_results
