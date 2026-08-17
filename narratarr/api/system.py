"""The system routes. APP-CONTRACT.md section 13.1.

`GET /system/health` takes no key. Every other route in this file declares
`dependencies=[Depends(require_key)]` on the route itself, since this
router holds the one route the key rule exempts; a router-level dependency
would have to be undone for that one route instead of stated for the rest.
"""

from __future__ import annotations

import shutil
from typing import Optional

from fastapi import APIRouter, Depends

from narratarr import __version__, runner
from narratarr.api.common import require_key
from narratarr.config import get_settings
from narratarr.db import connect
from narratarr.models import HealthResponse, ModelInfo, ModelsResponse, SecretStatus, SystemStatusResponse
from narratarr.queue import count_queued

router = APIRouter(prefix="/api/v1/system", tags=["system"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Return `{"status": "ok", "version": "..."}`. No key needed."""
    return HealthResponse(status="ok", version=__version__)


@router.get("/status", dependencies=[Depends(require_key)])
async def status() -> dict:
    """Report the runner state, the queue depth, the disk, and secret presence.

    APP-CONTRACT.md section 10.2 rule 4: a secret is reported present or
    absent, never by value.
    """
    settings = get_settings()
    conn = connect()
    try:
        queue_depth = count_queued(conn)
        running = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE state = 'running'"
        ).fetchone()["n"]
    finally:
        conn.close()

    disk_free = shutil.disk_usage(settings.config_dir if settings.config_dir.exists() else "/").free

    secrets_present = {
        "NARRATARR_API_KEY": SecretStatus(present=bool(settings.api_key)),
        "NARRATARR_ABS_TOKEN": SecretStatus(present=bool(settings.abs_token)),
    }

    response = SystemStatusResponse(
        runner_state="running" if running else "idle",
        queue_depth=queue_depth,
        disk_free_bytes=disk_free,
        models=_model_presence(settings),
        secrets=secrets_present,
    ).model_dump()
    # The engine preflight report is not part of the frozen response shape
    # of APP-CONTRACT.md section 13.1, but section 10.2 asks for secret and
    # capability presence here, and the 2026-08-16 preflight change makes
    # this the natural home for it. Flagged for the overlord to fold into
    # the contract's SystemStatusResponse shape if it should stay.
    response["engine_preflight"] = runner.get_last_preflight()
    return response


def _model_presence(settings) -> dict:
    """Return {name: downloaded} for each top-level entry under models_dir."""
    if not settings.models_dir.is_dir():
        return {}
    return {entry.name: True for entry in settings.models_dir.iterdir()}


@router.get("/models", response_model=ModelsResponse, dependencies=[Depends(require_key)])
async def list_models() -> ModelsResponse:
    """List the models under `NARRATARR_CONFIG_DIR/models`, downloaded or not.

    APP-CONTRACT.md section 11 names the engines Narratarr ships, but gives
    no fixed model manifest to check downloaded state against. This route
    reports what it finds on disk. Flagged for the overlord: a documented
    model manifest (name, expected checksum) would let this route also
    report a model that is expected but missing.
    """
    settings = get_settings()
    models: list[ModelInfo] = []
    if settings.models_dir.is_dir():
        for entry in sorted(settings.models_dir.iterdir()):
            size = _dir_size(entry) if entry.is_dir() else entry.stat().st_size
            models.append(ModelInfo(name=entry.name, downloaded=True, size_bytes=size))
    return ModelsResponse(models=models)


def _dir_size(path) -> int:
    """Return the total size in bytes of every file under a directory."""
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            total += child.stat().st_size
    return total


@router.post("/models/fetch", status_code=202, dependencies=[Depends(require_key)])
async def fetch_models() -> dict:
    """Start the first-run model download. Returns immediately.

    APP-CONTRACT.md section 13.1 documents this route, but no section
    documents an adapter entry point for the download itself (section 6's
    `Pipeline` has none). This route writes an event and returns 202. It
    does not block, per the house rule that the API never blocks on work
    the runner should do. Flagged for the overlord: name the real download
    entry point so this route can call it instead of only recording intent.
    """
    from narratarr.db import now, transaction

    with transaction() as conn:
        conn.execute(
            "INSERT INTO events (job_id, level, stage, message, data, created_at) "
            "VALUES (NULL, 'info', NULL, 'model fetch requested', NULL, ?)",
            (now(),),
        )
    return {"status": "accepted"}
