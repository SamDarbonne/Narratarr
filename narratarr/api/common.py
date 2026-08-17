"""The seam every API route shares.

APP-CONTRACT.md section 14.1 gives the exact signature of every name below.
Every router this worker's routes, and every router W3 builds, imports from
here rather than duplicating an error type or a pagination shape.
"""

from __future__ import annotations

from typing import Optional

from fastapi import Request

from narratarr.api import auth
from narratarr.db import transaction
from narratarr.models import ApiKeyRow


class ApiError(Exception):
    """One API error. `api/__init__.py` installs the handler that renders it.

    APP-CONTRACT.md section 13: every error response is
    `{"error": {"code": "...", "message": "...", "detail": {...}}}`.
    """

    def __init__(
        self, code: str, message: str, status: int = 400, detail: Optional[dict] = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail or {}


async def require_key(request: Request) -> ApiKeyRow:
    """The FastAPI dependency that checks the `X-Api-Key` header.

    APP-CONTRACT.md section 10.1: the key never goes into a URL, because a
    URL enters a log, a browser history, and a referrer header. This
    function reads only the `X-Api-Key` header. A caller that sends
    `?apikey=` instead of the header gets the same 401 as a caller who
    sends nothing at all.
    """
    raw_key = request.headers.get("X-Api-Key", "")
    if not raw_key:
        raise ApiError("unauthorized", "the X-Api-Key header is required", status=401)
    with transaction() as conn:
        key_row = auth.verify_key(conn, raw_key)
    if key_row is None:
        raise ApiError("unauthorized", "the API key is not valid", status=401)
    return key_row


def paginate(items: list, limit: int, offset: int) -> dict:
    """Return the `{"items", "total", "limit", "offset"}` envelope of section 13.

    `items` is the full, unsliced result set. This function slices to the
    requested page and reports the full length as `total`.
    """
    total = len(items)
    page = items[offset : offset + limit]
    return {"items": page, "total": total, "limit": limit, "offset": offset}
