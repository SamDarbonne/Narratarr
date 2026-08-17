"""The job routes. APP-CONTRACT.md section 13.2.

**This module never imports `abpipe` or `narratarr.adapter`.** Refer to
APP-CONTRACT.md section 3. Where a route needs a pipeline fact (the
per-stage status, the artifact list), it calls a helper in `narratarr.runner`
that does the lazy import instead, so this rule holds even for those two
routes.

**No route here runs the pipeline.** Every route that changes what work the
runner should do next writes a row and returns; the runner picks the work
up on its own schedule. Refer to the house rule of APP-CONTRACT.md
section 9.4.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from narratarr import runner
from narratarr.api.common import ApiError, paginate, require_key
from narratarr.config import get_settings
from narratarr.db import connect, new_id, now, transaction
from narratarr.models import Delivery, Event, Gate, Job, JobConfigUpdate

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"], dependencies=[Depends(require_key)])

# The closed set of APP-CONTRACT.md section 5.
JOB_STATES = {
    "queued", "running", "awaiting_sample_approval", "awaiting_homograph_review",
    "awaiting_qc_review", "delivering", "done", "failed", "cancelled", "paused",
}

_SLUG_RE = re.compile(r"[^a-z0-9]+")


# ------------------------------------------------------------------ helpers


def _slugify(text: str) -> str:
    """Return a lower-case, hyphen-only slug of a title."""
    slug = _SLUG_RE.sub("-", text.strip().lower()).strip("-")
    return slug or "book"


def _unique_slug(conn, base: str) -> str:
    """Return `base`, or `base` with a numeric suffix when it collides."""
    slug = base
    suffix = 2
    while conn.execute("SELECT 1 FROM jobs WHERE slug = ?", (slug,)).fetchone():
        slug = f"{base}-{suffix}"
        suffix += 1
    return slug


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write bytes atomically. APP-CONTRACT.md section 15 rule 5.

    Writes a temporary file in the same directory, flushes it to disk, then
    moves it into place with `os.replace`. A failed write removes its own
    temporary file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _get_job(job_id: str) -> Job:
    """Return the job, or raise a 404 `ApiError`."""
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ApiError("not_found", f"no job {job_id}", status=404)
    return Job.from_row(row)


def _require_state(job: Job, allowed: set, action: str) -> None:
    """Raise a 409 `ApiError` when the job is not in an allowed state."""
    if job.state not in allowed:
        raise ApiError(
            "conflict", f"cannot {action} a job in state {job.state}", status=409,
            detail={"state": job.state},
        )


def _job_list_dict(job: Job) -> dict:
    """Return a job dict for the list route. Drops the two config blobs."""
    data = job.to_dict()
    data.pop("book_config", None)
    data.pop("qc_config", None)
    return data


def _job_detail_dict(job: Job) -> dict:
    """Return a job dict with its gates and its deliveries, for one-job routes."""
    conn = connect()
    try:
        gates = [
            Gate.from_row(r).to_dict()
            for r in conn.execute(
                "SELECT * FROM gates WHERE job_id = ? ORDER BY created_at ASC", (job.id,)
            ).fetchall()
        ]
        deliveries = [
            Delivery.from_row(r).to_dict()
            for r in conn.execute(
                "SELECT * FROM deliveries WHERE job_id = ? ORDER BY created_at ASC", (job.id,)
            ).fetchall()
        ]
    finally:
        conn.close()
    data = job.to_dict()
    data["gates"] = gates
    data["deliveries"] = deliveries
    return data


def _str_or_none(value) -> Optional[str]:
    """Return a stripped string, or None for an absent or empty form field."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# --------------------------------------------------------------------- list


@router.get("")
async def list_jobs(
    state: Optional[str] = None, q: Optional[str] = None, limit: int = 50, offset: int = 0
) -> dict:
    """List jobs. Filters: `?state=`, `?q=`. Paginates per section 13."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    clauses = []
    params: list = []
    if state:
        clauses.append("state = ?")
        params.append(state)
    if q:
        like = f"%{q}%"
        clauses.append("(title LIKE ? OR author LIKE ? OR slug LIKE ?)")
        params.extend([like, like, like])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT * FROM jobs {where} ORDER BY priority DESC, created_at ASC", params
        ).fetchall()
    finally:
        conn.close()

    items = [_job_list_dict(Job.from_row(row)) for row in rows]
    return paginate(items, limit, offset)


# ------------------------------------------------------------------- create


@router.post("", status_code=201)
async def create_job(request: Request) -> dict:
    """Make a job, from an upload or from `{"source_path": "..."}`.

    An unsupported extension still makes the job, immediately `failed`,
    with a message that names the extension. APP-CONTRACT.md section 7:
    "never a silent skip."
    """
    settings = get_settings()
    content_type = request.headers.get("content-type", "")

    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is None:
            raise ApiError("validation_error", "the multipart body needs a 'file' field", status=422)
        raw_bytes = await upload.read()
        original_name = upload.filename or "upload.epub"
        title = _str_or_none(form.get("title"))
        author = _str_or_none(form.get("author"))
        year = _str_or_none(form.get("year"))
        genre = _str_or_none(form.get("genre"))
        language = _str_or_none(form.get("language")) or "en"
        priority = int(form.get("priority") or 0)
        allow_duplicate = _str_or_none(form.get("allow_duplicate") or "").lower() in (
            "1", "true", "yes", "on",
        ) if form.get("allow_duplicate") is not None else False
    else:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            raise ApiError(
                "validation_error", "the body must be JSON, or multipart/form-data", status=422
            )
        if not body.get("source_path"):
            raise ApiError("validation_error", "source_path is required", status=422)
        source = Path(body["source_path"])
        if not source.is_file():
            raise ApiError("not_found", f"no file at {source}", status=404)
        raw_bytes = source.read_bytes()
        original_name = source.name
        title = _str_or_none(body.get("title"))
        author = _str_or_none(body.get("author"))
        year = _str_or_none(body.get("year"))
        genre = _str_or_none(body.get("genre"))
        language = _str_or_none(body.get("language")) or "en"
        priority = int(body.get("priority") or 0)
        allow_duplicate = bool(body.get("allow_duplicate", False))

    source_sha256 = hashlib.sha256(raw_bytes).hexdigest()

    with transaction() as conn:
        if not allow_duplicate:
            dup = conn.execute(
                "SELECT id, slug FROM jobs WHERE source_sha256 = ?", (source_sha256,)
            ).fetchone()
            if dup is not None:
                raise ApiError(
                    "duplicate", f"job {dup['slug']} already holds this file", status=409,
                    detail={"job_id": dup["id"]},
                )

        display_title = title or Path(original_name).stem
        slug = _unique_slug(conn, _slugify(display_title))
        extension = Path(original_name).suffix.lower()
        target_path = settings.library_dir / f"{slug}{extension or '.epub'}"
        _atomic_write_bytes(target_path, raw_bytes)

        job_id = new_id()
        stamp = now()
        unsupported = extension != ".epub"
        state = "failed" if unsupported else "queued"
        error = f"unsupported extension: {extension or '(none)'}" if unsupported else None

        conn.execute(
            """
            INSERT INTO jobs (
                id, slug, title, author, year, genre, language, source_path,
                source_sha256, state, stage, worker, priority, progress_done,
                progress_total, error, book_config, qc_config, created_at,
                updated_at, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'local', ?, 0, 0, ?, '{}', '{}', ?, ?, NULL, ?)
            """,
            (
                job_id, slug, title, author, year, genre, language, str(target_path),
                source_sha256, state, priority, error, stamp, stamp,
                stamp if unsupported else None,
            ),
        )
        runner.write_event(conn, job_id, "info", f"job created from {original_name}")
        if unsupported:
            runner.write_event(conn, job_id, "error", error)

        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()

    return _job_detail_dict(Job.from_row(row))


# --------------------------------------------------------------------- read


@router.get("/{job_id}")
async def get_job(job_id: str) -> dict:
    """Return one job, with its gates and its deliveries."""
    return _job_detail_dict(_get_job(job_id))


@router.delete("/{job_id}")
async def delete_job(job_id: str, purge: bool = False) -> dict:
    """Delete the job. `?purge=true` also deletes its work directory."""
    job = _get_job(job_id)
    with transaction() as conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    if purge:
        settings = get_settings()
        shutil.rmtree(settings.work_dir / job.slug, ignore_errors=True)
    return {"deleted": True, "id": job_id}


# ------------------------------------------------------------------ control


@router.post("/{job_id}/start", status_code=202)
async def start_job(job_id: str) -> dict:
    """Resume a paused job. `(any state) -> paused -> queued`."""
    job = _get_job(job_id)
    _require_state(job, {"paused"}, "start")
    with transaction() as conn:
        conn.execute("UPDATE jobs SET state = 'queued', updated_at = ? WHERE id = ?", (now(), job_id))
        runner.write_event(conn, job_id, "info", "job resumed from pause")
    return {"accepted": True}


@router.post("/{job_id}/pause", status_code=202)
async def pause_job(job_id: str) -> dict:
    """Pause the job. Refer to the runner's cooperative pause check.

    APP-CONTRACT.md section 6 gives no interrupt token for a stage already
    in progress. A pause of a `running` job takes effect once the current
    stage finishes, not mid-stage. Refer to `narratarr/runner.py`,
    `process_job`.
    """
    job = _get_job(job_id)
    _require_state(job, JOB_STATES - {"done", "cancelled", "paused"}, "pause")
    with transaction() as conn:
        conn.execute("UPDATE jobs SET state = 'paused', updated_at = ? WHERE id = ?", (now(), job_id))
        runner.write_event(conn, job_id, "info", "job paused")
    return {"accepted": True}


@router.post("/{job_id}/cancel", status_code=202)
async def cancel_job(job_id: str) -> dict:
    """Cancel the job. `(any state) -> cancelled`."""
    job = _get_job(job_id)
    _require_state(job, JOB_STATES - {"done", "cancelled"}, "cancel")
    with transaction() as conn:
        conn.execute(
            "UPDATE jobs SET state = 'cancelled', updated_at = ?, finished_at = ? WHERE id = ?",
            (now(), now(), job_id),
        )
        runner.write_event(conn, job_id, "info", "job cancelled")
    return {"accepted": True}


@router.post("/{job_id}/retry", status_code=202)
async def retry_job(job_id: str) -> dict:
    """Clear the error and queue the job again."""
    job = _get_job(job_id)
    _require_state(job, {"failed", "cancelled"}, "retry")
    with transaction() as conn:
        conn.execute(
            "UPDATE jobs SET state = 'queued', error = NULL, updated_at = ? WHERE id = ?",
            (now(), job_id),
        )
        runner.write_event(conn, job_id, "info", "job retried")
    return {"accepted": True}


# ------------------------------------------------------------------- config


@router.get("/{job_id}/config")
async def get_job_config(job_id: str) -> dict:
    """Return the book config and the QC config."""
    job = _get_job(job_id)
    return {
        "book_config": json.loads(job.book_config or "{}"),
        "qc_config": json.loads(job.qc_config or "{}"),
    }


@router.put("/{job_id}/config")
async def put_job_config(job_id: str, body: JobConfigUpdate) -> dict:
    """Replace the book config and the QC config. `409` while the job runs."""
    job = _get_job(job_id)
    if job.state == "running":
        raise ApiError("conflict", "cannot edit the config of a running job", status=409)
    with transaction() as conn:
        conn.execute(
            "UPDATE jobs SET book_config = ?, qc_config = ?, updated_at = ? WHERE id = ?",
            (json.dumps(body.book_config), json.dumps(body.qc_config), now(), job_id),
        )
        runner.write_event(conn, job_id, "info", "job config replaced")
    return {"book_config": body.book_config, "qc_config": body.qc_config}


# -------------------------------------------------------------------- logs


@router.get("/{job_id}/events")
async def get_job_events(
    job_id: str, since: Optional[int] = None, level: Optional[str] = None, limit: int = 200
) -> dict:
    """Return the job's log. Filters: `?since=`, `?level=`."""
    _get_job(job_id)
    limit = max(1, min(limit, 1000))

    clauses = ["job_id = ?"]
    params: list = [job_id]
    if since is not None:
        clauses.append("id > ?")
        params.append(since)
    if level:
        clauses.append("level = ?")
        params.append(level)

    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT * FROM events WHERE {' AND '.join(clauses)} ORDER BY id ASC LIMIT ?",
            (*params, limit),
        ).fetchall()
    finally:
        conn.close()
    return {"items": [Event.from_row(row).to_dict() for row in rows]}


@router.get("/{job_id}/events/stream")
async def stream_job_events(job_id: str, request: Request) -> StreamingResponse:
    """Stream the job's log as server-sent events."""
    _get_job(job_id)

    async def _generate():
        last_id = 0
        while True:
            if await request.is_disconnected():
                break
            conn = connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM events WHERE job_id = ? AND id > ? ORDER BY id ASC",
                    (job_id, last_id),
                ).fetchall()
            finally:
                conn.close()
            for row in rows:
                event = Event.from_row(row).to_dict()
                last_id = event["id"]
                yield f"id: {event['id']}\ndata: {json.dumps(event)}\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(_generate(), media_type="text/event-stream")


# --------------------------------------------------------- pipeline facts


@router.get("/{job_id}/artifacts")
async def get_job_artifacts(job_id: str) -> dict:
    """Return the artifact paths and sizes. Never renders."""
    return runner.get_pipeline_artifacts(_get_job(job_id))


@router.get("/{job_id}/status")
async def get_job_pipeline_status(job_id: str) -> dict:
    """Return the per-stage fresh, stale, and absent count. Never renders."""
    return runner.get_pipeline_status(_get_job(job_id))


# --------------------------------------------------------------- delivery


@router.post("/{job_id}/deliver", status_code=202)
async def deliver_job(job_id: str) -> dict:
    """Deliver to every enabled target. Writes state; the runner does the work."""
    job = _get_job(job_id)
    _require_state(job, {"done", "failed"}, "deliver")
    with transaction() as conn:
        conn.execute(
            "UPDATE jobs SET state = 'queued', stage = 'deliver', error = NULL, updated_at = ? "
            "WHERE id = ?",
            (now(), job_id),
        )
        runner.write_event(conn, job_id, "info", "delivery requested")
    return {"accepted": True}


@router.post("/{job_id}/fix", status_code=202)
async def fix_job(job_id: str, body: dict) -> dict:
    """The Fix flow of APP-CONTRACT.md section 9.5.

    ASSUMPTION, flagged for the overlord: section 6's `Pipeline` class has
    no method to apply a single pronunciation or a forced homograph
    decision on its own; only a full `homograph_audit()` run touches
    `homographs.json`. This route stores a "pronunciation" or a
    "homograph" correction directly in the job's `book_config.pronunciations`
    map (the field abpipe CONTRACT.md section 4.1 already defines), then
    resumes the job at the `render` stage. The pipeline's own idempotence
    (pipeline CONTRACT.md section 3) then re-renders only the chunks the
    correction actually stales; this route asks for no explicit chapter
    list. A "rerender" action needs no correction applied; it only asks for
    the render stage to run again.
    """
    job = _get_job(job_id)
    items = body.get("items")
    if not items:
        raise ApiError("validation_error", "items must hold at least one entry", status=422)
    for item in items:
        if not item.get("reason"):
            raise ApiError("validation_error", "every fix item needs a reason", status=422)
        if item.get("action") not in ("rerender", "pronunciation", "homograph"):
            raise ApiError(
                "validation_error", f"unknown action {item.get('action')!r}", status=422
            )

    book_config = json.loads(job.book_config or "{}")
    pronunciations = book_config.setdefault("pronunciations", {})
    for item in items:
        if item.get("action") in ("pronunciation", "homograph"):
            value = item.get("value") or {}
            word = value.get("word") or item.get("word")
            reading = value.get("reading") or value.get("phonemes")
            if word and reading:
                pronunciations[word] = reading

    with transaction() as conn:
        conn.execute(
            "UPDATE jobs SET book_config = ?, state = 'queued', stage = 'render', "
            "error = NULL, updated_at = ? WHERE id = ?",
            (json.dumps(book_config), now(), job_id),
        )
        runner.write_event(conn, job_id, "info", "fix requested", data={"items": items})
    return {"accepted": True}
