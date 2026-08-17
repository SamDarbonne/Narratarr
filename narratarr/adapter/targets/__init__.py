"""The target layer. APP-CONTRACT section 8. Owner: W2.

Stage 8 of the pipeline contract is `abpipe.deliver` — the upstream author's own delivery
stage, with his server address hard-coded and an SSH read of his
Audiobookshelf database. APP-CONTRACT 3.1 forbids Narratarr from calling it.
This package is Narratarr's own stage 8: a small, stranger-safe delivery
layer that speaks plain HTTP, with no hard-coded address and no database
read.

`registry()` returns the map of `kind` to a target instance. `narratarr.api`
never imports `abpipe`; this map is API-owned code's one way to reach a
target's `validate`/`test`/`deliver`/`deliver_fix` without holding an
`abpipe` import of its own.

`deliver_job()` and `deliver_job_fix()` (APP-CONTRACT section 8.3, added at
the overlord's request) are the job-level entry points `narratarr/runner.py`
calls at the `deliver` stage and at the APP-CONTRACT 9.5 Fix flow. Neither
function imports `abpipe` directly — each builds a `narratarr.adapter.
Pipeline` and asks it for `deliver_book()`, which does the one `abpipe` call
(`ffmpeg.probe_duration`) this layer needs.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from narratarr.adapter.targets.audiobookshelf import AudiobookshelfTarget
from narratarr.adapter.targets.base import (
    DeliverBook,
    DeliveryResult,
    Progress,
    Target,
    TargetError,
)
from narratarr.adapter.targets.folder import FolderTarget

__all__ = [
    "AudiobookshelfTarget",
    "DeliverBook",
    "DeliveryResult",
    "FolderTarget",
    "Progress",
    "Target",
    "TargetError",
    "deliver_job",
    "deliver_job_fix",
    "registry",
]


def registry() -> dict[str, Target]:
    """Return the map of target `kind` to a target instance.

    A new instance each call — every target class in this package holds no
    per-call state of its own (config and book both arrive as arguments),
    so sharing one instance would cost nothing either way; a fresh map is
    simply the more obviously safe default for a caller who might mutate
    what it gets back.
    """
    return {
        "folder": FolderTarget(),
        "audiobookshelf": AudiobookshelfTarget(),
    }


# --------------------------------------------------------------------------- deliver_job


def _build_pipeline(job):
    """Build a `narratarr.adapter.Pipeline` for `job`, from its own stored
    configuration — the identical construction `narratarr/runner.py`'s own
    `_pipeline_for_job` uses, kept here as a private, independent copy
    rather than an import from `narratarr.runner`: this package must not
    depend on the runner, which already depends on it (through
    `deliver_job`/`deliver_job_fix`) — an import the other way would be a
    cycle.
    """
    from narratarr.adapter import Pipeline
    from narratarr.config import get_settings

    settings = get_settings()
    book_config = json.loads(job.book_config or "{}")
    qc_config = json.loads(job.qc_config or "{}")
    return Pipeline(settings.work_dir, job.slug, Path(job.source_path), book_config, qc_config)


def _enabled_target_rows(conn) -> list:
    """Return every row of `targets` where `enabled = 1`, oldest first."""
    return conn.execute("SELECT * FROM targets WHERE enabled = 1 ORDER BY created_at ASC").fetchall()


def _upsert_delivery(job_id: str, target_id: str, result: DeliveryResult) -> None:
    """Write or update the one `deliveries` row for `(job_id, target_id)`.

    APP-CONTRACT section 4.6: `idx_delivery_pair` is a unique index on
    exactly that pair, so a second delivery of the same job to the same
    target must UPDATE the existing row, never insert a second one — this
    is also what makes `deliver_job` idempotent at the database layer, on
    top of each target's own idempotent `deliver()`.

    `id` and `created_at` are never touched on an update — only the columns
    that can change between two delivery attempts (`state`, `remote_ref`,
    `url`, `bytes`, `error`, `delivered_at`) are in the `DO UPDATE SET`
    clause, so the row keeps its original identity and creation time
    across every re-delivery.

    **`result.message` is the only place a failure reason is stored, and a
    target's own `DeliveryResult.message` never carries a token** — every
    target in this package reads its secret from the environment and never
    echoes it back (APP-CONTRACT 10.2); this function does not additionally
    redact anything, because there is nothing here to redact.
    """
    from narratarr.db import new_id, now, transaction

    state = "delivered" if result.ok else "failed"
    stamp = now()
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO deliveries
                (id, job_id, target_id, state, remote_ref, url, bytes, error, created_at, delivered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id, target_id) DO UPDATE SET
                state = excluded.state,
                remote_ref = excluded.remote_ref,
                url = excluded.url,
                bytes = excluded.bytes,
                error = excluded.error,
                delivered_at = excluded.delivered_at
            """,
            (
                new_id(),
                job_id,
                target_id,
                state,
                result.remote_ref,
                result.url,
                result.bytes,
                None if result.ok else result.message,
                stamp,
                stamp if result.ok else None,
            ),
        )


def _deliver_to_every_target(
    job, method_name: str, progress: Callable[[Progress], None] | None
) -> list[DeliveryResult]:
    """Shared body of `deliver_job` and `deliver_job_fix`: build the book
    once, call `method_name` (`"deliver"` or `"deliver_fix"`) on every
    enabled target, and upsert one `deliveries` row per target.

    **One target failing must not stop the others.** Every target call is
    wrapped in its own `try`/`except`; a raised exception becomes a failed
    `DeliveryResult` for that target only, and the loop continues. A
    result is returned, and a `deliveries` row is written, for every
    enabled target — never fewer, whether that target succeeded or not.
    """
    from narratarr.db import connect

    try:
        book = _build_pipeline(job).deliver_book()
    except Exception as exc:
        raise TargetError(f"could not build the finished book for delivery: {exc}") from exc

    conn = connect()
    try:
        rows = _enabled_target_rows(conn)
    finally:
        conn.close()

    kinds = registry()
    results: list[DeliveryResult] = []

    for row in rows:
        target_kind = row["kind"]
        target_config: dict = json.loads(row["config"] or "{}")

        target_instance = kinds.get(target_kind)
        if target_instance is None:
            result = DeliveryResult(ok=False, message=f"'{target_kind}' is not a target kind Narratarr knows")
        else:
            method = getattr(target_instance, method_name)
            try:
                result = method(target_config, book, progress=progress)
            except Exception as exc:  # noqa: BLE001 - one target's fault must not stop the others
                result = DeliveryResult(ok=False, message=str(exc))

        results.append(result)
        _upsert_delivery(job.id, row["id"], result)

    return results


def deliver_job(job, *, progress: Callable[[Progress], None] | None = None) -> list[DeliveryResult]:
    """Deliver one finished book to every enabled target. Return one result
    per target. APP-CONTRACT section 8.3.

    Idempotent: a second call re-builds the identical `DeliverBook`, calls
    each target's own idempotent `deliver()` again (a folder target
    overwrites the same destination with the same bytes; the Audiobookshelf
    target re-copies, re-scans, and re-verifies), and upserts the same
    `deliveries` row rather than inserting a second one.
    """
    return _deliver_to_every_target(job, "deliver", progress)


def deliver_job_fix(job, *, progress: Callable[[Progress], None] | None = None) -> list[DeliveryResult]:
    """Re-deliver one job to every enabled target after a post-delivery
    correction. APP-CONTRACT section 9.5's Fix flow: the runner re-renders
    only the stale chunks, re-binds, and then calls this — not
    `deliver_job` — so every target's `deliver_fix()` (never merely
    `deliver()`) gets the chance to do anything a small correction needs
    that a first delivery does not.

    Every target in this package today has an identical `deliver_fix` and
    `deliver` (each delivery is already a complete, atomic, idempotent
    overwrite — see `FolderTarget.deliver_fix`'s and
    `AudiobookshelfTarget.deliver_fix`'s own docstrings), but the runner
    calling the *named* Fix method, not `deliver`, is what keeps this
    working correctly the day a target needs to do something different for
    a correction than for a first delivery.
    """
    return _deliver_to_every_target(job, "deliver_fix", progress)
