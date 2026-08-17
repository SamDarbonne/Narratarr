"""Tests for `narratarr/api/review.py`.

A test here never loads a model and never renders audio. Every WAV file
below is a handful of made-up bytes; only the HTTP layer is under test.

These tests build their own FastAPI app and their own database, instead of
using `tests/conftest.py` (owned by W1), because the fixture names of that
file are not part of the documented seam of APP-CONTRACT.md section 14.1.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

# --------------------------------------------------------------------- setup


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    """Return a `TestClient` wired to a fresh, empty database.

    Sets `NARRATARR_CONFIG_DIR` to an isolated temp directory, clears the
    `config.get_settings` cache, builds the schema, mounts `review.router`,
    and overrides `require_key` so no real auth module needs to exist yet.
    """
    monkeypatch.setenv("NARRATARR_CONFIG_DIR", str(tmp_path))

    from narratarr import config

    config.get_settings.cache_clear()

    from narratarr import db

    db.init_db()

    from narratarr.api import review
    from narratarr.api.common import ApiError, require_key

    app = FastAPI()
    app.include_router(review.router)

    class _FakeApiKey:
        id = "test-key-id"
        name = "test-key"

    def _fake_require_key():
        return _FakeApiKey()

    app.dependency_overrides[require_key] = _fake_require_key

    @app.exception_handler(ApiError)
    def _handle_api_error(request, exc: ApiError):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=getattr(exc, "status", 400),
            content={
                "error": {
                    "code": getattr(exc, "code", "error"),
                    "message": getattr(exc, "message", str(exc)),
                    "detail": getattr(exc, "detail", None) or {},
                }
            },
        )

    client = TestClient(app)
    client.settings = config.get_settings()
    yield client
    config.get_settings.cache_clear()


# ------------------------------------------------------------------- helpers


def _make_job(**overrides) -> dict:
    from narratarr import db

    stamp = db.now()
    job = {
        "id": db.new_id(),
        "slug": overrides.pop("slug", "book-a"),
        "title": "Book A",
        "author": "Someone",
        "year": "1907",
        "genre": "fiction",
        "language": "en",
        "source_path": "/config/library/book-a.epub",
        "source_sha256": "a" * 64,
        "cover_path": None,
        "state": "awaiting_qc_review",
        "stage": "qc",
        "worker": "local",
        "priority": 0,
        "progress_done": 0,
        "progress_total": 0,
        "error": None,
        "book_config": "{}",
        "qc_config": "{}",
        "created_at": stamp,
        "updated_at": stamp,
        "started_at": stamp,
        "finished_at": None,
    }
    job.update(overrides)
    conn = db.connect()
    try:
        conn.execute(
            """
            INSERT INTO jobs (id, slug, title, author, year, genre, language,
                source_path, source_sha256, cover_path, state, stage, worker,
                priority, progress_done, progress_total, error, book_config,
                qc_config, created_at, updated_at, started_at, finished_at)
            VALUES (:id, :slug, :title, :author, :year, :genre, :language,
                :source_path, :source_sha256, :cover_path, :state, :stage, :worker,
                :priority, :progress_done, :progress_total, :error, :book_config,
                :qc_config, :created_at, :updated_at, :started_at, :finished_at)
            """,
            job,
        )
        conn.commit()
    finally:
        conn.close()
    return job


def _make_gate(job_id: str, **overrides) -> dict:
    from narratarr import db

    gate = {
        "id": db.new_id(),
        "job_id": job_id,
        "kind": "qc",
        "state": "open",
        "payload": "{}",
        "open_items": 1,
        "created_at": db.now(),
        "resolved_at": None,
        "resolved_by": None,
        "resolution": None,
        "reason": None,
    }
    gate.update(overrides)
    conn = db.connect()
    try:
        conn.execute(
            """
            INSERT INTO gates (id, job_id, kind, state, payload, open_items,
                created_at, resolved_at, resolved_by, resolution, reason)
            VALUES (:id, :job_id, :kind, :state, :payload, :open_items,
                :created_at, :resolved_at, :resolved_by, :resolution, :reason)
            """,
            gate,
        )
        conn.commit()
    finally:
        conn.close()
    return gate


def _make_review_item(job_id: str, gate_id: str, **overrides) -> dict:
    from narratarr import db

    item = {
        "id": db.new_id(),
        "job_id": job_id,
        "gate_id": gate_id,
        "kind": "qc_chunk",
        "chapter": "ch01",
        "chunk": "0001",
        "word": None,
        "occurrence": None,
        "source_text": "the cat sat on the mat",
        "transcript": "the cat sat on that mat",
        "context": None,
        "wer": 0.16,
        "coverage": 1.0,
        "duration_s": 2.4,
        "flags": "[]",
        "wav_sha256": "b" * 64,
        "candidates": None,
        "state": "open",
        "resolution": None,
        "reason": None,
        "resolved_at": None,
        "created_at": db.now(),
    }
    item.update(overrides)
    conn = db.connect()
    try:
        conn.execute(
            """
            INSERT INTO review_items (id, job_id, gate_id, kind, chapter, chunk,
                word, occurrence, source_text, transcript, context, wer,
                coverage, duration_s, flags, wav_sha256, candidates, state,
                resolution, reason, resolved_at, created_at)
            VALUES (:id, :job_id, :gate_id, :kind, :chapter, :chunk,
                :word, :occurrence, :source_text, :transcript, :context, :wer,
                :coverage, :duration_s, :flags, :wav_sha256, :candidates, :state,
                :resolution, :reason, :resolved_at, :created_at)
            """,
            item,
        )
        conn.commit()
    finally:
        conn.close()
    return item


def _chunk_audio_path(client, job: dict, item: dict):
    return (
        client.settings.work_dir
        / job["slug"]
        / "04-audio"
        / item["chapter"]
        / f"{item['chunk']}.wav"
    )


def _write_fake_wav(path, payload: bytes = b"RIFF____WAVEfmt fake-audio-bytes-0123456789") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


HEADERS = {"X-Api-Key": "test-key"}


# ----------------------------------------------------------------------- gates


def test_list_gates_returns_open_gates_across_several_jobs(app_client):
    job_a = _make_job(slug="book-a")
    job_b = _make_job(slug="book-b")
    gate_a = _make_gate(job_a["id"], kind="qc")
    gate_b = _make_gate(job_b["id"], kind="homograph")
    # A resolved gate must not appear in the queue.
    _make_gate(job_a["id"], kind="sample", state="resolved")

    resp = app_client.get("/api/v1/gates", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    ids = {row["id"] for row in body["items"]}
    assert ids == {gate_a["id"], gate_b["id"]}
    assert body["total"] == 2
    # The queue names the job, the way a servarr app's queue does.
    slugs = {row["job_slug"] for row in body["items"]}
    assert slugs == {"book-a", "book-b"}


def test_get_gate_unknown_id_is_404(app_client):
    resp = app_client.get("/api/v1/gates/does-not-exist", headers=HEADERS)
    assert resp.status_code == 404


def test_get_gate_includes_its_review_items(app_client):
    job = _make_job()
    gate = _make_gate(job["id"])
    item = _make_review_item(job["id"], gate["id"])

    resp = app_client.get(f"/api/v1/gates/{gate['id']}", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert [row["id"] for row in body["items"]] == [item["id"]]


def test_resolve_gate_requeues_the_job_and_returns_202(app_client):
    job = _make_job(state="awaiting_sample_approval")
    gate = _make_gate(job["id"], kind="sample", open_items=0)

    resp = app_client.post(
        f"/api/v1/gates/{gate['id']}/resolve",
        headers=HEADERS,
        json={"resolution": "approved", "reason": "The sample sounds correct."},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["state"] == "resolved"
    assert body["resolution"] == "approved"

    from narratarr import db

    conn = db.connect()
    try:
        row = conn.execute("SELECT state FROM jobs WHERE id = ?", (job["id"],)).fetchone()
    finally:
        conn.close()
    assert row["state"] == "queued"


def test_resolve_gate_twice_is_409(app_client):
    job = _make_job()
    gate = _make_gate(job["id"], kind="sample")

    body = {"resolution": "approved", "reason": "ok"}
    first = app_client.post(f"/api/v1/gates/{gate['id']}/resolve", headers=HEADERS, json=body)
    assert first.status_code == 202

    second = app_client.post(f"/api/v1/gates/{gate['id']}/resolve", headers=HEADERS, json=body)
    assert second.status_code == 409


# ------------------------------------------------------------------- accept


def test_accept_empty_reason_is_422(app_client):
    job = _make_job()
    gate = _make_gate(job["id"])
    item = _make_review_item(job["id"], gate["id"])

    resp = app_client.post(
        f"/api/v1/review/items/{item['id']}/accept", headers=HEADERS, json={"reason": ""}
    )
    assert resp.status_code == 422


def test_accept_whitespace_only_reason_is_422(app_client):
    job = _make_job()
    gate = _make_gate(job["id"])
    item = _make_review_item(job["id"], gate["id"])

    resp = app_client.post(
        f"/api/v1/review/items/{item['id']}/accept", headers=HEADERS, json={"reason": "   \n\t  "}
    )
    assert resp.status_code == 422


def test_accept_records_the_reason_and_pins_the_hash(app_client):
    job = _make_job()
    gate = _make_gate(job["id"], open_items=1)
    item = _make_review_item(job["id"], gate["id"], wav_sha256="c" * 64)

    resp = app_client.post(
        f"/api/v1/review/items/{item['id']}/accept",
        headers=HEADERS,
        json={"reason": "Whisper mishears the proper noun. The audio is correct."},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["state"] == "accepted"
    assert body["reason"] == "Whisper mishears the proper noun. The audio is correct."
    assert body["wav_sha256"] == "c" * 64

    # The job returns to the runner.
    from narratarr import db

    conn = db.connect()
    try:
        job_row = conn.execute("SELECT state FROM jobs WHERE id = ?", (job["id"],)).fetchone()
        gate_row = conn.execute(
            "SELECT state, open_items FROM gates WHERE id = ?", (gate["id"],)
        ).fetchone()
    finally:
        conn.close()
    assert job_row["state"] == "queued"
    assert gate_row["open_items"] == 0
    assert gate_row["state"] == "resolved"


def test_accept_on_a_non_open_item_is_409(app_client):
    job = _make_job()
    gate = _make_gate(job["id"])
    item = _make_review_item(job["id"], gate["id"], state="accepted")

    resp = app_client.post(
        f"/api/v1/review/items/{item['id']}/accept", headers=HEADERS, json={"reason": "fine"}
    )
    assert resp.status_code == 409


def test_accept_unknown_item_is_404(app_client):
    resp = app_client.post(
        "/api/v1/review/items/does-not-exist/accept", headers=HEADERS, json={"reason": "fine"}
    )
    assert resp.status_code == 404


# ----------------------------------------------------------------- rerender


def test_rerender_an_accepted_item_voids_the_pin_with_the_correct_explanation(app_client):
    job = _make_job()
    gate = _make_gate(job["id"], open_items=0)
    item = _make_review_item(
        job["id"],
        gate["id"],
        state="accepted",
        reason="Whisper mishears the proper noun. The audio is correct.",
        wav_sha256="d" * 64,
    )

    resp = app_client.post(f"/api/v1/review/items/{item['id']}/rerender", headers=HEADERS)
    assert resp.status_code == 202
    body = resp.json()
    assert body["state"] == "voided"
    assert "voided_reason" in body
    assert "does not give the same" in body["voided_reason"]
    assert "not proof that the audio changed" in body["voided_reason"]
    # The voided_reason never implies the words or the reading changed.
    assert "words" in body["voided_reason"]

    from narratarr import db

    conn = db.connect()
    try:
        job_row = conn.execute("SELECT state FROM jobs WHERE id = ?", (job["id"],)).fetchone()
    finally:
        conn.close()
    assert job_row["state"] == "queued"


def test_rerender_an_open_item_marks_it_rerendered_with_no_voided_reason(app_client):
    job = _make_job()
    gate = _make_gate(job["id"], open_items=1)
    item = _make_review_item(job["id"], gate["id"], state="open")

    resp = app_client.post(f"/api/v1/review/items/{item['id']}/rerender", headers=HEADERS)
    assert resp.status_code == 202
    body = resp.json()
    assert body["state"] == "rerendered"
    assert "voided_reason" not in body


def test_rerender_a_resolved_item_is_409(app_client):
    job = _make_job()
    gate = _make_gate(job["id"])
    item = _make_review_item(job["id"], gate["id"], state="resolved")

    resp = app_client.post(f"/api/v1/review/items/{item['id']}/rerender", headers=HEADERS)
    assert resp.status_code == 409


# ------------------------------------------------------------------ resolve


def test_resolve_homograph_records_a_human_decision(app_client):
    job = _make_job()
    gate = _make_gate(job["id"], kind="homograph", open_items=1)
    candidates = [
        {"reading": "verb", "phonemes": "wˈWnd", "audio": "/config/work/x/cand-1.wav"},
        {"reading": "noun", "phonemes": "wˈuːnd", "audio": "/config/work/x/cand-2.wav"},
    ]
    item = _make_review_item(
        job["id"],
        gate["id"],
        kind="homograph_occurrence",
        word="wound",
        occurrence=1,
        candidates=json.dumps(candidates),
    )

    resp = app_client.post(
        f"/api/v1/review/items/{item['id']}/resolve", headers=HEADERS, json={"reading": "verb"}
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["state"] == "resolved"
    assert body["resolution"] == "verb"


def test_resolve_homograph_unknown_reading_is_422(app_client):
    job = _make_job()
    gate = _make_gate(job["id"], kind="homograph")
    candidates = [
        {"reading": "verb", "phonemes": "wˈWnd", "audio": "a.wav"},
        {"reading": "noun", "phonemes": "wˈuːnd", "audio": "b.wav"},
    ]
    item = _make_review_item(
        job["id"], gate["id"], kind="homograph_occurrence", candidates=json.dumps(candidates)
    )

    resp = app_client.post(
        f"/api/v1/review/items/{item['id']}/resolve", headers=HEADERS, json={"reading": "adjective"}
    )
    assert resp.status_code == 422


def test_resolve_on_a_qc_chunk_item_is_rejected(app_client):
    job = _make_job()
    gate = _make_gate(job["id"])
    item = _make_review_item(job["id"], gate["id"], kind="qc_chunk")

    resp = app_client.post(
        f"/api/v1/review/items/{item['id']}/resolve", headers=HEADERS, json={"reading": "verb"}
    )
    assert resp.status_code == 400


# --------------------------------------------------------------------- audio


def test_homograph_audio_serves_both_candidates(app_client, tmp_path):
    job = _make_job()
    gate = _make_gate(job["id"], kind="homograph")
    cand1 = tmp_path / "cand-1.wav"
    cand2 = tmp_path / "cand-2.wav"
    _write_fake_wav(cand1, b"candidate-one-bytes")
    _write_fake_wav(cand2, b"candidate-two-bytes-here")
    candidates = [
        {"reading": "verb", "phonemes": "wˈWnd", "audio": str(cand1)},
        {"reading": "noun", "phonemes": "wˈuːnd", "audio": str(cand2)},
    ]
    item = _make_review_item(
        job["id"], gate["id"], kind="homograph_occurrence", candidates=json.dumps(candidates)
    )

    resp1 = app_client.get(f"/api/v1/review/items/{item['id']}/audio/1", headers=HEADERS)
    assert resp1.status_code == 200
    assert resp1.headers["content-type"] == "audio/wav"
    assert resp1.content == b"candidate-one-bytes"

    resp2 = app_client.get(f"/api/v1/review/items/{item['id']}/audio/2", headers=HEADERS)
    assert resp2.status_code == 200
    assert resp2.content == b"candidate-two-bytes-here"


def test_homograph_audio_out_of_range_candidate_is_404(app_client, tmp_path):
    job = _make_job()
    gate = _make_gate(job["id"], kind="homograph")
    cand1 = tmp_path / "cand-1.wav"
    _write_fake_wav(cand1)
    item = _make_review_item(
        job["id"],
        gate["id"],
        kind="homograph_occurrence",
        candidates=json.dumps([{"reading": "verb", "phonemes": "x", "audio": str(cand1)}]),
    )

    resp = app_client.get(f"/api/v1/review/items/{item['id']}/audio/2", headers=HEADERS)
    assert resp.status_code == 404


def test_chunk_audio_supports_a_range_request(app_client):
    job = _make_job()
    gate = _make_gate(job["id"])
    item = _make_review_item(job["id"], gate["id"], kind="qc_chunk")
    payload = bytes(range(256)) * 4  # 1024 bytes, easy to slice and check
    path = _chunk_audio_path(app_client, job, item)
    _write_fake_wav(path, payload)

    full = app_client.get(f"/api/v1/review/items/{item['id']}/audio", headers=HEADERS)
    assert full.status_code == 200
    assert full.content == payload

    ranged = app_client.get(
        f"/api/v1/review/items/{item['id']}/audio",
        headers={**HEADERS, "Range": "bytes=10-19"},
    )
    assert ranged.status_code == 206
    assert ranged.content == payload[10:20]
    assert ranged.headers["content-range"] == f"bytes 10-19/{len(payload)}"
    assert ranged.headers["accept-ranges"] == "bytes"


def test_chunk_audio_missing_file_is_404(app_client):
    job = _make_job()
    gate = _make_gate(job["id"])
    item = _make_review_item(job["id"], gate["id"], kind="qc_chunk")

    resp = app_client.get(f"/api/v1/review/items/{item['id']}/audio", headers=HEADERS)
    assert resp.status_code == 404


def test_audio_route_rejects_a_query_parameter_key(app_client, monkeypatch):
    """APP-CONTRACT.md section 10.1: the key never goes into a URL.

    This app's `require_key` override reads only the `X-Api-Key` header.
    A caller who tries a `?apikey=` fallback must still be refused,
    because no route here ever reads that query parameter on its own
    behalf, and the auth dependency does not consult it either.
    """
    job = _make_job()
    gate = _make_gate(job["id"])
    item = _make_review_item(job["id"], gate["id"], kind="qc_chunk")
    path = _chunk_audio_path(app_client, job, item)
    _write_fake_wav(path)

    from narratarr.api.common import require_key

    def _strict_require_key(request: Request):
        from narratarr.api.common import ApiError

        key = request.headers.get("x-api-key")
        if key != "test-key":
            raise ApiError("unauthorized", "The X-Api-Key header is missing or wrong.", status=401)
        return object()

    app_client.app.dependency_overrides[require_key] = _strict_require_key
    try:
        resp = app_client.get(f"/api/v1/review/items/{item['id']}/audio?apikey=test-key")
        assert resp.status_code == 401
    finally:
        # Restore the permissive override for any later use of this client.
        app_client.app.dependency_overrides[require_key] = lambda: object()
