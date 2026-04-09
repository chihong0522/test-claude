"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from polymarket.api.routers import backtests, pipeline, reports, traders
from polymarket.config import settings
from polymarket.db import init_db

logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await init_db()
    logger.info("Database initialized")

    # Schedule daily pipeline
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        pipeline._run_pipeline_task,
        CronTrigger(hour=settings.pipeline_schedule_hour, minute=settings.pipeline_schedule_minute),
        id="daily_pipeline",
        name="Daily Research Pipeline",
    )
    scheduler.start()
    logger.info(
        "Scheduler started — pipeline runs daily at %02d:%02d UTC",
        settings.pipeline_schedule_hour,
        settings.pipeline_schedule_minute,
    )

    yield

    # Shutdown
    scheduler.shutdown()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Polymarket Copy Trading Research",
        description="Find, score, and backtest the most profitable Polymarket traders",
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS for local development
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # API routers
    app.include_router(traders.router)
    app.include_router(reports.router)
    app.include_router(backtests.router)
    app.include_router(pipeline.router)

    # Health check
    @app.get("/api/health")
    async def health():
        return {"status": "ok", "version": "0.1.0"}

    # Static files
    static_dir = FRONTEND_DIR / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # SPA fallback — serve index.html for all non-API routes
    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        index = FRONTEND_DIR / "index.html"
        if index.exists():
            return FileResponse(str(index))
        return {"message": "Frontend not built. Access API at /api/health"}

    return app


app = create_app()
