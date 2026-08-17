"""The job queue.

APP-CONTRACT.md section 5.2 rule 1 says one book renders at a time. This
module gives the runner one atomic way to pick the next job, and one way to
recover a job that a kill left mid-render.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from narratarr.db import connect, now
from narratarr.models import Job


def claim_next_job(conn: Optional[sqlite3.Connection] = None) -> Optional[Job]:
    """Claim the next queued job, and return it. Return None when none waits.

    The claim is one atomic UPDATE: an `UPDATE ... WHERE id = (SELECT ...)`
    against the row of highest `priority`, then oldest `created_at`. A
    second caller that runs this at the same moment finds no queued row
    left to claim, so two runners never claim the same job.

    The claim never touches `stage`. A fresh job already holds `stage`
    NULL, and a job a kill left mid-render keeps the stage it was on, so
    the runner resumes there instead of restarting at `extract`.
    """
    owns_conn = conn is None
    conn = conn or connect()
    try:
        stamp = now()
        cur = conn.execute(
            """
            UPDATE jobs
            SET state = 'running',
                started_at = COALESCE(started_at, ?),
                updated_at = ?
            WHERE id = (
                SELECT id FROM jobs
                WHERE state = 'queued'
                ORDER BY priority DESC, created_at ASC
                LIMIT 1
            )
            RETURNING *
            """,
            (stamp, stamp),
        )
        row = cur.fetchone()
        conn.commit()
        return Job.from_row(row) if row is not None else None
    finally:
        if owns_conn:
            conn.close()


def requeue_stale_running_jobs(conn: Optional[sqlite3.Connection] = None) -> int:
    """Set every `running` job back to `queued`. Return the count changed.

    Call this once, when the runner starts. APP-CONTRACT.md section 5.2
    rule 2: every stage is idempotent, so a job that a kill left `running`
    resumes safely at its first missing or stale artifact once it is
    `queued` again. `stage` is left as it was, so the runner resumes at the
    same stage rather than restarting from `extract`.
    """
    owns_conn = conn is None
    conn = conn or connect()
    try:
        stamp = now()
        cur = conn.execute(
            """
            UPDATE jobs
            SET state = 'queued', updated_at = ?
            WHERE state = 'running'
            """,
            (stamp,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        if owns_conn:
            conn.close()


def count_queued(conn: Optional[sqlite3.Connection] = None) -> int:
    """Return the count of jobs waiting in state `queued`."""
    owns_conn = conn is None
    conn = conn or connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE state = 'queued'"
        ).fetchone()
        return int(row["n"])
    finally:
        if owns_conn:
            conn.close()
