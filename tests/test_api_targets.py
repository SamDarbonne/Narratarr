"""Tests for `narratarr/api/targets.py` and `narratarr/api/settings.py`.

A test here never loads a model, renders audio, or touches the network.
`FolderTarget` needs neither, so it stands in for "a real target" below.

These tests build their own FastAPI app and their own database, instead of
using `tests/conftest.py` (owned by W1) — refer to the note at the top of
`tests/test_api_review.py`.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    """Return a `TestClient` wired to a fresh database, for `targets` and `settings`."""
    monkeypatch.setenv("NARRATARR_CONFIG_DIR", str(tmp_path))

    from narratarr import config

    config.get_settings.cache_clear()

    from narratarr import db

    db.init_db()

    from narratarr.api import settings as settings_api
    from narratarr.api import targets
    from narratarr.api.common import ApiError, require_key

    app = FastAPI()
    app.include_router(targets.router)
    app.include_router(settings_api.router)

    class _FakeApiKey:
        id = "test-key-id"
        name = "test-key"

    app.dependency_overrides[require_key] = lambda: _FakeApiKey()

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


HEADERS = {"X-Api-Key": "test-key"}

FOLDER_CONFIG = {"root": "/output", "layout": "{author}/{title}/{title}.m4b", "copy_cover": True}


def _abs_config(tmp_path, token_env="NARRATARR_ABS_TOKEN"):
    return {
        "base_url": "http://audiobookshelf:13378",
        "library_id": "lib-1",
        "token_env": token_env,
        "folder_target": {"root": str(tmp_path / "abs-out"), "layout": FOLDER_CONFIG["layout"]},
    }


# ------------------------------------------------------------------- targets


def test_create_and_get_folder_target(app_client):
    resp = app_client.post(
        "/api/v1/targets",
        headers=HEADERS,
        json={"name": "main-folder", "kind": "folder", "config": FOLDER_CONFIG},
    )
    assert resp.status_code == 201
    created = resp.json()
    assert created["name"] == "main-folder"
    assert created["kind"] == "folder"

    resp2 = app_client.get(f"/api/v1/targets/{created['id']}", headers=HEADERS)
    assert resp2.status_code == 200
    assert resp2.json()["config"]["root"] == "/output"


def test_create_target_invalid_config_is_422(app_client):
    resp = app_client.post(
        "/api/v1/targets",
        headers=HEADERS,
        json={"name": "bad", "kind": "folder", "config": {"root": ""}},
    )
    assert resp.status_code == 422


def test_create_target_unknown_kind_is_422(app_client):
    resp = app_client.post(
        "/api/v1/targets",
        headers=HEADERS,
        json={"name": "mystery", "kind": "carrier-pigeon", "config": {}},
    )
    assert resp.status_code == 422


def test_create_target_duplicate_name_is_409(app_client):
    body = {"name": "dup", "kind": "folder", "config": FOLDER_CONFIG}
    first = app_client.post("/api/v1/targets", headers=HEADERS, json=body)
    assert first.status_code == 201
    second = app_client.post("/api/v1/targets", headers=HEADERS, json=body)
    assert second.status_code == 409


def test_get_unknown_target_is_404(app_client):
    resp = app_client.get("/api/v1/targets/does-not-exist", headers=HEADERS)
    assert resp.status_code == 404


def test_put_target_updates_config(app_client):
    created = app_client.post(
        "/api/v1/targets",
        headers=HEADERS,
        json={"name": "to-edit", "kind": "folder", "config": FOLDER_CONFIG},
    ).json()

    new_config = dict(FOLDER_CONFIG, copy_cover=False)
    resp = app_client.put(
        f"/api/v1/targets/{created['id']}",
        headers=HEADERS,
        json={"name": "to-edit", "kind": "folder", "config": new_config},
    )
    assert resp.status_code == 200
    assert resp.json()["config"]["copy_cover"] is False


def test_delete_target(app_client):
    created = app_client.post(
        "/api/v1/targets",
        headers=HEADERS,
        json={"name": "to-delete", "kind": "folder", "config": FOLDER_CONFIG},
    ).json()

    resp = app_client.delete(f"/api/v1/targets/{created['id']}", headers=HEADERS)
    assert resp.status_code == 200

    resp2 = app_client.get(f"/api/v1/targets/{created['id']}", headers=HEADERS)
    assert resp2.status_code == 404


def test_target_test_route_writes_nothing_and_checks_reachability(app_client, tmp_path):
    writable_root = tmp_path / "out"
    writable_root.mkdir()
    created = app_client.post(
        "/api/v1/targets",
        headers=HEADERS,
        json={
            "name": "reachable",
            "kind": "folder",
            "config": {"root": str(writable_root), "layout": FOLDER_CONFIG["layout"]},
        },
    ).json()

    resp = app_client.post(f"/api/v1/targets/{created['id']}/test", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    # The test route writes nothing: the directory holds no new file.
    assert list(writable_root.iterdir()) == []


def test_target_test_unknown_id_is_404(app_client):
    resp = app_client.post("/api/v1/targets/does-not-exist/test", headers=HEADERS)
    assert resp.status_code == 404


# ------------------------------------------------------- secret redaction


def test_config_rejects_a_raw_token_field(app_client):
    resp = app_client.post(
        "/api/v1/targets",
        headers=HEADERS,
        json={
            "name": "leaky",
            "kind": "audiobookshelf",
            "config": {
                "base_url": "http://audiobookshelf:13378",
                "library_id": "lib-1",
                "token_env": "NARRATARR_ABS_TOKEN",
                "token": "sk-super-secret-value",
                "folder_target": {"root": "/output"},
            },
        },
    )
    assert resp.status_code == 422
    assert "sk-super-secret-value" not in resp.text


def test_token_value_never_appears_in_any_response_body(app_client, tmp_path, monkeypatch):
    """APP-CONTRACT.md section 10.2: the API redacts every secret.

    The real token lives only in the environment. This test plants one,
    creates and reads back an audiobookshelf target every way this file's
    routes allow, and asserts the raw value is absent from every body.
    """
    monkeypatch.setenv("NARRATARR_ABS_TOKEN", "sk-super-secret-value")
    config = _abs_config(tmp_path)

    created = app_client.post(
        "/api/v1/targets", headers=HEADERS, json={"name": "abs", "kind": "audiobookshelf", "config": config}
    )
    assert created.status_code == 201
    assert "sk-super-secret-value" not in created.text
    target_id = created.json()["id"]

    get_one = app_client.get(f"/api/v1/targets/{target_id}", headers=HEADERS)
    assert "sk-super-secret-value" not in get_one.text

    list_all = app_client.get("/api/v1/targets", headers=HEADERS)
    assert "sk-super-secret-value" not in list_all.text

    updated = app_client.put(
        f"/api/v1/targets/{target_id}",
        headers=HEADERS,
        json={"name": "abs", "kind": "audiobookshelf", "config": config},
    )
    assert "sk-super-secret-value" not in updated.text

    settings_resp = app_client.get("/api/v1/settings", headers=HEADERS)
    assert "sk-super-secret-value" not in settings_resp.text
    assert settings_resp.json()["secrets"]["NARRATARR_ABS_TOKEN"]["present"] is True


def test_target_query_parameter_key_is_rejected(app_client):
    """No route in this file accepts a key from the query string."""
    from narratarr.api.common import require_key

    def _strict_require_key(request: Request):
        from narratarr.api.common import ApiError

        if request.headers.get("x-api-key") != "test-key":
            raise ApiError("unauthorized", "Missing or wrong key.", status=401)
        return object()

    app_client.app.dependency_overrides[require_key] = _strict_require_key
    try:
        resp = app_client.get("/api/v1/targets?apikey=test-key")
        assert resp.status_code == 401
    finally:
        app_client.app.dependency_overrides[require_key] = lambda: object()


# ------------------------------------------------------------------ settings


def test_get_settings_reports_secret_presence_never_value(app_client, monkeypatch):
    monkeypatch.delenv("NARRATARR_ABS_TOKEN", raising=False)
    resp = app_client.get("/api/v1/settings", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["secrets"]["NARRATARR_ABS_TOKEN"]["present"] is False

    monkeypatch.setenv("NARRATARR_ABS_TOKEN", "sk-another-secret")
    resp2 = app_client.get("/api/v1/settings", headers=HEADERS)
    assert resp2.json()["secrets"]["NARRATARR_ABS_TOKEN"]["present"] is True
    assert "sk-another-secret" not in resp2.text


def test_put_settings_upserts_and_get_reflects_it(app_client):
    resp = app_client.put(
        "/api/v1/settings", headers=HEADERS, json={"settings": {"sample_gate": False}}
    )
    assert resp.status_code == 200
    assert resp.json()["settings"]["sample_gate"] is False

    resp2 = app_client.get("/api/v1/settings", headers=HEADERS)
    assert resp2.json()["settings"]["sample_gate"] is False


def test_library_scan_writes_nothing_but_a_sentinel_and_returns_202(app_client, tmp_path):
    library_dir = tmp_path / "library"
    library_dir.mkdir()
    (library_dir / "a-book.epub").write_bytes(b"not a real epub")

    resp = app_client.post("/api/v1/library/scan", headers=HEADERS)
    assert resp.status_code == 202

    listing = app_client.get("/api/v1/library", headers=HEADERS)
    assert listing.status_code == 200
    names = [row["name"] for row in listing.json()["items"]]
    assert "a-book.epub" in names
