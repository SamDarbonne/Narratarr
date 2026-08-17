"""Tests for the jobs and system HTTP routes.

APP-CONTRACT.md section 13 defines the routes, the status codes, the
pagination envelope, and the error envelope. Section 10.1 defines the API
key rule. Every test here goes through the real FastAPI app, with the
background runner thread disabled (refer to `tests/conftest.py`), so a
route never blocks on a render it would otherwise start.
"""

from __future__ import annotations

import json

from narratarr import db as db_module


def _make_epub(tmp_path, name: str = "book.epub", content: bytes = b"fake epub bytes") -> str:
    """Write a small file with an .epub extension. Return its path as a string."""
    path = tmp_path / name
    path.write_bytes(content)
    return str(path)


# ---------------------------------------------------------------------- auth


def test_health_needs_no_key(client):
    """GET /system/health succeeds even with no valid key."""
    response = client.get("/api/v1/system/health", headers={"X-Api-Key": ""})
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_missing_key_is_rejected(client):
    """A protected route with no X-Api-Key header returns 401."""
    response = client.get("/api/v1/jobs", headers={"X-Api-Key": ""})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_wrong_key_is_rejected(client):
    """A protected route with a wrong key returns 401."""
    response = client.get("/api/v1/jobs", headers={"X-Api-Key": "not-the-real-key"})
    assert response.status_code == 401


def test_correct_key_is_accepted(client):
    """The default client carries the correct key, and the route succeeds."""
    response = client.get("/api/v1/jobs")
    assert response.status_code == 200


def test_query_parameter_key_is_rejected(client):
    """APP-CONTRACT.md section 10.1: the key is never accepted in a URL.

    A caller that puts the correct key in `?apikey=` but sends no header
    (or a wrong one) still gets 401. Only the `X-Api-Key` header counts.
    """
    real_key = client.headers["X-Api-Key"]
    response = client.get(f"/api/v1/jobs?apikey={real_key}", headers={"X-Api-Key": ""})
    assert response.status_code == 401


# -------------------------------------------------------------------- create


def test_create_job_from_source_path(client, tmp_path):
    """POST /jobs with {"source_path": "..."} makes a queued job. 201."""
    epub_path = _make_epub(tmp_path)
    response = client.post("/api/v1/jobs", json={"source_path": epub_path, "title": "My Book"})
    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "My Book"
    assert body["state"] == "queued"
    assert body["slug"] == "my-book"
    assert body["gates"] == []
    assert body["deliveries"] == []


def test_create_job_missing_source_path_is_422(client):
    """POST /jobs with neither an upload nor source_path returns 422."""
    response = client.post("/api/v1/jobs", json={})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_create_job_nonexistent_path_is_404(client):
    """POST /jobs with a source_path that does not exist returns 404."""
    response = client.post("/api/v1/jobs", json={"source_path": "/no/such/file.epub"})
    assert response.status_code == 404


def test_create_job_duplicate_is_refused(client, tmp_path):
    """A second job with the same file hash is refused with 409."""
    epub_path = _make_epub(tmp_path, name="dup.epub", content=b"identical bytes")
    first = client.post("/api/v1/jobs", json={"source_path": epub_path, "title": "First"})
    assert first.status_code == 201

    second_path = _make_epub(tmp_path, name="dup2.epub", content=b"identical bytes")
    second = client.post("/api/v1/jobs", json={"source_path": second_path, "title": "Second"})
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "duplicate"


def test_create_job_duplicate_allowed_when_flagged(client, tmp_path):
    """allow_duplicate: true lets a second job with the same hash through."""
    epub_path = _make_epub(tmp_path, name="dup3.epub", content=b"same bytes again")
    first = client.post("/api/v1/jobs", json={"source_path": epub_path, "title": "First"})
    assert first.status_code == 201

    second_path = _make_epub(tmp_path, name="dup4.epub", content=b"same bytes again")
    second = client.post(
        "/api/v1/jobs",
        json={"source_path": second_path, "title": "Second", "allow_duplicate": True},
    )
    assert second.status_code == 201


def test_create_job_unsupported_extension_fails_not_skips(client, tmp_path):
    """An unsupported extension still makes a job, immediately failed."""
    text_path = tmp_path / "notes.txt"
    text_path.write_text("not an epub")
    response = client.post("/api/v1/jobs", json={"source_path": str(text_path)})
    assert response.status_code == 201
    body = response.json()
    assert body["state"] == "failed"
    assert ".txt" in body["error"]


# ---------------------------------------------------------------------- read


def test_get_job_not_found(client):
    """GET /jobs/{id} for an unknown id returns 404."""
    response = client.get("/api/v1/jobs/no-such-id")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_list_jobs_paginates(client, tmp_path):
    """GET /jobs returns the {"items", "total", "limit", "offset"} envelope."""
    for i in range(3):
        path = _make_epub(tmp_path, name=f"book{i}.epub", content=f"book {i}".encode())
        client.post("/api/v1/jobs", json={"source_path": path, "title": f"Book {i}"})

    response = client.get("/api/v1/jobs?limit=2&offset=0")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert len(body["items"]) == 2
    # The list route omits the two config blobs to stay light.
    assert "book_config" not in body["items"][0]


def test_list_jobs_filters_by_state(client, tmp_path):
    """?state= filters the list to jobs in that state."""
    ok_path = _make_epub(tmp_path, name="ok.epub")
    bad_path = tmp_path / "bad.txt"
    bad_path.write_text("nope")
    client.post("/api/v1/jobs", json={"source_path": ok_path, "title": "OK"})
    client.post("/api/v1/jobs", json={"source_path": str(bad_path), "title": "Bad"})

    response = client.get("/api/v1/jobs?state=failed")
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["title"] == "Bad"


# ------------------------------------------------------------------- control


def test_pause_then_start_round_trip(client, tmp_path, make_job):
    """A queued job can be paused, then started again."""
    job_id = make_job(state="queued")
    paused = client.post(f"/api/v1/jobs/{job_id}/pause")
    assert paused.status_code == 202

    detail = client.get(f"/api/v1/jobs/{job_id}").json()
    assert detail["state"] == "paused"

    started = client.post(f"/api/v1/jobs/{job_id}/start")
    assert started.status_code == 202
    detail = client.get(f"/api/v1/jobs/{job_id}").json()
    assert detail["state"] == "queued"


def test_start_a_non_paused_job_is_409(client, make_job):
    """POST /jobs/{id}/start on a job that is not paused returns 409."""
    job_id = make_job(state="queued")
    response = client.post(f"/api/v1/jobs/{job_id}/start")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


def test_cancel_a_done_job_is_409(client, make_job):
    """A job already `done` cannot be cancelled again."""
    job_id = make_job(state="done")
    response = client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert response.status_code == 409


def test_retry_clears_the_error(client, make_job):
    """POST /jobs/{id}/retry clears jobs.error and queues the job again."""
    job_id = make_job(state="failed", error="something broke")
    response = client.post(f"/api/v1/jobs/{job_id}/retry")
    assert response.status_code == 202

    detail = client.get(f"/api/v1/jobs/{job_id}").json()
    assert detail["state"] == "queued"
    assert detail["error"] is None


def test_deliver_requires_a_resting_state(client, make_job):
    """POST /jobs/{id}/deliver on a queued (in-flight) job returns 409."""
    job_id = make_job(state="queued")
    response = client.post(f"/api/v1/jobs/{job_id}/deliver")
    assert response.status_code == 409


def test_deliver_from_done_queues_the_deliver_stage(client, make_job):
    """POST /jobs/{id}/deliver on a done job re-queues it at the deliver stage."""
    job_id = make_job(state="done", stage=None)
    response = client.post(f"/api/v1/jobs/{job_id}/deliver")
    assert response.status_code == 202

    detail = client.get(f"/api/v1/jobs/{job_id}").json()
    assert detail["state"] == "queued"
    assert detail["stage"] == "deliver"


# -------------------------------------------------------------------- config


def test_get_and_put_job_config(client, make_job):
    """PUT /jobs/{id}/config replaces the book config and the QC config."""
    job_id = make_job(state="queued")
    response = client.put(
        f"/api/v1/jobs/{job_id}/config",
        json={"book_config": {"voice": "bm_george"}, "qc_config": {"wer_max": 0.2}},
    )
    assert response.status_code == 200

    fetched = client.get(f"/api/v1/jobs/{job_id}/config").json()
    assert fetched["book_config"]["voice"] == "bm_george"
    assert fetched["qc_config"]["wer_max"] == 0.2


def test_put_job_config_conflicts_while_running(client, make_job):
    """PUT /jobs/{id}/config on a running job returns 409."""
    job_id = make_job(state="running")
    response = client.put(f"/api/v1/jobs/{job_id}/config", json={"book_config": {}, "qc_config": {}})
    assert response.status_code == 409


# --------------------------------------------------------------------- fix


def test_fix_requires_a_reason(client, make_job):
    """POST /jobs/{id}/fix with no reason on an item returns 422."""
    job_id = make_job(state="done")
    response = client.post(
        f"/api/v1/jobs/{job_id}/fix",
        json={"items": [{"chapter": "ch01", "chunk": "0001", "action": "rerender"}]},
    )
    assert response.status_code == 422


def test_fix_accepts_a_pronunciation_correction(client, make_job):
    """A valid fix request queues the job at the render stage."""
    job_id = make_job(state="done")
    response = client.post(
        f"/api/v1/jobs/{job_id}/fix",
        json={
            "items": [
                {
                    "chapter": "ch01", "chunk": "0001", "action": "pronunciation",
                    "value": {"word": "gippo", "reading": "guivin"},
                    "reason": "wrong reading",
                }
            ]
        },
    )
    assert response.status_code == 202
    detail = client.get(f"/api/v1/jobs/{job_id}").json()
    assert detail["state"] == "queued"
    assert detail["stage"] == "render"
    assert detail["book_config"]["pronunciations"]["gippo"] == "guivin"


# ------------------------------------------------------------------- events


def test_get_events(client, make_job):
    """GET /jobs/{id}/events returns the events written for that job."""
    job_id = make_job(state="queued")
    conn = db_module.connect()
    try:
        from narratarr.runner import write_event

        write_event(conn, job_id, "info", "hello")
        write_event(conn, job_id, "warning", "careful")
        conn.commit()
    finally:
        conn.close()

    response = client.get(f"/api/v1/jobs/{job_id}/events")
    assert response.status_code == 200
    messages = [item["message"] for item in response.json()["items"]]
    assert messages == ["hello", "careful"]


def test_get_events_filters_by_level(client, make_job):
    """?level= filters the event log."""
    job_id = make_job(state="queued")
    conn = db_module.connect()
    try:
        from narratarr.runner import write_event

        write_event(conn, job_id, "info", "hello")
        write_event(conn, job_id, "error", "oh no")
        conn.commit()
    finally:
        conn.close()

    response = client.get(f"/api/v1/jobs/{job_id}/events?level=error")
    messages = [item["message"] for item in response.json()["items"]]
    assert messages == ["oh no"]


# -------------------------------------------------------------------- delete


def test_delete_job(client, make_job):
    """DELETE /jobs/{id} removes the row. A later GET returns 404."""
    job_id = make_job(state="queued")
    response = client.delete(f"/api/v1/jobs/{job_id}")
    assert response.status_code == 200
    assert client.get(f"/api/v1/jobs/{job_id}").status_code == 404


# ------------------------------------------------------------- pipeline facts


def test_status_and_artifacts_do_not_crash_without_the_adapter(client, make_job):
    """GET .../status and .../artifacts degrade to {} when the adapter is absent.

    W2's adapter is not installed in this test environment. Both routes
    must still answer 200, per `narratarr.runner.get_pipeline_status` and
    `get_pipeline_artifacts`'s empty-dict-on-fault convention.
    """
    job_id = make_job(state="queued")
    status_response = client.get(f"/api/v1/jobs/{job_id}/status")
    artifacts_response = client.get(f"/api/v1/jobs/{job_id}/artifacts")
    assert status_response.status_code == 200
    assert artifacts_response.status_code == 200
    assert isinstance(status_response.json(), dict)
    assert isinstance(artifacts_response.json(), dict)


# ---------------------------------------------------------------------- system


def test_system_status_reports_secret_presence_not_value(client, monkeypatch):
    """GET /system/status reports presence, never the secret's value."""
    from narratarr.config import get_settings

    monkeypatch.setenv("NARRATARR_ABS_TOKEN", "super-secret-value")
    get_settings.cache_clear()

    response = client.get("/api/v1/system/status")
    assert response.status_code == 200
    body = response.json()
    assert body["secrets"]["NARRATARR_ABS_TOKEN"]["present"] is True
    assert "super-secret-value" not in json.dumps(body)
    get_settings.cache_clear()
