"""Shared test fixtures.

APP-CONTRACT.md section 15 rule 2: a test never loads a model and never
renders real audio. Every fixture here works against a temporary directory
and a temporary sqlite database; nothing here touches `/config`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# So `import narratarr` resolves when pytest is run from any directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from narratarr.config import get_settings  # noqa: E402
from narratarr import db as db_module  # noqa: E402
from narratarr.db import new_id, now  # noqa: E402

TEST_API_KEY = "test-key-0123456789abcdef0123456789abcdef"


@pytest.fixture
def narratarr_env(tmp_path, monkeypatch):
    """Point every `NARRATARR_*` path at a fresh temp directory.

    Clears the `get_settings()` cache before and after, so this test's
    settings never leak into another test.
    """
    monkeypatch.setenv("NARRATARR_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("NARRATARR_OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("NARRATARR_WATCH_DIR", str(tmp_path / "watch"))
    monkeypatch.setenv("NARRATARR_API_KEY", "")
    monkeypatch.setenv("NARRATARR_SAMPLE_GATE", "true")
    monkeypatch.setenv("NARRATARR_PRUNE", "false")
    monkeypatch.setenv("NARRATARR_EVENTS_PER_JOB_MAX", "5000")
    get_settings.cache_clear()
    settings = get_settings()
    settings.ensure_directories()
    yield settings
    get_settings.cache_clear()


@pytest.fixture
def db(narratarr_env):
    """Apply the schema to a fresh temp database. Yields the settings object."""
    db_module.init_db()
    yield narratarr_env


@pytest.fixture
def make_job(db):
    """Return a factory that inserts a minimal job row. Returns the job id.

    Every column not given a keyword argument takes a sensible default, so
    a test names only the fields it cares about.
    """

    def _make(**overrides) -> str:
        conn = db_module.connect()
        try:
            job_id = overrides.pop("id", new_id())
            stamp = now()
            defaults = {
                "slug": f"book-{job_id[:8]}",
                "title": "Test Book",
                "author": "Test Author",
                "year": "2020",
                "genre": "Fiction",
                "language": "en",
                "source_path": "/config/library/book.epub",
                "source_sha256": "0" * 64,
                "cover_path": None,
                "state": "queued",
                "stage": None,
                "worker": "local",
                "priority": 0,
                "progress_done": 0,
                "progress_total": 0,
                "error": None,
                "book_config": "{}",
                "qc_config": "{}",
                "created_at": stamp,
                "updated_at": stamp,
                "started_at": None,
                "finished_at": None,
            }
            defaults.update(overrides)
            columns = ", ".join(defaults.keys())
            placeholders = ", ".join("?" for _ in defaults)
            conn.execute(
                f"INSERT INTO jobs (id, {columns}) VALUES (?, {placeholders})",
                (job_id, *defaults.values()),
            )
            conn.commit()
        finally:
            conn.close()
        return job_id

    return _make


@pytest.fixture
def client(narratarr_env, monkeypatch):
    """A `TestClient` with the runner thread disabled and a known API key.

    The route handlers run exactly as they do in production; only the
    background thread that claims and processes jobs on its own schedule
    is skipped, so a test controls exactly when a job is processed (by
    calling `narratarr.runner` functions directly).
    """
    from fastapi.testclient import TestClient

    monkeypatch.setenv("NARRATARR_API_KEY", TEST_API_KEY)
    monkeypatch.setenv("NARRATARR_TEST_DISABLE_RUNNER", "1")
    get_settings.cache_clear()

    from narratarr.api import create_app

    app = create_app()
    with TestClient(app) as test_client:
        test_client.headers.update({"X-Api-Key": TEST_API_KEY})
        yield test_client
