"""Tests for narratarr/runner.py.

APP-CONTRACT.md section 15.2: a test never loads a model and never renders
audio. Every test below injects a fake pipeline, a fake deliverer, and a
fake engine preflight through the seams `process_job()` documents. None of
them imports `narratarr.adapter`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest

from narratarr import db as db_module
from narratarr import runner
from narratarr.models import Job


# ------------------------------------------------------------------- fakes


@dataclass(frozen=True)
class FakeStageResult:
    """A stand-in for adapter.StageResult. Refer to APP-CONTRACT.md section 6."""

    stage: str
    done: int = 1
    skipped: int = 0
    failed: int = 0
    aborted: bool = False
    abort_reason: Optional[str] = None
    detail: dict = field(default_factory=dict)


class FakePipeline:
    """A stand-in for adapter.Pipeline. Never touches a model or real audio."""

    def __init__(
        self,
        workspace: Path,
        slug: str,
        source: Path,
        book_config: dict,
        qc_config: dict,
        *,
        stage_results: Optional[dict] = None,
        qc_pending: Optional[list] = None,
        homograph_unresolved: Optional[list] = None,
        on_run_stage: Optional[dict] = None,
        artifacts_result: Optional[dict] = None,
        status_result: Optional[dict] = None,
        prune_chapters=None,
    ) -> None:
        self.workspace = workspace
        self.slug = slug
        self.source = source
        self.book_config = book_config
        self.qc_config = qc_config
        self.calls: list[str] = []
        self.stage_results = stage_results or {}
        self.qc_pending = qc_pending or []
        self.homograph_unresolved = homograph_unresolved or []
        self.on_run_stage = on_run_stage or {}
        self.artifacts_result = artifacts_result if artifacts_result is not None else {}
        self.status_result = status_result if status_result is not None else {}
        if prune_chapters is not None:
            self.prune_chapters = prune_chapters

    def run_stage(self, stage, chapters=None, force=False, progress=None):
        self.calls.append(stage)
        if progress is not None:
            progress(SimpleNamespace(stage=stage, done=1, total=1, message=""))
        hook = self.on_run_stage.get(stage)
        if hook is not None:
            hook()
        return self.stage_results.get(stage, FakeStageResult(stage=stage))

    def status(self):
        return self.status_result

    def render_sample(self, chapter=None, seconds=90.0):
        self.calls.append("render_sample")
        return self.workspace / self.slug / "samples" / "sample.wav"

    def homograph_audit(self, write=False, llm=True):
        self.calls.append("homograph_audit")
        return {"unresolved_class_a": self.homograph_unresolved}

    def homograph_candidates(self, chapter, chunk, word, occurrence):
        return []

    def qc_report(self):
        self.calls.append("qc_report")
        return {"chunks": self.qc_pending}

    def accept_chunk(self, chapter, chunk, reason):
        pass

    def rerender_chunk(self, chapter, chunk):
        return {}

    def artifacts(self):
        return self.artifacts_result

    def chunk_audio_path(self, chapter, chunk):
        return None


def _pipeline_factory(**kwargs):
    """Return a pipeline_factory that always hands back one FakePipeline."""
    holder: dict = {}

    def _factory(workspace, slug, source, book_config, qc_config):
        pipeline = FakePipeline(workspace, slug, source, book_config, qc_config, **kwargs)
        holder["pipeline"] = pipeline
        return pipeline

    _factory.holder = holder
    return _factory


def _good_preflight(engine, voice, lang_code):
    return {
        "espeak_fallback": True,
        "warmup_samples": 100,
        "warmup_sample_rate": 24000,
        "oov_probe_word": "zzznotaword",
        "oov_probe_nonempty": True,
    }


def _bad_preflight(engine, voice, lang_code):
    return {
        "espeak_fallback": False,
        "warmup_samples": 0,
        "warmup_sample_rate": 24000,
        "oov_probe_word": "zzznotaword",
        "oov_probe_nonempty": False,
    }


def _ok_delivery(job):
    return [SimpleNamespace(ok=True, remote_ref="ref-1", url="http://x/1", bytes=100, message="")]


def _failing_delivery(job):
    return [SimpleNamespace(ok=False, remote_ref=None, url=None, bytes=0, message="boom")]


def _disable_sample_gate(monkeypatch) -> None:
    """Turn off the sample gate for a test that is not about the gate itself.

    APP-CONTRACT.md section 9.1: the sample gate is ON by default, and
    `tests/conftest.py`'s `narratarr_env` fixture matches that default. A
    test whose own point is the full stage walk, the event log, delivery,
    or pruning would otherwise stop at `awaiting_sample_approval` before
    it ever reaches what it means to check. The gate's own behaviour is
    covered on its own by `test_sample_gate_opens_and_stops_the_runner`,
    `test_sample_gate_disabled_skips_straight_through`, and
    `test_resume_after_sample_approved_skips_render_sample`.
    """
    from narratarr.config import get_settings

    monkeypatch.setenv("NARRATARR_SAMPLE_GATE", "false")
    get_settings.cache_clear()


def _load_job(job_id: str) -> Job:
    conn = db_module.connect()
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        conn.close()
    return Job.from_row(row)


# --------------------------------------------------------------- the walk


def test_process_job_walks_every_stage_to_done(db, make_job, monkeypatch):
    """A job with no gate hit and every target delivered ends `done`."""
    _disable_sample_gate(monkeypatch)
    job_id = make_job(state="running", stage=None, source_path="/config/library/book.epub")
    job = _load_job(job_id)
    factory = _pipeline_factory()

    result = runner.process_job(
        job,
        pipeline_factory=factory,
        deliver_targets=_ok_delivery,
        engine_preflight=_good_preflight,
    )

    assert result.state == "done"
    pipeline = factory.holder["pipeline"]
    # "homograph_audit" and "qc_report" are calls the runner makes in
    # addition to run_stage(), to check whether a gate must open.
    assert pipeline.calls == [
        "extract", "normalize", "chunk", "homograph_audit",
        "render", "qc", "qc_report", "assemble", "bind",
    ]


def test_process_job_writes_events_for_every_stage(db, make_job, monkeypatch):
    """The runner writes an event for every state change. Section 5.2 rule 5."""
    _disable_sample_gate(monkeypatch)
    job_id = make_job(state="running")
    job = _load_job(job_id)
    runner.process_job(
        job, pipeline_factory=_pipeline_factory(),
        deliver_targets=_ok_delivery, engine_preflight=_good_preflight,
    )

    conn = db_module.connect()
    try:
        rows = conn.execute(
            "SELECT stage, message FROM events WHERE job_id = ? ORDER BY id ASC", (job_id,)
        ).fetchall()
    finally:
        conn.close()
    messages = [row["message"] for row in rows]
    assert any("job" in m and "claimed" in m for m in messages)
    assert any("stage bind finished" in m for m in messages)
    assert any("delivery finished" in m for m in messages)


def test_process_job_reports_progress(db, make_job):
    """A stage's progress callback writes into jobs.progress_done/total."""
    job_id = make_job(state="running")
    job = _load_job(job_id)
    runner.process_job(
        job, pipeline_factory=_pipeline_factory(),
        deliver_targets=_ok_delivery, engine_preflight=_good_preflight,
    )

    conn = db_module.connect()
    try:
        row = conn.execute(
            "SELECT progress_done, progress_total FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row["progress_done"] == 1
    assert row["progress_total"] == 1


# ------------------------------------------------------------------ gates


def test_sample_gate_opens_and_stops_the_runner(db, make_job):
    """The sample gate opens, the job is freed, and the runner moves on."""
    job_id = make_job(state="running")
    job = _load_job(job_id)
    factory = _pipeline_factory()

    result = runner.process_job(
        job, pipeline_factory=factory,
        deliver_targets=_ok_delivery, engine_preflight=_good_preflight,
    )

    assert result.state == "awaiting_sample_approval"
    assert result.stage == "sample"
    assert "render" not in factory.holder["pipeline"].calls

    conn = db_module.connect()
    try:
        gate = conn.execute(
            "SELECT * FROM gates WHERE job_id = ? AND kind = 'sample'", (job_id,)
        ).fetchone()
    finally:
        conn.close()
    assert gate is not None
    assert gate["state"] == "open"


def test_sample_gate_disabled_skips_straight_through(db, make_job, monkeypatch):
    """NARRATARR_SAMPLE_GATE=false never opens a sample gate."""
    from narratarr.config import get_settings

    monkeypatch.setenv("NARRATARR_SAMPLE_GATE", "false")
    get_settings.cache_clear()

    job_id = make_job(state="running")
    job = _load_job(job_id)
    factory = _pipeline_factory()
    result = runner.process_job(
        job, pipeline_factory=factory,
        deliver_targets=_ok_delivery, engine_preflight=_good_preflight,
    )
    assert result.state == "done"
    assert "render_sample" not in factory.holder["pipeline"].calls
    get_settings.cache_clear()


def test_resume_after_sample_approved_skips_render_sample(db, make_job):
    """A resolved, approved sample gate is not asked again on resume."""
    job_id = make_job(state="running", stage="sample")
    conn = db_module.connect()
    try:
        conn.execute(
            "INSERT INTO gates (id, job_id, kind, state, payload, open_items, "
            "created_at, resolved_at, resolution) VALUES "
            "('g1', ?, 'sample', 'resolved', '{}', 0, '20260101T000000Z', "
            "'20260101T000100Z', 'approved')",
            (job_id,),
        )
        conn.commit()
    finally:
        conn.close()

    job = _load_job(job_id)
    factory = _pipeline_factory()
    result = runner.process_job(
        job, pipeline_factory=factory,
        deliver_targets=_ok_delivery, engine_preflight=_good_preflight,
    )
    assert result.state == "done"
    assert "render_sample" not in factory.holder["pipeline"].calls


def test_homograph_gate_opens_with_review_items(db, make_job):
    """An unresolved class A homograph opens a gate with one review item each."""
    job_id = make_job(state="running", stage="homographs")
    job = _load_job(job_id)
    factory = _pipeline_factory(
        homograph_unresolved=[
            {"chapter": "ch01", "chunk": "0001", "word": "wound", "occurrence": 1,
             "context": "he wound the clock", "candidates": [{"reading": "verb"}]},
        ]
    )
    result = runner.process_job(
        job, pipeline_factory=factory,
        deliver_targets=_ok_delivery, engine_preflight=_good_preflight,
    )
    assert result.state == "awaiting_homograph_review"

    conn = db_module.connect()
    try:
        items = conn.execute(
            "SELECT * FROM review_items WHERE job_id = ? AND kind = 'homograph_occurrence'",
            (job_id,),
        ).fetchall()
    finally:
        conn.close()
    assert len(items) == 1
    assert items[0]["word"] == "wound"


def test_qc_gate_opens_with_review_items(db, make_job):
    """A needs_human chunk opens the QC gate with one review item."""
    job_id = make_job(state="running", stage="qc")
    job = _load_job(job_id)
    factory = _pipeline_factory(
        qc_pending=[
            {"chapter": "ch01", "chunk": "0007", "status": "needs_human",
             "source_text": "a", "transcript": "b", "wer": 0.4, "coverage": 0.9,
             "duration_s": 3.2, "flags": ["low_coverage"], "wav_sha256": "abc"},
        ]
    )
    result = runner.process_job(
        job, pipeline_factory=factory,
        deliver_targets=_ok_delivery, engine_preflight=_good_preflight,
    )
    assert result.state == "awaiting_qc_review"

    conn = db_module.connect()
    try:
        items = conn.execute(
            "SELECT * FROM review_items WHERE job_id = ? AND kind = 'qc_chunk'", (job_id,)
        ).fetchall()
        gate = conn.execute(
            "SELECT * FROM gates WHERE job_id = ? AND kind = 'qc'", (job_id,)
        ).fetchone()
    finally:
        conn.close()
    assert len(items) == 1
    assert items[0]["chunk"] == "0007"
    assert gate["open_items"] == 1


# ------------------------------------------------------------- espeak hazard


def test_preflight_failure_refuses_to_render(db, make_job):
    """A bad preflight report fails the job before run_stage('render') is called."""
    job_id = make_job(state="running", stage="render")
    job = _load_job(job_id)
    factory = _pipeline_factory()

    result = runner.process_job(
        job, pipeline_factory=factory,
        deliver_targets=_ok_delivery, engine_preflight=_bad_preflight,
    )

    assert result.state == "failed"
    assert "render" not in factory.holder["pipeline"].calls
    assert "preflight" in (result.error or "").lower()


def test_render_log_espeak_warning_fails_job_even_after_good_preflight(db, make_job):
    """The secondary log-grep check still fires when the log holds the warning.

    This simulates the exact hazard the overlord's P0 measurement found:
    the engine object looks fine at preflight time, but the render log
    still ends up holding the warning (for example, mid-render).
    """
    from narratarr.config import get_settings

    settings = get_settings()
    job_id = make_job(state="running", stage="render", slug="hazard-book")
    job = _load_job(job_id)

    logs_dir = settings.work_dir / "hazard-book" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "render-20260101T000000Z.log").write_text(
        "WARNING:root:EspeakFallback not Enabled: OOD words will be skipped\n"
    )

    factory = _pipeline_factory()
    result = runner.process_job(
        job, pipeline_factory=factory,
        deliver_targets=_ok_delivery, engine_preflight=_good_preflight,
    )

    assert result.state == "failed"
    assert "espeak" in (result.error or "").lower() or "EspeakFallback" in (result.error or "")
    # The preflight passed, so run_stage("render") DID run; the secondary
    # check catches the warning only after that call returns.
    assert "render" in factory.holder["pipeline"].calls


def test_clean_render_log_does_not_fail_the_job(db, make_job):
    """No warning in the render log: the job proceeds past render."""
    job_id = make_job(state="running", stage="render")
    job = _load_job(job_id)
    factory = _pipeline_factory()
    result = runner.process_job(
        job, pipeline_factory=factory,
        deliver_targets=_ok_delivery, engine_preflight=_good_preflight,
    )
    assert result.state == "done"


# --------------------------------------------------------- crash recovery


def test_on_start_requeues_running_jobs(db, make_job):
    """runner.on_start() sets every running job back to queued."""
    job_id = make_job(state="running", stage="render")
    runner.on_start(engine_preflight=_good_preflight)

    conn = db_module.connect()
    try:
        row = conn.execute("SELECT state, stage FROM jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        conn.close()
    assert row["state"] == "queued"
    assert row["stage"] == "render"  # resumed AT render, not restarted


def test_kill_and_restart_resumes_at_the_same_stage(db, make_job):
    """A job a kill left `running` at `render` resumes there, not at `extract`.

    This is the P1 exit criterion APP-CONTRACT.md section 5.2 rule 2
    describes: "a kill is safe" because every stage is idempotent and the
    runner resumes at the stage it was on.
    """
    job_id = make_job(state="running", stage="render")

    runner.on_start(engine_preflight=_good_preflight)  # simulates the restart

    from narratarr.queue import claim_next_job

    claimed = claim_next_job()
    assert claimed.id == job_id
    assert claimed.stage == "render"  # the claim itself never resets stage

    factory = _pipeline_factory()
    result = runner.process_job(
        claimed, pipeline_factory=factory,
        deliver_targets=_ok_delivery, engine_preflight=_good_preflight,
    )
    assert result.state == "done"
    # Resumed AT render: extract, normalize, and chunk never ran again.
    # "qc_report" is qc_report(), called separately from run_stage("qc") to
    # check for a needs_human chunk.
    assert factory.holder["pipeline"].calls == ["render", "qc", "qc_report", "assemble", "bind"]


# --------------------------------------------------------------- delivery


def test_delivery_failure_fails_the_job(db, make_job, monkeypatch):
    """A target that reports ok=False fails the job."""
    _disable_sample_gate(monkeypatch)
    job_id = make_job(state="running")
    job = _load_job(job_id)
    result = runner.process_job(
        job, pipeline_factory=_pipeline_factory(),
        deliver_targets=_failing_delivery, engine_preflight=_good_preflight,
    )
    assert result.state == "failed"
    assert "target" in (result.error or "").lower()


# ------------------------------------------------------------------- prune


def test_prune_skipped_when_setting_is_off(db, make_job, monkeypatch):
    """NARRATARR_PRUNE=false (the default): prune_chapters is never called."""
    from narratarr.config import get_settings

    monkeypatch.setenv("NARRATARR_PRUNE", "false")
    _disable_sample_gate(monkeypatch)

    calls = []
    job_id = make_job(state="running")
    job = _load_job(job_id)
    factory = _pipeline_factory(prune_chapters=lambda: calls.append("pruned"))
    runner.process_job(
        job, pipeline_factory=factory,
        deliver_targets=_ok_delivery, engine_preflight=_good_preflight,
    )
    assert calls == []
    get_settings.cache_clear()


def test_prune_runs_when_on_done_and_review_empty(db, make_job, monkeypatch):
    """NARRATARR_PRUNE=true, job done, no open review: prune_chapters runs."""
    from narratarr.config import get_settings

    monkeypatch.setenv("NARRATARR_PRUNE", "true")
    _disable_sample_gate(monkeypatch)

    calls = []
    job_id = make_job(state="running")
    job = _load_job(job_id)
    factory = _pipeline_factory(prune_chapters=lambda: calls.append("pruned"))
    result = runner.process_job(
        job, pipeline_factory=factory,
        deliver_targets=_ok_delivery, engine_preflight=_good_preflight,
    )
    assert result.state == "done"
    assert calls == ["pruned"]
    get_settings.cache_clear()


def test_prune_never_runs_with_an_open_review_item(db, make_job, monkeypatch):
    """Pipeline CONTRACT.md 15.1: never prune while a fix is pending."""
    from narratarr.config import get_settings

    monkeypatch.setenv("NARRATARR_PRUNE", "true")
    _disable_sample_gate(monkeypatch)

    calls = []
    job_id = make_job(state="running")
    conn = db_module.connect()
    try:
        conn.execute(
            "INSERT INTO gates (id, job_id, kind, state, created_at) "
            "VALUES ('g1', ?, 'qc', 'resolved', '20260101T000000Z')",
            (job_id,),
        )
        conn.execute(
            "INSERT INTO review_items (id, job_id, gate_id, kind, chapter, "
            "flags, state, created_at) VALUES "
            "('r1', ?, 'g1', 'qc_chunk', 'ch01', '[]', 'open', '20260101T000000Z')",
            (job_id,),
        )
        conn.commit()
    finally:
        conn.close()

    job = _load_job(job_id)
    factory = _pipeline_factory(prune_chapters=lambda: calls.append("pruned"))
    runner.process_job(
        job, pipeline_factory=factory,
        deliver_targets=_ok_delivery, engine_preflight=_good_preflight,
    )
    assert calls == []
    get_settings.cache_clear()


# --------------------------------------------------------- pause and cancel


def test_pause_between_stages_stops_the_walk(db, make_job):
    """A person pausing the job mid-run stops the walk before the next stage.

    The adapter gives no interrupt token for a stage in progress (refer to
    the runner module docstring), so this test pauses the job from inside
    the fake pipeline's `extract` call, and checks the walk never reaches
    `normalize`.
    """
    job_id = make_job(state="running")

    def _pause_now():
        conn = db_module.connect()
        try:
            conn.execute("UPDATE jobs SET state = 'paused' WHERE id = ?", (job_id,))
            conn.commit()
        finally:
            conn.close()

    job = _load_job(job_id)
    factory = _pipeline_factory(on_run_stage={"extract": _pause_now})
    result = runner.process_job(
        job, pipeline_factory=factory,
        deliver_targets=_ok_delivery, engine_preflight=_good_preflight,
    )
    assert result.state == "paused"
    assert factory.holder["pipeline"].calls == ["extract"]


# -------------------------------------------------------------- the cap


def test_events_are_capped_per_job(db, make_job, monkeypatch):
    """write_event() deletes the oldest rows past events_per_job_max."""
    from narratarr.config import get_settings

    monkeypatch.setenv("NARRATARR_EVENTS_PER_JOB_MAX", "5")
    get_settings.cache_clear()

    job_id = make_job(state="running")
    conn = db_module.connect()
    try:
        for i in range(10):
            runner.write_event(conn, job_id, "info", f"event {i}")
        conn.commit()
        rows = conn.execute(
            "SELECT message FROM events WHERE job_id = ? ORDER BY id ASC", (job_id,)
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 5
    assert [r["message"] for r in rows] == [f"event {i}" for i in range(5, 10)]
    get_settings.cache_clear()


def test_run_once_processes_a_single_job(db, make_job, monkeypatch):
    """run_once() claims and fully processes exactly one queued job."""
    _disable_sample_gate(monkeypatch)
    make_job(slug="only-one")
    claimed = runner.run_once(
        pipeline_factory=_pipeline_factory(),
        deliver_targets=_ok_delivery,
        engine_preflight=_good_preflight,
    )
    assert claimed is True
    # No job is queued anymore (the one job is now `done`), so a second
    # call claims nothing. Default seams are safe here: claim_next_job()
    # returns None before any pipeline_factory would ever be invoked.
    assert runner.run_once() is False

    conn = db_module.connect()
    try:
        row = conn.execute("SELECT state FROM jobs WHERE slug = 'only-one'").fetchone()
    finally:
        conn.close()
    assert row["state"] == "done"
