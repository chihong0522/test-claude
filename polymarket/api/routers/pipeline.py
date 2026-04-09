"""Pipeline control API endpoints."""

from __future__ import annotations

import asyncio
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from polymarket.api.schemas import PipelineStatus
from polymarket.db import async_session, get_session
from polymarket.reporter.daily_pipeline import run_daily_pipeline

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])

# Simple in-memory state for pipeline status
_pipeline_state = {
    "running": False,
    "last_run": None,
    "last_result": None,
}


async def _run_pipeline_task():
    """Background task for running the pipeline."""
    _pipeline_state["running"] = True
    try:
        async with async_session() as session:
            result = await run_daily_pipeline(session)
            _pipeline_state["last_result"] = result
            _pipeline_state["last_run"] = datetime.utcnow()
    finally:
        _pipeline_state["running"] = False


@router.post("/trigger")
async def trigger_pipeline(background_tasks: BackgroundTasks):
    """Manually trigger the daily pipeline."""
    if _pipeline_state["running"]:
        return {"status": "already_running"}

    background_tasks.add_task(_run_pipeline_task)
    return {"status": "started"}


@router.get("/status", response_model=PipelineStatus)
async def get_pipeline_status():
    """Get current pipeline status."""
    return PipelineStatus(
        running=_pipeline_state["running"],
        last_run=_pipeline_state["last_run"],
        last_result=_pipeline_state["last_result"],
    )
