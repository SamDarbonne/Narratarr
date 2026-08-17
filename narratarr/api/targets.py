"""The delivery targets. APP-CONTRACT.md section 13.4 and section 8.

A target holds the configuration Narratarr needs to deliver a finished
audiobook: a folder, or an Audiobookshelf server. `narratarr/adapter/targets/`
(owner W2) holds the code that actually validates a configuration and talks
to a target. This module imports that code lazily, inside each handler, so
this module still imports with no pipeline installed. Refer to
APP-CONTRACT.md section 3.

**A secret never leaves this module in a response.** APP-CONTRACT.md section
10.2: a target configuration returns `"token_env": "NARRATARR_ABS_TOKEN"`,
and never the token itself. This module also refuses to store a raw secret
in the first place, so a client that sends one by mistake gets an error, not
a false sense that the value is safe in the database.
"""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from narratarr.api.common import ApiError, paginate, require_key
from narratarr.db import connect, new_id, now, transaction
from narratarr.models import Target

router = APIRouter(prefix="/api/v1/targets", tags=["targets"], dependencies=[Depends(require_key)])


# A key name that suggests raw secret material. A caller must use the
# `*_env` convention instead — refer to APP-CONTRACT.md section 8.2 rule 2.
# The API refuses a config that carries one of these keys, so a raw secret
# never reaches the database.
_SECRET_KEY_DENYLIST = {"token", "password", "secret", "api_key", "auth_token", "key"}


class TargetCreateRequest(BaseModel):
    """The body of `POST /targets`."""

    name: str
    kind: str
    enabled: bool = True
    config: dict = Field(default_factory=dict)


class TargetUpdateRequest(BaseModel):
    """The body of `PUT /targets/{id}`. Replaces the whole target."""

    name: str
    kind: str
    enabled: bool = True
    config: dict = Field(default_factory=dict)


def _reject_raw_secrets(config: dict) -> None:
    """Raise a 422 `ApiError` when `config` carries a raw-secret-shaped key.

    APP-CONTRACT.md section 10.2: a secret is read from the environment at
    the moment of use, and never enters the database. A target names its
    secret's environment variable with a `*_env` key, for example
    `token_env`. A literal `token` key means a caller pasted the secret
    itself, which this API must refuse.
    """
    bad_keys = sorted(k for k in config if k.lower() in _SECRET_KEY_DENYLIST)
    if bad_keys:
        raise ApiError(
            "secret_in_config",
            f"Remove {bad_keys} from config. Name the environment variable "
            "that holds the secret instead, for example \"token_env\".",
            status=422,
        )


def _get_target_instance(kind: str):
    """Return a target instance for `kind`. Raise a 422 `ApiError` for an unknown kind.

    Imported lazily so this module needs no pipeline install to import.
    Refer to APP-CONTRACT.md section 3 and section 6.
    `narratarr.adapter.targets.registry()` returns the `{kind: Target}` map.
    """
    from narratarr.adapter.targets import registry

    target = registry().get(kind)
    if target is None:
        raise ApiError("unknown_kind", f"'{kind}' is not a target kind.", status=422)
    return target


def _validate_target_config(kind: str, config: dict) -> None:
    """Validate a target configuration through the target's own `validate()`."""
    target = _get_target_instance(kind)
    try:
        target.validate(config)
    except ValueError as exc:
        raise ApiError("invalid_config", str(exc), status=422)


def _serialize_target(row) -> dict:
    """Return one target as a JSON-safe dict, with every secret stripped.

    `config` never carries a raw secret past `_reject_raw_secrets()` at
    write time. This second pass is defence in depth: it strips the same
    deny-listed keys again, so a row written before that check existed
    still comes back clean.
    """
    data = Target.from_row(row).to_dict()
    data["config"] = {
        k: v for k, v in data["config"].items() if k.lower() not in _SECRET_KEY_DENYLIST
    }
    return data


def _get_target_row(conn, target_id: str):
    """Return the `targets` row for `target_id`, or None."""
    return conn.execute("SELECT * FROM targets WHERE id = ?", (target_id,)).fetchone()


def _require_target(conn, target_id: str):
    """Return the target row, or raise a 404 `ApiError`."""
    row = _get_target_row(conn, target_id)
    if row is None:
        raise ApiError("target_not_found", "No target holds this id.", status=404)
    return row


@router.get("")
def list_targets(limit: int = 50, offset: int = 0):
    """List every target."""
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM targets ORDER BY created_at ASC").fetchall()
        items = [_serialize_target(row) for row in rows]
        return paginate(items, limit, offset)
    finally:
        conn.close()


@router.post("", status_code=201)
def create_target(body: TargetCreateRequest):
    """Make a target. Validates `config` through the target's own `validate()`."""
    _reject_raw_secrets(body.config)
    _validate_target_config(body.kind, body.config)

    with transaction() as conn:
        target_id = new_id()
        stamp = now()
        try:
            conn.execute(
                """
                INSERT INTO targets (id, name, kind, enabled, config, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    target_id,
                    body.name,
                    body.kind,
                    1 if body.enabled else 0,
                    json.dumps(body.config),
                    stamp,
                    stamp,
                ),
            )
        except sqlite3.IntegrityError:
            raise ApiError(
                "duplicate_name", f"A target already holds the name '{body.name}'.", status=409
            )
        return _serialize_target(_get_target_row(conn, target_id))


@router.get("/{target_id}")
def get_target_route(target_id: str):
    """Return one target."""
    conn = connect()
    try:
        row = _require_target(conn, target_id)
        return _serialize_target(row)
    finally:
        conn.close()


@router.put("/{target_id}")
def update_target(target_id: str, body: TargetUpdateRequest):
    """Replace a target's configuration. Validates through `validate()` again."""
    _reject_raw_secrets(body.config)
    _validate_target_config(body.kind, body.config)

    with transaction() as conn:
        _require_target(conn, target_id)
        try:
            conn.execute(
                """
                UPDATE targets
                SET name = ?, kind = ?, enabled = ?, config = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    body.name,
                    body.kind,
                    1 if body.enabled else 0,
                    json.dumps(body.config),
                    now(),
                    target_id,
                ),
            )
        except sqlite3.IntegrityError:
            raise ApiError(
                "duplicate_name", f"A target already holds the name '{body.name}'.", status=409
            )
        return _serialize_target(_get_target_row(conn, target_id))


@router.delete("/{target_id}")
def delete_target(target_id: str):
    """Delete a target. Cascades to its `deliveries` rows."""
    with transaction() as conn:
        _require_target(conn, target_id)
        conn.execute("DELETE FROM targets WHERE id = ?", (target_id,))
        return {"deleted": True}


@router.post("/{target_id}/test")
def test_target(target_id: str):
    """Check that a target is reachable. Writes nothing.

    APP-CONTRACT.md section 13.4: this route never writes. The `Target`
    protocol's own `test()` method carries the same promise. Refer to
    APP-CONTRACT.md section 8.
    """
    conn = connect()
    try:
        row = _require_target(conn, target_id)
        target_dict = _serialize_target(row)
    finally:
        conn.close()

    target = _get_target_instance(target_dict["kind"])
    result = target.test(target_dict["config"])
    return {
        "ok": result.ok,
        "message": result.message,
        "url": result.url,
    }
