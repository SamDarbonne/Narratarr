"""Hashing and the meta-file idempotence rule.

CONTRACT.md section 3 defines every rule in this module.
This module is a kernel file. Only the overlord edits it.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = 1
META_SUFFIX = ".meta.json"


# --------------------------------------------------------------------------- hashes


def hash_bytes(data: bytes) -> str:
    """Return the hex SHA-256 of the bytes."""
    return hashlib.sha256(data).hexdigest()


def hash_text(text: str) -> str:
    """Return the hex SHA-256 of the UTF-8 bytes of the string."""
    return hash_bytes(text.encode("utf-8"))


def hash_file(path: str | os.PathLike) -> str:
    """Return the hex SHA-256 of the content of the file."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json(obj: Any) -> bytes:
    """Return the canonical JSON bytes of the object."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def hash_obj(obj: Any) -> str:
    """Return the hex SHA-256 of the canonical JSON of the object."""
    return hash_bytes(canonical_json(obj))


def hash_many(parts: Iterable[str]) -> str:
    """Return the hex SHA-256 of the joined hashes of a list of strings."""
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


# --------------------------------------------------------------------------- time


def utc_stamp() -> str:
    """Return the current UTC time in the form YYYYMMDDThhmmssZ."""
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


# --------------------------------------------------------------------------- atomic io


def write_json(path: str | os.PathLike, obj: Any) -> None:
    """Write the object as JSON. The write is atomic."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), delete=False, suffix=".tmp"
    )
    try:
        json.dump(obj, handle, ensure_ascii=False, indent=2, sort_keys=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, path)


def read_json(path: str | os.PathLike) -> Any | None:
    """Return the parsed JSON of the file, or None when the file is absent or bad."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, NotADirectoryError, json.JSONDecodeError):
        return None


def write_text(path: str | os.PathLike, text: str) -> None:
    """Write the text as UTF-8. The write is atomic."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), delete=False, suffix=".tmp"
    )
    try:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, path)


def write_bytes(path: str | os.PathLike, data: bytes) -> None:
    """Write the bytes. The write is atomic (temp file in the same directory,
    then os.replace). A failed write removes its own temp file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "wb", dir=str(path.parent), delete=False, suffix=".tmp"
    )
    try:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, path)


# --------------------------------------------------------------------------- meta


def meta_path(out_path: str | os.PathLike) -> Path:
    """Return the path of the meta file of an output file."""
    out_path = Path(out_path)
    return out_path.with_name(out_path.name + META_SUFFIX)


def read_meta(out_path: str | os.PathLike) -> dict | None:
    """Return the meta of an output file, or None when the meta is absent or bad."""
    data = read_json(meta_path(out_path))
    if isinstance(data, dict):
        return data
    return None


def write_meta(
    out_path: str | os.PathLike,
    stage: str,
    input_hash: str,
    config_hash: str,
    extra: dict | None = None,
) -> dict:
    """Write the meta file of an output file. Return the meta."""
    out_path = Path(out_path)
    output_size = out_path.stat().st_size
    meta = {
        "schema": SCHEMA,
        "stage": stage,
        "output": out_path.name,
        "input_hash": input_hash,
        "config_hash": config_hash,
        "output_sha256": hash_file(out_path),
        "output_size": output_size,
        "created_at": utc_stamp(),
        "extra": extra or {},
    }
    write_json(meta_path(out_path), meta)
    return meta


def is_fresh(
    out_path: str | os.PathLike,
    input_hash: str,
    config_hash: str,
    check_output_hash: bool = False,
) -> bool:
    """Return True when the output needs no work.

    The output is fresh when the file exists, the meta parses, both hashes
    match, AND the file's current size on disk equals meta["output_size"].
    CONTRACT.md 3 calls the size check mandatory: it is one `stat` call, so it
    is always affordable, and it is the only thing that catches a file
    truncated by a kill or a full disk while its meta survives untouched --
    a truncated WAV with an intact, matching meta file is exactly the fault
    that made a truncated output trusted forever before this check existed.

    Backward compatibility: a meta file written before `output_size` existed
    (schema 1 metas from before this fix) has no `output_size` key. That is
    treated as STALE, not as "skip the check" -- a meta that predates the
    field cannot vouch for the file beside it, and the field's whole purpose
    is to prevent exactly that kind of blind trust. The cost is a rebuild of
    the (cheap) affected artifacts on the next run; see CONTRACT.md 3 and the
    worker report for the measured blast radius on the real book.

    Set check_output_hash to also verify the output file's content against
    output_sha256. That reads the whole file, so a stage uses it only when
    the cost is acceptable; the size check above runs unconditionally either
    way.
    """
    out_path = Path(out_path)
    try:
        actual_size = out_path.stat().st_size
    except OSError:
        return False
    meta = read_meta(out_path)
    if not meta:
        return False
    if meta.get("schema") != SCHEMA:
        return False
    if meta.get("input_hash") != input_hash:
        return False
    if meta.get("config_hash") != config_hash:
        return False
    expected_size = meta.get("output_size")
    if expected_size is None:
        return False  # backward-compat decision: missing output_size => stale
    if actual_size != expected_size:
        return False
    if check_output_hash and meta.get("output_sha256") != hash_file(out_path):
        return False
    return True


def clear_meta(out_path: str | os.PathLike) -> None:
    """Remove an output file and its meta file. A missing file is not an error."""
    out_path = Path(out_path)
    for target in (out_path, meta_path(out_path)):
        try:
            target.unlink()
        except FileNotFoundError:
            pass
