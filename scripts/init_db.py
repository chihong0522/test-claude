#!/usr/bin/env python3
"""Initialize the database schema."""
import asyncio

from polymarket.db import init_db


async def main():
    print("Initializing database...")
    await init_db()
    print("Database initialized successfully.")


if __name__ == "__main__":
    asyncio.run(main())
