"""FastAPI service: REST API + live status page + built-in scheduler.

    uvicorn statuspulse.api:app            # serve
    RUN_SCHEDULER=1 uvicorn statuspulse.api:app   # also run checks every interval_s
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import os

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse

from . import config
from .page import build_status, render_html
from .runner import run_cycle
from .store import Store

log = logging.getLogger("statuspulse")

SETTINGS = config.load(os.environ.get("MONITORS_FILE", "monitors.toml"))
STORE = Store(os.environ.get("STATUSPULSE_DB", "data/statuspulse.db"))


async def _scheduler():
    while True:
        try:
            await run_cycle(SETTINGS, STORE)
        except Exception:  # keep the loop alive whatever happens in one cycle
            log.exception("monitoring cycle failed")
        await asyncio.sleep(SETTINGS.interval_s)


@contextlib.asynccontextmanager
async def lifespan(_app):
    task = asyncio.create_task(_scheduler()) if os.environ.get("RUN_SCHEDULER") == "1" else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="StatusPulse", version="1.0.0", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
def status_page():
    return render_html(build_status(SETTINGS, STORE), footer_note="Served live by the StatusPulse API.")


@app.get("/api/health")
def health():
    return {"status": "ok", "monitors": len(SETTINGS.monitors)}


@app.get("/api/status")
def status():
    return build_status(SETTINGS, STORE)


@app.get("/api/monitors/{name}/checks")
def checks(name: str, limit: int = 50):
    if name not in {m.name for m in SETTINGS.monitors}:
        raise HTTPException(404, f"unknown monitor {name!r}")
    return STORE.recent_checks(name, min(max(limit, 1), 500))


@app.get("/api/incidents")
def incidents(limit: int = 50, monitor: str | None = None):
    return STORE.incidents(min(max(limit, 1), 200), monitor)


@app.post("/api/run")
async def run_now(authorization: str | None = Header(default=None)):
    """Run one check cycle now. Requires `Authorization: Bearer <RUN_TOKEN>`."""
    token = os.environ.get("RUN_TOKEN")
    if not token:
        raise HTTPException(403, "manual runs are disabled (set RUN_TOKEN)")
    given = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(given, token):
        raise HTTPException(401, "invalid token")
    results, alerts = await run_cycle(SETTINGS, STORE)
    return {"checked": len(results), "alerts": [a.payload() for a in alerts]}
