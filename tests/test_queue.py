"""Tests for narratarr/queue.py.

APP-CONTRACT.md section 5.2 rule 1: one book renders at a time. The claim
is one atomic UPDATE, so a second runner never claims the same job.
"""

from __future__ import annotations

from narratarr import db as db_module
from narratarr.queue import claim_next_job, count_queued, requeue_stale_running_jobs


def test_claim_next_job_returns_none_when_empty(db):
    """claim_next_job() returns None when no job is queued."""
    assert claim_next_job() is None


def test_claim_next_job_picks_highest_priority(db, make_job):
    """The claim honors priority DESC, then created_at ASC."""
    make_job(slug="low", priority=0, created_at="20260101T000000Z")
    high_id = make_job(slug="high", priority=10, created_at="20260101T000000Z")

    claimed = claim_next_job()
    assert claimed is not None
    assert claimed.id == high_id
    assert claimed.state == "running"


def test_claim_next_job_picks_oldest_of_equal_priority(db, make_job):
    """Two jobs of equal priority: the older created_at claims first."""
    older_id = make_job(slug="older", priority=0, created_at="20260101T000000Z")
    make_job(slug="newer", priority=0, created_at="20260101T010000Z")

    claimed = claim_next_job()
    assert claimed.id == older_id


def test_claim_next_job_is_atomic_and_does_not_double_claim(db, make_job):
    """Two sequential claims never return the same job.

    The claim is a single `UPDATE ... WHERE id = (SELECT ...)`, so once one
    caller's UPDATE commits, a second caller's SELECT subquery no longer
    sees the row in state 'queued'. This test claims twice in a row and
    checks the two results are distinct jobs, which is the observable
    guarantee even without real thread concurrency.
    """
    first_id = make_job(slug="one")
    second_id = make_job(slug="two")

    first = claim_next_job()
    second = claim_next_job()

    assert {first.id, second.id} == {first_id, second_id}
    assert first.id != second.id
    assert claim_next_job() is None


def test_claim_next_job_leaves_stage_untouched(db, make_job):
    """A claim never resets `stage`, so a resumed job keeps its progress.

    This is the bug the crash-recovery guarantee depends on: a job a kill
    left mid-render must resume AT its stage, not restart from `extract`.
    """
    job_id = make_job(slug="resumed", stage="render", state="queued")
    claimed = claim_next_job()
    assert claimed.id == job_id
    assert claimed.stage == "render"


def test_claim_next_job_ignores_non_queued_states(db, make_job):
    """A job in `running`, `done`, or any other state is never claimed."""
    make_job(slug="already-running", state="running")
    make_job(slug="finished", state="done")
    assert claim_next_job() is None


def test_requeue_stale_running_jobs(db, make_job):
    """Every `running` job returns to `queued`. A `queued` job is untouched."""
    running_id = make_job(slug="was-running", state="running", stage="render")
    queued_id = make_job(slug="already-queued", state="queued")
    done_id = make_job(slug="finished", state="done")

    count = requeue_stale_running_jobs()
    assert count == 1

    conn = db_module.connect()
    try:
        rows = {
            row["id"]: row["state"]
            for row in conn.execute("SELECT id, state FROM jobs").fetchall()
        }
    finally:
        conn.close()

    assert rows[running_id] == "queued"
    assert rows[queued_id] == "queued"
    assert rows[done_id] == "done"


def test_requeue_preserves_stage(db, make_job):
    """A requeued job keeps its stage, so the runner resumes there."""
    job_id = make_job(slug="mid-render", state="running", stage="render")
    requeue_stale_running_jobs()

    conn = db_module.connect()
    try:
        row = conn.execute("SELECT stage FROM jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        conn.close()
    assert row["stage"] == "render"


def test_count_queued(db, make_job):
    """count_queued() counts only jobs in state 'queued'."""
    make_job(slug="a", state="queued")
    make_job(slug="b", state="queued")
    make_job(slug="c", state="running")
    assert count_queued() == 2
