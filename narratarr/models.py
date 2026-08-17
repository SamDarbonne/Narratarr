"""The row types, and the API request and response models.

APP-CONTRACT.md section 4 defines every database table. Each dataclass below
is a typed view of one row of one table. `from_row()` builds one from a
`sqlite3.Row`. `to_dict()` returns a JSON-safe dict, with every JSON text
column parsed into a Python object.

The Pydantic models below are the request and response bodies of the routes
this worker owns: `api/system.py` and `api/jobs.py`. Refer to APP-CONTRACT.md
section 13.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from pydantic import BaseModel, Field

# --------------------------------------------------------------------- helpers


def _loads(text: str | None, default: Any) -> Any:
    """Parse a JSON text column. Return the default when the text is absent or bad."""
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ row types


@dataclass
class Job:
    """One row of the `jobs` table. Refer to APP-CONTRACT.md section 4.2."""

    id: str
    slug: str
    title: Optional[str]
    author: Optional[str]
    year: Optional[str]
    genre: Optional[str]
    language: str
    source_path: str
    source_sha256: str
    cover_path: Optional[str]
    state: str
    stage: Optional[str]
    worker: str
    priority: int
    progress_done: int
    progress_total: int
    error: Optional[str]
    book_config: str
    qc_config: str
    created_at: str
    updated_at: str
    started_at: Optional[str]
    finished_at: Optional[str]

    @classmethod
    def from_row(cls, row: Any) -> "Job":
        """Build a Job from a `sqlite3.Row` of the `jobs` table."""
        return cls(**{key: row[key] for key in row.keys()})

    def to_dict(self) -> dict:
        """Return a JSON-safe dict. `book_config` and `qc_config` are parsed."""
        data = self.__dict__.copy()
        data["book_config"] = _loads(self.book_config, {})
        data["qc_config"] = _loads(self.qc_config, {})
        return data


@dataclass
class Gate:
    """One row of the `gates` table. Refer to APP-CONTRACT.md section 4.3."""

    id: str
    job_id: str
    kind: str
    state: str
    payload: str
    open_items: int
    created_at: str
    resolved_at: Optional[str]
    resolved_by: Optional[str]
    resolution: Optional[str]
    reason: Optional[str]

    @classmethod
    def from_row(cls, row: Any) -> "Gate":
        """Build a Gate from a `sqlite3.Row` of the `gates` table."""
        return cls(**{key: row[key] for key in row.keys()})

    def to_dict(self) -> dict:
        """Return a JSON-safe dict. `payload` is parsed."""
        data = self.__dict__.copy()
        data["payload"] = _loads(self.payload, {})
        return data


@dataclass
class ReviewItem:
    """One row of `review_items`. Refer to APP-CONTRACT.md section 4.4."""

    id: str
    job_id: str
    gate_id: str
    kind: str
    chapter: str
    chunk: Optional[str]
    word: Optional[str]
    occurrence: Optional[int]
    source_text: Optional[str]
    transcript: Optional[str]
    context: Optional[str]
    wer: Optional[float]
    coverage: Optional[float]
    duration_s: Optional[float]
    flags: str
    wav_sha256: Optional[str]
    candidates: Optional[str]
    state: str
    resolution: Optional[str]
    reason: Optional[str]
    resolved_at: Optional[str]
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> "ReviewItem":
        """Build a ReviewItem from a `sqlite3.Row` of `review_items`."""
        return cls(**{key: row[key] for key in row.keys()})

    def to_dict(self) -> dict:
        """Return a JSON-safe dict. `flags` and `candidates` are parsed."""
        data = self.__dict__.copy()
        data["flags"] = _loads(self.flags, [])
        data["candidates"] = _loads(self.candidates, None)
        return data


@dataclass
class Event:
    """One row of the `events` table. Refer to APP-CONTRACT.md section 4.5."""

    id: int
    job_id: Optional[str]
    level: str
    stage: Optional[str]
    message: str
    data: Optional[str]
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> "Event":
        """Build an Event from a `sqlite3.Row` of the `events` table."""
        return cls(**{key: row[key] for key in row.keys()})

    def to_dict(self) -> dict:
        """Return a JSON-safe dict. `data` is parsed."""
        result = self.__dict__.copy()
        result["data"] = _loads(self.data, None)
        return result


@dataclass
class Target:
    """One row of the `targets` table. Refer to APP-CONTRACT.md section 4.6."""

    id: str
    name: str
    kind: str
    enabled: int
    config: str
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Any) -> "Target":
        """Build a Target from a `sqlite3.Row` of the `targets` table."""
        return cls(**{key: row[key] for key in row.keys()})

    def to_dict(self) -> dict:
        """Return a JSON-safe dict. `config` is parsed."""
        data = self.__dict__.copy()
        data["config"] = _loads(self.config, {})
        data["enabled"] = bool(self.enabled)
        return data


@dataclass
class Delivery:
    """One row of `deliveries`. Refer to APP-CONTRACT.md section 4.6."""

    id: str
    job_id: str
    target_id: str
    state: str
    remote_ref: Optional[str]
    url: Optional[str]
    bytes: Optional[int]
    error: Optional[str]
    created_at: str
    delivered_at: Optional[str]

    @classmethod
    def from_row(cls, row: Any) -> "Delivery":
        """Build a Delivery from a `sqlite3.Row` of the `deliveries` table."""
        return cls(**{key: row[key] for key in row.keys()})

    def to_dict(self) -> dict:
        """Return a JSON-safe dict."""
        return self.__dict__.copy()


@dataclass
class ApiKeyRow:
    """One row of `api_keys`. Refer to APP-CONTRACT.md section 4.7.

    The name avoids `ApiKey`, kept for the auth model below, so a reader
    never confuses the database row with the checked, authenticated caller.
    """

    id: str
    name: str
    key_sha256: str
    created_at: str
    last_used_at: Optional[str]

    @classmethod
    def from_row(cls, row: Any) -> "ApiKeyRow":
        """Build an ApiKeyRow from a `sqlite3.Row` of `api_keys`."""
        return cls(**{key: row[key] for key in row.keys()})

    def to_dict(self) -> dict:
        """Return a JSON-safe dict. The key hash never leaves this process."""
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
        }


@dataclass
class SettingRow:
    """One row of the `settings` table. Refer to APP-CONTRACT.md section 4.7."""

    key: str
    value: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Any) -> "SettingRow":
        """Build a SettingRow from a `sqlite3.Row` of the `settings` table."""
        return cls(**{key: row[key] for key in row.keys()})

    def to_dict(self) -> dict:
        """Return a JSON-safe dict. `value` is parsed."""
        return {
            "key": self.key,
            "value": _loads(self.value, None),
            "updated_at": self.updated_at,
        }


# ------------------------------------------------------------- pydantic: auth


class ApiKeyOut(BaseModel):
    """One API key, without its secret. Refer to APP-CONTRACT.md section 4.7."""

    id: str
    name: str
    created_at: str
    last_used_at: Optional[str] = None


class ApiKeyCreated(ApiKeyOut):
    """An API key, at the moment of creation. Holds the key once."""

    key: str


# ------------------------------------------------------------- pydantic: jobs


class JobCreateRequest(BaseModel):
    """The body of `POST /jobs`, for the `source_path` form.

    An upload uses `multipart/form-data` instead. Refer to
    APP-CONTRACT.md section 13.2.
    """

    source_path: str
    title: Optional[str] = None
    author: Optional[str] = None
    year: Optional[str] = None
    genre: Optional[str] = None
    language: str = "en"
    priority: int = 0
    allow_duplicate: bool = False


class JobConfigUpdate(BaseModel):
    """The body of `PUT /jobs/{id}/config`."""

    book_config: dict = Field(default_factory=dict)
    qc_config: dict = Field(default_factory=dict)


class GateResolveRequest(BaseModel):
    """The body of `POST /gates/{id}/resolve`. Refer to section 9.4."""

    resolution: str
    reason: Optional[str] = None


# ----------------------------------------------------------- pydantic: system


class HealthResponse(BaseModel):
    """The body of `GET /system/health`. No key needed for this route."""

    status: str = "ok"
    version: str


class SecretStatus(BaseModel):
    """Whether one named secret is present. Never its value."""

    present: bool


class SystemStatusResponse(BaseModel):
    """The body of `GET /system/status`."""

    runner_state: str
    queue_depth: int
    disk_free_bytes: int
    models: dict[str, bool]
    secrets: dict[str, SecretStatus]


class ModelInfo(BaseModel):
    """One entry of `GET /system/models`."""

    name: str
    downloaded: bool
    size_bytes: Optional[int] = None


class ModelsResponse(BaseModel):
    """The body of `GET /system/models`."""

    models: list[ModelInfo]
