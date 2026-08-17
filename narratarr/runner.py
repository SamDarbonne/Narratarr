"""The in-process job runner.

APP-CONTRACT.md section 5 defines the job state machine. Section 5.1 orders
the stages. Section 5.2 gives the five rules this module obeys. This module
calls the pipeline only through the adapter of APP-CONTRACT.md section 6,
the `Pipeline` class W2 builds. **This module never imports `abpipe`.**

Every function that needs the real pipeline imports it lazily, inside the
function body, through an injected factory. A test passes a fake object
instead, so a test never loads a model and never renders audio. Refer to
APP-CONTRACT.md section 15.2.

## The espeak fallback hazard, and the three defences

APP-CONTRACT.md section 11.2 and pipeline CONTRACT.md section 17.1 record a
silent-data-loss fault: when the espeak fallback fails to construct, misaki
is built with `unk=""`, and every out-of-lexicon word is deleted from the
render, silently. QC cannot see the loss, because the transcript and the
source both lose the same word.

**The overlord's P0 measurement changed the plan.** The `kokoro` package logs
its own warning through loguru, and `kokoro/__init__.py` calls
`logger.disable("kokoro")` at import. A log grep alone therefore finds
nothing, even on a broken fallback: the warning is silenced before it is
ever written. Three defences now apply, in this priority order:

1. **`_run_preflight_check` is the primary defence.** It calls the engine's
   `preflight()` report through the adapter, and refuses to start a render
   when the report says the fallback is absent or the probe word rendered
   empty audio. This check reads the engine object directly, so a disabled
   logger cannot hide the fault from it.
2. **`_enable_kokoro_logger` is the belt.** It turns the `kokoro` package's
   logger back on at startup, so the log channel works too.
3. **`_render_logs_have_espeak_warning` is the secondary check**, kept for
   the `mlx-audio` path and any future engine that uses the standard
   library logger instead of loguru.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from narratarr.config import get_settings
from narratarr.db import connect, new_id, now, transaction
from narratarr.models import Job
from narratarr.queue import claim_next_job, requeue_stale_running_jobs

logger = logging.getLogger("narratarr.runner")

# The stage walk, in the order of APP-CONTRACT.md section 5.1.
STAGE_ORDER = [
    "extract",
    "normalize",
    "chunk",
    "sample",
    "homographs",
    "render",
    "qc",
    "assemble",
    "bind",
    "deliver",
]

# The abpipe stages that go through the adapter's run_stage(). Refer to
# abpipe CONTRACT.md section 1, and APP-CONTRACT.md section 3.1: Narratarr
# runs the pipeline through stage 7 (bind) only. "sample", "homographs", and
# "deliver" are Narratarr's own steps, and each gets its own handler below.
PIPELINE_STAGES = {"extract", "normalize", "chunk", "render", "qc", "assemble", "bind"}

# APP-CONTRACT.md section 11.2: the secondary detection line. Refer to the
# module docstring for why this check alone is not enough.
ESPEAK_WARNING = "EspeakFallback not Enabled"


# --------------------------------------------------------------- the seams
#
# Three dependencies are injected, never imported at module scope. A test
# supplies a fake for each one. Production code takes the default, which
# imports the real thing lazily, inside the function body.


def _default_pipeline_factory(
    workspace: Path, slug: str, source: Path, book_config: dict, qc_config: dict
) -> Any:
    """Build the real Pipeline. Import the adapter lazily.

    This is the seam of APP-CONTRACT.md section 6. W2 owns
    `narratarr/adapter/__init__.py` and the `Pipeline` class inside it.
    """
    from narratarr.adapter import Pipeline  # local import: see the module docstring

    return Pipeline(
        workspace, slug, source, _with_deployment_engine(book_config), qc_config
    )


def _with_deployment_engine(book_config: dict) -> dict:
    """Return `book_config` with this deployment's engine filled in.

    Warning: the pipeline's own default engine is `kokoro_mlx`, which runs
    on Apple silicon only. A job created from a watch-folder drop carries an
    empty book config, so without this the container reached the sample
    stage and failed with `No module named 'mlx'`. That was measured on the
    first real end-to-end run, not imagined.

    The deployment decides the engine, through `NARRATARR_ENGINE`,
    `NARRATARR_VOICE`, and `NARRATARR_LANG_CODE`. A book that names its own
    engine keeps it: an explicit per-book choice always wins over a default.

    **Only the keys that are absent are filled.** `engine.describe()` is
    hashed into stage 4's `config_hash`, so overwriting a value a book
    already holds would stale every rendered file of that book.
    """
    settings = get_settings()
    merged = dict(book_config or {})
    engine = dict(merged.get("engine") or {})
    engine.setdefault("name", settings.engine)
    engine.setdefault("voice", settings.voice)
    engine.setdefault("lang_code", settings.lang_code)
    if settings.num_threads:
        engine.setdefault("num_threads", settings.num_threads)
    merged["engine"] = engine
    return merged


PipelineFactory = Callable[[Path, str, Path, dict, dict], Any]


def _default_deliver_targets(job: Job) -> list:
    """Deliver the finished book to every enabled target. Import lazily.

    ASSUMPTION, flagged for the overlord: APP-CONTRACT.md section 8
    documents the per-target `Target` protocol (`validate`, `test`,
    `deliver`, `deliver_fix`), but no job-level "run every enabled target"
    entry point. This function assumes W2 exposes
    `narratarr.adapter.targets.deliver_job(job) -> list[DeliveryResult]`.
    Confirm the real name and signature with W2, the same way the Pipeline
    factory above must match W2's real class.
    """
    from narratarr.adapter.targets import deliver_job  # local import: see above

    return deliver_job(job)


DeliverTargets = Callable[[Job], list]


def _pipeline_for_job(
    job: Job, pipeline_factory: PipelineFactory = _default_pipeline_factory
) -> Any:
    """Build a Pipeline for an existing job, from its stored configuration."""
    settings = get_settings()
    book_config = json.loads(job.book_config or "{}")
    qc_config = json.loads(job.qc_config or "{}")
    return pipeline_factory(
        settings.work_dir, job.slug, Path(job.source_path), book_config, qc_config
    )


def get_pipeline_status(
    job: Job, pipeline_factory: PipelineFactory = _default_pipeline_factory
) -> dict:
    """Return the per-stage fresh, stale, and absent count of a job.

    Read-only: `Pipeline.status()` inspects meta files on disk and never
    renders. `api/jobs.py` calls this instead of importing the adapter
    itself, per APP-CONTRACT.md section 3. Returns `{}` when the pipeline
    cannot be built yet (for example, before the adapter exists, or before
    `extract` has run), rather than raising into the API layer.
    """
    try:
        pipeline = _pipeline_for_job(job, pipeline_factory)
        return pipeline.status() or {}
    except Exception as exc:  # noqa: BLE001 - a read-only status call must not 500
        logger.info("could not read pipeline status for %s: %s", job.slug, exc)
        return {}


def get_pipeline_artifacts(
    job: Job, pipeline_factory: PipelineFactory = _default_pipeline_factory
) -> dict:
    """Return the artifact paths and sizes of a job. Read-only; never renders.

    Refer to `get_pipeline_status` for the empty-dict-on-fault convention.
    """
    try:
        pipeline = _pipeline_for_job(job, pipeline_factory)
        return pipeline.artifacts() or {}
    except Exception as exc:  # noqa: BLE001
        logger.info("could not read pipeline artifacts for %s: %s", job.slug, exc)
        return {}


def _default_engine_preflight(engine: str, voice: str, lang_code: str) -> dict:
    """Check the TTS engine construction is safe. Import the adapter lazily.

    Calls `narratarr.adapter.preflight_engine(engine, voice, lang_code)`,
    and returns the report dict the overlord specified:
    `{"espeak_fallback": bool, "warmup_samples": int,
    "warmup_sample_rate": int, "oov_probe_word": str,
    "oov_probe_nonempty": bool}`. Confirmed against W2's real seam
    2026-08-16: the name and signature match, and every failure mode
    comes back as `PipelineError`, so the single `except Exception` clause
    around every call site of this function covers all of them.
    """
    from narratarr.adapter import preflight_engine  # local import: see above

    return preflight_engine(engine, voice, lang_code)


EnginePreflight = Callable[[str, str, str], dict]


def _set_torch_threads(num_threads: int) -> None:
    """Cap torch's CPU thread count once, at process start-up.

    Ruling from the overlord, 2026-08-16: `torch.set_num_threads()` is
    process-global, so it is set once, here, rather than threaded through
    every adapter call. `NARRATARR_NUM_THREADS` (default 3) matches the
    `cpus: 3` cgroup cap of APP-CONTRACT.md section 11.3: torch otherwise
    defaults to one thread per core and fights that cap. W2's
    `preflight_engine()` and the engine's `describe()` deliberately carry
    no thread count, because `describe()` is hashed into stage 4's
    `config_hash` (pipeline CONTRACT.md section 3.2); a thread count there
    would stale every already-rendered file for a setting that changes
    speed, not audio. A missing `torch` package is not an error: the API
    layer must still start on a machine with no pipeline extra installed.
    """
    try:
        import torch
    except ImportError:
        logger.info(
            "torch is not installed; skipping the thread cap (NARRATARR_NUM_THREADS=%d)",
            num_threads,
        )
        return
    torch.set_num_threads(num_threads)
    logger.info("torch thread count set to %d (NARRATARR_NUM_THREADS)", num_threads)


def _enable_kokoro_logger() -> None:
    """Turn the `kokoro` package's own loguru logger back on.

    `kokoro/__init__.py` calls `logger.disable("kokoro")` at import, so the
    package's own "EspeakFallback not Enabled" warning is silenced before
    it is ever written. This call is the belt to `_run_preflight_check`'s
    braces: it does nothing to protect a render by itself, but it makes the
    secondary log-grep check meaningful again. A missing `loguru` package
    (this venv installs no heavy adapter dependency) is not an error.
    """
    try:
        from loguru import logger as loguru_logger
    except ImportError:
        return
    loguru_logger.enable("kokoro")


# ---------------------------------------------------------- preflight cache

_LAST_PREFLIGHT: Optional[dict] = None
_PREFLIGHT_LOCK = threading.Lock()


def _record_preflight(report: dict, job_slug: Optional[str]) -> None:
    """Cache the most recent engine preflight report."""
    global _LAST_PREFLIGHT
    with _PREFLIGHT_LOCK:
        _LAST_PREFLIGHT = {**report, "job_slug": job_slug, "checked_at": now()}


def get_last_preflight() -> Optional[dict]:
    """Return the most recent engine preflight report, or None when none ran.

    `api/system.py` calls this to fill `GET /system/status`, instead of
    importing the adapter itself. This keeps the rule of APP-CONTRACT.md
    section 3: no module under `narratarr/api/` imports `abpipe`.
    """
    with _PREFLIGHT_LOCK:
        return dict(_LAST_PREFLIGHT) if _LAST_PREFLIGHT is not None else None


def _resolve_engine_config(job: Job, settings) -> tuple[str, str, str]:
    """Return (engine, voice, lang_code) for a job.

    A book config may override the engine (abpipe CONTRACT.md section 4.1).
    The job's own `book_config` wins; the global setting is the default.
    """
    book_config = json.loads(job.book_config or "{}")
    engine_cfg = book_config.get("engine", {}) if isinstance(book_config, dict) else {}
    return (
        engine_cfg.get("name", settings.engine),
        engine_cfg.get("voice", settings.voice),
        engine_cfg.get("lang_code", settings.lang_code),
    )


# ------------------------------------------------------------------ events


def write_event(
    conn: sqlite3.Connection,
    job_id: Optional[str],
    level: str,
    message: str,
    stage: Optional[str] = None,
    data: Optional[dict] = None,
) -> None:
    """Write one event row, then enforce the per-job cap.

    APP-CONTRACT.md section 4.5: the events table is capped at
    `events_per_job_max` rows for each job, default 5000. A render of 2,000
    chunks would otherwise grow the database without a bound.
    """
    conn.execute(
        """
        INSERT INTO events (job_id, level, stage, message, data, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (job_id, level, stage, message, json.dumps(data) if data is not None else None, now()),
    )
    if job_id is not None:
        _cap_events(conn, job_id)


def _cap_events(conn: sqlite3.Connection, job_id: str) -> None:
    """Delete the oldest events of a job past `events_per_job_max`."""
    limit = get_settings().events_per_job_max
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE job_id = ?", (job_id,)
    ).fetchone()
    over = row["n"] - limit
    if over > 0:
        conn.execute(
            """
            DELETE FROM events WHERE id IN (
                SELECT id FROM events WHERE job_id = ? ORDER BY id ASC LIMIT ?
            )
            """,
            (job_id, over),
        )


# -------------------------------------------------------------- job state


def _set_job_stage(conn: sqlite3.Connection, job: Job, stage: Optional[str]) -> None:
    """Write the job's current stage, and update the in-memory copy."""
    conn.execute(
        "UPDATE jobs SET stage = ?, updated_at = ? WHERE id = ?", (stage, now(), job.id)
    )
    job.stage = stage


def _set_job_state(
    conn: sqlite3.Connection,
    job: Job,
    state: str,
    *,
    error: Optional[str] = None,
    finished: bool = False,
) -> None:
    """Write the job's state. A finished state also stamps `finished_at`."""
    stamp = now()
    if finished:
        conn.execute(
            "UPDATE jobs SET state = ?, error = ?, updated_at = ?, finished_at = ? WHERE id = ?",
            (state, error, stamp, stamp, job.id),
        )
        job.finished_at = stamp
    else:
        conn.execute(
            "UPDATE jobs SET state = ?, error = ?, updated_at = ? WHERE id = ?",
            (state, error, stamp, job.id),
        )
    job.state = state
    job.error = error


def _fail_job(conn: sqlite3.Connection, job: Job, reason: str) -> None:
    """Mark the job `failed`. Write the reason as an event and as `jobs.error`."""
    _set_job_state(conn, job, "failed", error=reason, finished=True)
    write_event(conn, job.id, "error", reason, stage=job.stage)


def _make_progress_callback(conn: sqlite3.Connection, job: Job) -> Callable[[Any], None]:
    """Return a callback that writes an adapter Progress into the job row.

    `progress_total` of 0 means unknown. APP-CONTRACT.md section 4.2: the
    user interface then shows an indeterminate bar, never a false
    percentage.
    """

    def _callback(progress: Any) -> None:
        conn.execute(
            "UPDATE jobs SET progress_done = ?, progress_total = ?, updated_at = ? WHERE id = ?",
            (progress.done, progress.total, now(), job.id),
        )
        conn.commit()
        job.progress_done = progress.done
        job.progress_total = progress.total

    return _callback


# ------------------------------------------------------------------- gates


def _open_gate(conn: sqlite3.Connection, job: Job, kind: str, payload: dict) -> str:
    """Insert one open gate row. Return its id."""
    gate_id = new_id()
    conn.execute(
        """
        INSERT INTO gates (id, job_id, kind, state, payload, open_items, created_at)
        VALUES (?, ?, ?, 'open', ?, 0, ?)
        """,
        (gate_id, job.id, kind, json.dumps(payload), now()),
    )
    return gate_id


def _has_approved_gate(conn: sqlite3.Connection, job: Job, kind: str) -> bool:
    """Return True when the job's most recent resolved gate of this kind was approved."""
    row = conn.execute(
        """
        SELECT resolution FROM gates
        WHERE job_id = ? AND kind = ? AND state = 'resolved'
        ORDER BY created_at DESC LIMIT 1
        """,
        (job.id, kind),
    ).fetchone()
    return bool(row) and row["resolution"] == "approved"


def _add_review_item(conn: sqlite3.Connection, job: Job, gate_id: str, item: dict) -> None:
    """Insert one `review_items` row. `item` supplies the table's own fields."""
    conn.execute(
        """
        INSERT INTO review_items (
            id, job_id, gate_id, kind, chapter, chunk, word, occurrence,
            source_text, transcript, context, wer, coverage, duration_s,
            flags, wav_sha256, candidates, state, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
        """,
        (
            new_id(),
            job.id,
            gate_id,
            item.get("kind"),
            item.get("chapter"),
            item.get("chunk"),
            item.get("word"),
            item.get("occurrence"),
            item.get("source_text"),
            item.get("transcript"),
            item.get("context"),
            item.get("wer"),
            item.get("coverage"),
            item.get("duration_s"),
            json.dumps(item.get("flags", [])),
            item.get("wav_sha256"),
            json.dumps(item["candidates"]) if item.get("candidates") is not None else None,
            now(),
        ),
    )


# ----------------------------------------------------- the espeak defences


def _render_logs_have_espeak_warning(workspace: Path, slug: str) -> bool:
    """Return True when a render log of this book holds the espeak warning.

    SECONDARY CHECK. Refer to the module docstring: the `kokoro` package
    disables its own logger, so this check catches only the `mlx-audio`
    path (stdlib logging) or a future engine. `_run_preflight_check` is the
    check that must not be skipped.

    Grep every render log, not only the last one. Pipeline CONTRACT.md
    section 17.1: a failed attempt writes its own log, and the warning can
    sit there while the successful run is clean.
    """
    logs_dir = workspace / slug / "logs"
    if not logs_dir.is_dir():
        return False
    for log_path in sorted(logs_dir.glob("render-*.log")):
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if ESPEAK_WARNING in text:
            return True
    return False


def _run_preflight_check(
    conn: sqlite3.Connection, job: Job, settings, engine_preflight: EnginePreflight
) -> bool:
    """Refuse to render when the engine's own preflight report is bad.

    PRIMARY CHECK. Reads the engine object directly (through the adapter),
    so a disabled logger cannot hide a broken espeak fallback from it.
    Returns False, and marks the job failed, when the report is missing
    the fallback or the out-of-lexicon probe rendered empty audio.
    """
    engine, voice, lang_code = _resolve_engine_config(job, settings)
    try:
        report = engine_preflight(engine, voice, lang_code)
    except Exception as exc:  # noqa: BLE001 - any construction fault refuses the render
        _fail_job(
            conn, job,
            f"engine preflight raised: {exc}. Refusing to render. "
            "Refer to APP-CONTRACT.md section 11.2.",
        )
        return False

    _record_preflight(report, job.slug)
    ok = bool(report.get("espeak_fallback")) and bool(report.get("oov_probe_nonempty"))
    if not ok:
        write_event(
            conn, job.id, "error",
            "PREFLIGHT FAILED: the espeak fallback is absent, or the out-of-lexicon "
            "probe rendered empty audio. Every out-of-lexicon word may be silently "
            "deleted from this render. Refusing to start. Refer to APP-CONTRACT.md "
            "section 11.2.",
            stage="render", data=report,
        )
        _fail_job(conn, job, "engine preflight failed; refer to the preflight event for the report")
        return False

    write_event(conn, job.id, "info", "engine preflight passed", stage="render", data=report)
    return True


# ------------------------------------------------------------ stage runners


def _run_pipeline_stage(
    conn: sqlite3.Connection,
    job: Job,
    pipeline: Any,
    stage: str,
    settings,
    engine_preflight: EnginePreflight,
) -> bool:
    """Run one abpipe stage through the adapter. Return False on a stop.

    A stop means the runner already set the job to `failed` (or, for `qc`,
    opened a gate) and the caller must return to the outer loop.
    """
    if stage == "render":
        if not _run_preflight_check(conn, job, settings, engine_preflight):
            conn.commit()
            return False

    progress_cb = _make_progress_callback(conn, job)
    try:
        result = pipeline.run_stage(stage, progress=progress_cb)
    except Exception as exc:  # noqa: BLE001 - PipelineError or any other fault
        _fail_job(conn, job, f"stage {stage} raised: {exc}")
        conn.commit()
        return False

    write_event(
        conn, job.id, "info", f"stage {stage} finished", stage=stage,
        data={"done": result.done, "skipped": result.skipped, "failed": result.failed},
    )

    if stage == "render" and _render_logs_have_espeak_warning(settings.work_dir, job.slug):
        # SECONDARY CHECK. The primary preflight check above already passed,
        # so this fires only for an engine that still logs the warning
        # through a channel `_enable_kokoro_logger` does not touch, or a
        # fallback that broke mid-render after preflight passed.
        _fail_job(
            conn, job,
            "the render log holds 'EspeakFallback not Enabled': every out-of-lexicon "
            "word may have been deleted from the audio. Refer to APP-CONTRACT.md "
            "section 11.2.",
        )
        conn.commit()
        return False

    if result.aborted or result.failed:
        reason = result.abort_reason or f"stage {stage} reported {result.failed} failure(s)"
        _fail_job(conn, job, reason)
        conn.commit()
        return False

    if stage == "qc":
        return _check_qc_gate(conn, job, pipeline)

    conn.commit()
    return True


def _check_qc_gate(conn: sqlite3.Connection, job: Job, pipeline: Any) -> bool:
    """Open the QC gate when a chunk needs a person. Return False on a stop."""
    report = pipeline.qc_report() or {}
    pending = [c for c in report.get("chunks", []) if c.get("status") == "needs_human"]
    if not pending:
        conn.commit()
        return True

    gate_id = _open_gate(conn, job, "qc", {"pending": len(pending)})
    for chunk in pending:
        _add_review_item(
            conn, job, gate_id,
            {
                "kind": "qc_chunk",
                "chapter": chunk.get("chapter"),
                "chunk": chunk.get("chunk"),
                "source_text": chunk.get("source_text"),
                "transcript": chunk.get("transcript"),
                "wer": chunk.get("wer"),
                "coverage": chunk.get("coverage"),
                "duration_s": chunk.get("duration_s"),
                "flags": chunk.get("flags", []),
                "wav_sha256": chunk.get("wav_sha256"),
            },
        )
    conn.execute("UPDATE gates SET open_items = ? WHERE id = ?", (len(pending), gate_id))
    _set_job_state(conn, job, "awaiting_qc_review")
    write_event(conn, job.id, "info", f"QC gate opened, {len(pending)} chunk(s) need a person", stage="qc")
    conn.commit()
    return False


def _run_sample_stage(conn: sqlite3.Connection, job: Job, pipeline: Any, settings) -> bool:
    """Render the hazard sample, and gate it, unless already approved.

    APP-CONTRACT.md section 9.1: the sample gate is ON by default. A
    resumed job whose most recent sample gate was approved skips straight
    through, so approval is not asked twice for the same render.
    """
    if not settings.sample_gate:
        conn.commit()
        return True
    if _has_approved_gate(conn, job, "sample"):
        conn.commit()
        return True

    try:
        wav_path = pipeline.render_sample()
    except Exception as exc:  # noqa: BLE001
        _fail_job(conn, job, f"sample render raised: {exc}")
        conn.commit()
        return False

    gate_id = _open_gate(conn, job, "sample", {"wav_path": str(wav_path)})
    conn.execute("UPDATE gates SET open_items = 1 WHERE id = ?", (gate_id,))
    _set_job_state(conn, job, "awaiting_sample_approval")
    write_event(conn, job.id, "info", "sample gate opened", stage="sample")
    conn.commit()
    return False


def _run_homograph_stage(conn: sqlite3.Connection, job: Job, pipeline: Any) -> bool:
    """Run the homograph audit, and gate an unresolved class A disagreement."""
    try:
        audit = pipeline.homograph_audit(write=True) or {}
    except Exception as exc:  # noqa: BLE001
        _fail_job(conn, job, f"homograph audit raised: {exc}")
        conn.commit()
        return False

    unresolved = audit.get("unresolved_class_a", [])
    if not unresolved:
        conn.commit()
        return True

    gate_id = _open_gate(conn, job, "homograph", {"count": len(unresolved)})
    for occ in unresolved:
        _add_review_item(
            conn, job, gate_id,
            {
                "kind": "homograph_occurrence",
                "chapter": occ.get("chapter"),
                "chunk": occ.get("chunk"),
                "word": occ.get("word"),
                "occurrence": occ.get("occurrence"),
                "context": occ.get("context"),
                "candidates": occ.get("candidates"),
            },
        )
    conn.execute("UPDATE gates SET open_items = ? WHERE id = ?", (len(unresolved), gate_id))
    _set_job_state(conn, job, "awaiting_homograph_review")
    write_event(
        conn, job.id, "info", f"homograph gate opened, {len(unresolved)} occurrence(s)",
        stage="homographs",
    )
    conn.commit()
    return False


def _run_deliver_stage(
    conn: sqlite3.Connection, job: Job, pipeline: Any, deliver_targets: DeliverTargets, settings
) -> None:
    """Deliver to every enabled target, then mark the job done or failed."""
    _set_job_state(conn, job, "delivering")
    write_event(conn, job.id, "info", "delivery started", stage="deliver")
    conn.commit()

    try:
        results = deliver_targets(job) or []
    except Exception as exc:  # noqa: BLE001
        _fail_job(conn, job, f"delivery raised: {exc}")
        conn.commit()
        return

    all_ok = all(getattr(r, "ok", False) for r in results) if results else True
    if all_ok:
        _set_job_state(conn, job, "done", finished=True)
        write_event(conn, job.id, "info", "delivery finished, job done", stage="deliver")
        _maybe_prune(conn, job, pipeline, settings)
    else:
        failed_count = sum(1 for r in results if not getattr(r, "ok", False))
        _fail_job(conn, job, f"{failed_count} target(s) failed delivery")
    conn.commit()


def _maybe_prune(conn: sqlite3.Connection, job: Job, pipeline: Any, settings) -> None:
    """Prune the finished job's intermediate audio, when every condition holds.

    APP-CONTRACT.md section 5.2 rule 4, and pipeline CONTRACT.md section
    15.1: prune only when the job is `done`, its review queue is empty, and
    `NARRATARR_PRUNE` is on. A pruned chapter turns a one-chunk fix into a
    whole-chapter re-render, so this function stays conservative.

    ASSUMPTION, flagged for the overlord: APP-CONTRACT.md section 6 lists
    no `prune` method on `Pipeline`, though pipeline CONTRACT.md section 15
    documents `prune_chapter(ctx, chapter_id, dry_run)`. This function
    calls `pipeline.prune_chapters()` when the adapter provides it, and
    otherwise writes a warning event and skips. Ask W2 to add the method,
    or confirm its real name.
    """
    if not settings.prune or job.state != "done":
        return
    open_review = conn.execute(
        "SELECT COUNT(*) AS n FROM review_items WHERE job_id = ? AND state = 'open'",
        (job.id,),
    ).fetchone()["n"]
    if open_review:
        return

    prune_fn = getattr(pipeline, "prune_chapters", None)
    if prune_fn is None:
        write_event(
            conn, job.id, "warning",
            "NARRATARR_PRUNE is on, but the adapter gives no prune method; skipping",
            stage="deliver",
        )
        return
    prune_fn()


# --------------------------------------------------------------- the walk


def process_job(
    job: Job,
    conn: Optional[sqlite3.Connection] = None,
    pipeline_factory: PipelineFactory = _default_pipeline_factory,
    deliver_targets: DeliverTargets = _default_deliver_targets,
    engine_preflight: EnginePreflight = _default_engine_preflight,
) -> Job:
    """Run one claimed job through the stage walk of APP-CONTRACT.md 5.1.

    `job` must already be `running` (`claim_next_job` leaves it that way).
    This function returns once the job reaches a resting state: a gate
    (`awaiting_*`), `delivering`/`done`, or `failed`. It never blocks on a
    person: a gate stops the job and returns control to the caller, which
    moves on to the next job.
    """
    owns_conn = conn is None
    conn = conn or connect()
    settings = get_settings()
    try:
        write_event(conn, job.id, "info", f"job {job.slug} claimed", stage=job.stage)
        conn.commit()

        book_config = json.loads(job.book_config or "{}")
        qc_config = json.loads(job.qc_config or "{}")
        try:
            pipeline = pipeline_factory(
                settings.work_dir, job.slug, Path(job.source_path), book_config, qc_config
            )
        except Exception as exc:  # noqa: BLE001 - a fault here fails the job
            _fail_job(conn, job, f"could not start the pipeline: {exc}")
            conn.commit()
            return job

        start_index = STAGE_ORDER.index(job.stage) if job.stage in STAGE_ORDER else 0

        for stage in STAGE_ORDER[start_index:]:
            # Cooperative pause and cancel. A person may change this job's
            # state through the API while the previous stage ran. The
            # adapter of APP-CONTRACT.md section 6 gives no interrupt token
            # for a stage already in progress, so this check acts only
            # BETWEEN stages, never inside one.
            row = conn.execute("SELECT state FROM jobs WHERE id = ?", (job.id,)).fetchone()
            if row is not None and row["state"] != "running":
                job.state = row["state"]
                write_event(
                    conn, job.id, "info",
                    f"stopping before stage {stage}: job state is now {row['state']}",
                    stage=job.stage,
                )
                conn.commit()
                return job

            _set_job_stage(conn, job, stage)
            write_event(conn, job.id, "info", f"stage {stage} started", stage=stage)
            conn.commit()

            if stage in PIPELINE_STAGES:
                if not _run_pipeline_stage(conn, job, pipeline, stage, settings, engine_preflight):
                    return job
            elif stage == "sample":
                if not _run_sample_stage(conn, job, pipeline, settings):
                    return job
            elif stage == "homographs":
                if not _run_homograph_stage(conn, job, pipeline):
                    return job
            elif stage == "deliver":
                _run_deliver_stage(conn, job, pipeline, deliver_targets, settings)
                return job
            else:  # pragma: no cover - STAGE_ORDER is closed
                raise AssertionError(f"unhandled stage {stage}")

        return job  # pragma: no cover - STAGE_ORDER always ends in "deliver"
    finally:
        if owns_conn:
            conn.close()


# ---------------------------------------------------------------- the loop


def on_start(engine_preflight: EnginePreflight = _default_engine_preflight) -> int:
    """Recover from a kill, and warm the preflight cache. Call this once.

    APP-CONTRACT.md section 5.2 rule 2: on start, set every `running` job
    back to `queued`. Every stage is idempotent, so the job resumes at its
    first missing or stale artifact. This is what makes "survives
    kill-and-restart mid-render" true.

    Also caps the torch thread count, runs the belt-and-braces logger fix,
    and runs one preflight check against the default engine, so
    `GET /system/status` shows a fresh report even before the first job
    runs.
    """
    settings = get_settings()
    _set_torch_threads(settings.num_threads)
    _enable_kokoro_logger()

    try:
        report = engine_preflight(settings.engine, settings.voice, settings.lang_code)
        _record_preflight(report, job_slug=None)
    except Exception as exc:  # noqa: BLE001 - a missing adapter must not block startup
        logger.warning("startup engine preflight did not run: %s", exc)

    with transaction() as conn:
        count = requeue_stale_running_jobs(conn)
        if count:
            write_event(conn, None, "info", f"{count} job(s) requeued after restart")
    return count


def run_once(
    pipeline_factory: PipelineFactory = _default_pipeline_factory,
    deliver_targets: DeliverTargets = _default_deliver_targets,
    engine_preflight: EnginePreflight = _default_engine_preflight,
) -> bool:
    """Claim and process one job. Return True when a job was claimed."""
    conn = connect()
    try:
        job = claim_next_job(conn)
    finally:
        conn.close()
    if job is None:
        return False

    process_job(
        job,
        pipeline_factory=pipeline_factory,
        deliver_targets=deliver_targets,
        engine_preflight=engine_preflight,
    )
    return True


# ------------------------------------------------------------ the watch folder
#
# APP-CONTRACT section 7. W2 built the poller and W3 built the route that
# asks for a scan. Nothing called either of them: an EPUB dropped in /watch
# sat there for ever and the queue stayed empty. This is the seam that joins
# them, and it is the P1 exit criterion.

_WATCH: dict = {"folder": None, "last_poll": 0.0}


def _watch_folder():
    """Return the one WatchFolder, made on first use."""
    if _WATCH["folder"] is None:
        from narratarr.adapter.ingest import WatchFolder

        _WATCH["folder"] = WatchFolder(get_settings().watch_dir)
    return _WATCH["folder"]


def scan_watch_folder() -> list[str]:
    """Ingest every stable new file in the watch folder. Return the new job ids.

    A file is ingested only when its size is the same on two consecutive
    polls. A half-copied EPUB is the obvious fault here, and the poller owns
    that rule. Refer to APP-CONTRACT section 7.

    The file is copied into `/config/library` and the job renders from the
    copy. Narratarr never renders from the watch folder, because the watch
    folder belongs to the user and a file there can move under a running
    render.
    """
    from narratarr.adapter.ingest import ingest_file

    settings = get_settings()
    new_ids: list[str] = []
    try:
        found = _watch_folder().poll()
    except Exception as exc:  # a bad mount must not kill the runner
        logger.warning("the watch folder could not be read: %s", exc)
        return new_ids

    for path in found:
        try:
            with transaction() as conn:
                slugs = [r["slug"] for r in conn.execute("SELECT slug FROM jobs")]
                hashes = [
                    r["source_sha256"] for r in conn.execute("SELECT source_sha256 FROM jobs")
                ]
                result = ingest_file(path, settings.library_dir, slugs, hashes)

                job_id = new_id()
                stamp = now()
                unsupported = path.suffix.lower() != ".epub"
                error = f"unsupported extension: {path.suffix or '(none)'}"
                conn.execute(
                    """
                    INSERT INTO jobs (
                        id, slug, title, source_path, source_sha256, state, stage,
                        worker, priority, progress_done, progress_total, error,
                        book_config, qc_config, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, 'local', 0, 0, 0, ?, '{}', '{}', ?, ?)
                    """,
                    (
                        job_id, result.slug, path.stem, str(result.path),
                        result.source_sha256,
                        "failed" if unsupported else "queued",
                        error if unsupported else None,
                        stamp, stamp,
                    ),
                )
                write_event(conn, job_id, "info", f"ingested {path.name} from the watch folder")
                if unsupported:
                    write_event(conn, job_id, "error", error)
            new_ids.append(job_id)
            logger.info("ingested %s as job %s", path.name, result.slug)
        except Exception as exc:
            # A duplicate, a DRM-protected file, or an unreadable file must
            # not stop the poller from seeing the next book.
            logger.warning("could not ingest %s: %s", path.name, exc)
    return new_ids


def _scan_requested() -> bool:
    """Return True when the scan route asked for a scan, and clear the flag."""
    try:
        with transaction() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = '_watch_scan_requested_at'"
            ).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM settings WHERE key = '_watch_scan_requested_at'")
            return True
    except Exception:
        return False


def run_forever(
    stop_event: Optional[threading.Event] = None,
    poll_interval_s: float = 1.0,
    pipeline_factory: PipelineFactory = _default_pipeline_factory,
    deliver_targets: DeliverTargets = _default_deliver_targets,
    engine_preflight: EnginePreflight = _default_engine_preflight,
) -> None:
    """Run the claim-and-process loop until `stop_event` is set.

    Call `on_start()` once before this function, at process start-up, so a
    job a kill left `running` returns to the queue first. APP-CONTRACT.md
    section 5.2 rule 1: one book renders at a time, so this loop holds one
    in-process worker.
    """
    stop_event = stop_event or threading.Event()
    interval = float(get_settings().watch_interval_s)
    while not stop_event.is_set():
        # Poll the watch folder on its own schedule, and whenever the scan
        # route has asked for one. The API never does this work itself.
        if time.monotonic() - _WATCH["last_poll"] >= interval or _scan_requested():
            _WATCH["last_poll"] = time.monotonic()
            scan_watch_folder()
        claimed = run_once(
            pipeline_factory=pipeline_factory,
            deliver_targets=deliver_targets,
            engine_preflight=engine_preflight,
        )
        if not claimed:
            stop_event.wait(poll_interval_s)
