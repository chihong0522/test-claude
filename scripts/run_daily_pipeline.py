#!/usr/bin/env python3
"""Run the daily research pipeline manually."""
import asyncio
import json
import logging

from polymarket.db import async_session, init_db
from polymarket.reporter.daily_pipeline import run_daily_pipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


async def main():
    await init_db()
    async with async_session() as session:
        result = await run_daily_pipeline(session)
        print("\n=== Pipeline Result ===")
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
