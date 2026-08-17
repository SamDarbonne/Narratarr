"""The Audiobookshelf target. APP-CONTRACT section 8.2. Owner: W2.

Copies the book with the folder target first, triggers a library scan over
the Audiobookshelf HTTP API, then polls the library items list until the
book appears — paginating through EVERY page, never trusting one page size.

`vendor/abpipe/abpipe/deliver.py`'s module docstring records the confirmed
API shape and the measured pagination fault this module exists to avoid: a
fixed `limit=500` call silently truncated to the first 500 items once a real
library reached 561, the new book landed on the second page, and the poll
could never succeed however long it waited — a correct delivery reported as
a failure, forever. This module reuses that knowledge (the item shape, the
query parameters, the pagination contract) and NOT that module's code: this
target speaks HTTP directly (httpx), never SSH — APP-CONTRACT 3.1 forbids
Narratarr from reading `absdatabase.sqlite` or shelling out to `.80`.

APP-CONTRACT 10.2: the Audiobookshelf token is read from the environment
variable named by `token_env`, at the moment of use. It never enters the
database, a log line, or the return value of any function in this module —
`validate()` and every DeliveryResult below name the *variable*, never the
token itself.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable

import httpx

from narratarr.adapter.targets.base import DeliverBook, DeliveryResult, Progress, TargetError
from narratarr.adapter.targets.folder import FolderTarget

# vendor/abpipe/abpipe/deliver.py's own measured constants (module
# docstring, and CONTRACT.md section 12 step 4): a page size is a batch
# size for the paging loop, never an assumed ceiling. MAX_ITEM_PAGES *
# ITEMS_PAGE_SIZE = 100,000 items — a hard safety cap, so a malformed or
# looping server response cannot spin the poll forever.
ITEMS_PAGE_SIZE = 500
MAX_ITEM_PAGES = 200

POLL_TIMEOUT_S = 300.0
POLL_INITIAL_DELAY_S = 2.0
POLL_MAX_DELAY_S = 20.0
POLL_BACKOFF = 1.5

# 5 percent, per APP-CONTRACT 8.2 rule 6 / pipeline CONTRACT.md section 12 step 5.
DURATION_TOLERANCE = 0.05


def _within_tolerance(expected: float, got: float, tolerance: float = DURATION_TOLERANCE) -> bool:
    """Return True when `got` is within `tolerance` (a fraction) of `expected`."""
    if expected <= 0:
        return got == expected
    return abs(got - expected) <= tolerance * expected


def _auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _items_url(base_url: str, library_id: str, page: int) -> str:
    return f"{base_url}/api/libraries/{library_id}/items?limit={ITEMS_PAGE_SIZE}&page={page}"


def _fetch_items_page(client: httpx.Client, base_url: str, library_id: str, token: str, page: int) -> list[dict]:
    """Fetch one page of the library items list.

    Returns `[]` for a missing or malformed `results` field, or a
    non-2xx/unparsable response, rather than raising — a garbled page reads
    as "no items here" to the pagination loop, per
    `vendor/abpipe/abpipe/deliver.py`'s own `_fetch_items_page`.
    """
    try:
        resp = client.get(_items_url(base_url, library_id, page), headers=_auth_header(token), timeout=30.0)
    except httpx.HTTPError:
        return []
    if resp.status_code != 200:
        return []
    try:
        data = resp.json()
    except ValueError:
        return []
    results = data.get("results") if isinstance(data, dict) else None
    return results if isinstance(results, list) else []


def _find_item(items: list[dict], title: str) -> dict | None:
    for item in items:
        meta = (item.get("media") or {}).get("metadata") or {}
        if meta.get("title") == title:
            return item
    return None


def find_item_across_pages(client: httpx.Client, base_url: str, library_id: str, token: str, title: str) -> dict | None:
    """Walk every page of the items list looking for `title`.

    Stops as soon as the item is found, a page comes back empty (the
    library is exhausted), or a page reports the exact same item ids as the
    page before it (a malformed or looping server response) — and always
    stops after MAX_ITEM_PAGES regardless, so this loop cannot spin forever
    no matter what the server sends back. Mirrors
    `vendor/abpipe/abpipe/deliver.py`'s `_find_item_across_pages` exactly,
    over HTTP instead of over SSH.
    """
    previous_ids: frozenset | None = None
    for page in range(MAX_ITEM_PAGES):
        items = _fetch_items_page(client, base_url, library_id, token, page)
        if not items:
            return None
        found = _find_item(items, title)
        if found is not None:
            return found
        current_ids = frozenset(item.get("id") for item in items)
        if current_ids == previous_ids:
            return None
        previous_ids = current_ids
    return None


def _poll_for_item(
    client: httpx.Client,
    base_url: str,
    library_id: str,
    token: str,
    title: str,
    timeout: float,
    progress: Callable[[Progress], None] | None,
    sleep=time.sleep,
    now=time.monotonic,
) -> dict:
    """Poll the library items list until `title` appears. Raise TargetError on timeout."""
    deadline = now() + timeout
    delay = POLL_INITIAL_DELAY_S
    while now() < deadline:
        found = find_item_across_pages(client, base_url, library_id, token, title)
        if found is not None:
            return found
        if progress is not None:
            progress(
                Progress(
                    stage="deliver", done=0, total=0,
                    message=f"waiting for {title!r} to appear in Audiobookshelf library {library_id}",
                )
            )
        sleep(delay)
        delay = min(delay * POLL_BACKOFF, POLL_MAX_DELAY_S)
    raise TargetError(
        f"audiobookshelf target: item {title!r} did not appear in library {library_id} "
        f"within {timeout:.0f}s"
    )


def _verify_item(item: dict, book: DeliverBook) -> None:
    """Raise TargetError when the delivered item does not match `book`.
    APP-CONTRACT 8.2 rule 6: title, author, chapter count, and duration
    (within 5 percent).
    """
    meta = (item.get("media") or {}).get("metadata") or {}
    media = item.get("media") or {}

    if meta.get("title") != book.title:
        raise TargetError(
            f"audiobookshelf target: title mismatch — expected {book.title!r}, got {meta.get('title')!r}"
        )
    if meta.get("authorName") != book.author:
        raise TargetError(
            f"audiobookshelf target: author mismatch — expected {book.author!r}, got {meta.get('authorName')!r}"
        )
    if media.get("numChapters") != book.chapters:
        raise TargetError(
            f"audiobookshelf target: chapter count mismatch — expected {book.chapters}, "
            f"got {media.get('numChapters')}"
        )
    got_duration = media.get("duration")
    if got_duration is None:
        raise TargetError("audiobookshelf target: item reports no duration")
    if not _within_tolerance(book.duration_s, float(got_duration)):
        raise TargetError(
            f"audiobookshelf target: duration mismatch — expected close to {book.duration_s:.1f}s, "
            f"got {got_duration:.1f}s (tolerance {DURATION_TOLERANCE:.0%})"
        )


class AudiobookshelfTarget:
    """`kind: "audiobookshelf"`. APP-CONTRACT 8.2.

    `config["folder_target"]` must be an embedded folder-target config
    object (`{"root": ..., "layout": ..., "copy_cover": ...}`), not a
    target name. APP-CONTRACT 8.2 writes `"folder_target": "…"`, the same
    ellipsis style it uses for a plain string field — this worker's report
    flags that as ambiguous: a target's `validate`/`deliver` take only
    `config: dict` and a `DeliverBook` (APP-CONTRACT section 8), with no
    database handle to resolve a target *name* into its stored config. A
    caller (W3's `api/targets.py`) that holds a target name must resolve it
    to its config and embed it here before calling this target.
    """

    kind = "audiobookshelf"

    def validate(self, config: dict) -> None:
        if not isinstance(config, dict):
            raise ValueError("audiobookshelf target: config must be an object")
        for key in ("base_url", "library_id", "token_env"):
            value = config.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"audiobookshelf target: {key!r} must be a non-empty string")
        folder_config = config.get("folder_target")
        if not isinstance(folder_config, dict):
            raise ValueError(
                "audiobookshelf target: 'folder_target' must be an embedded folder-target "
                "config object with 'root' (see this target's class docstring) — a target "
                "name must be resolved to its config before it reaches this class"
            )
        FolderTarget().validate(folder_config)

    def _token(self, config: dict) -> str:
        """Read the token from the environment. Never store it, log it, or
        return it — APP-CONTRACT 10.2."""
        env_name = config["token_env"]
        token = os.environ.get(env_name)
        if not token:
            raise ValueError(
                f"audiobookshelf target: environment variable {env_name!r} is unset. "
                "APP-CONTRACT 10.2: a secret is read from the environment at the moment "
                "of use — set it before delivering to this target."
            )
        return token

    def test(self, config: dict) -> DeliveryResult:
        """Check the library is reachable. Writes nothing — GET only."""
        self.validate(config)
        try:
            token = self._token(config)
        except ValueError as exc:
            return DeliveryResult(ok=False, message=str(exc))
        base_url = config["base_url"].rstrip("/")
        url = f"{base_url}/api/libraries/{config['library_id']}"
        try:
            with httpx.Client() as client:
                resp = client.get(url, headers=_auth_header(token), timeout=10.0)
        except httpx.HTTPError as exc:
            return DeliveryResult(ok=False, message=f"audiobookshelf target: {exc}")
        if resp.status_code != 200:
            return DeliveryResult(
                ok=False, message=f"audiobookshelf target: HTTP {resp.status_code} from {url}"
            )
        return DeliveryResult(ok=True, message=f"library {config['library_id']} is reachable")

    def deliver(
        self,
        config: dict,
        book: DeliverBook,
        progress: Callable[[Progress], None] | None = None,
    ) -> DeliveryResult:
        """Copy with the folder target, scan, then poll and verify. Idempotent:
        a second delivery copies the same file over the same path (the
        folder target's own idempotence) and polls/verifies again."""
        self.validate(config)
        folder_result = FolderTarget().deliver(config["folder_target"], book, progress=progress)
        if not folder_result.ok:
            return folder_result

        token = self._token(config)
        base_url = config["base_url"].rstrip("/")
        library_id = config["library_id"]

        with httpx.Client() as client:
            scan_url = f"{base_url}/api/libraries/{library_id}/scan"
            try:
                client.post(scan_url, headers=_auth_header(token), timeout=30.0)
            except httpx.HTTPError as exc:
                raise TargetError(f"audiobookshelf target: could not trigger the scan: {exc}") from exc

            if progress is not None:
                progress(Progress(stage="deliver", done=0, total=0, message="scan triggered; waiting for the item"))

            item = _poll_for_item(client, base_url, library_id, token, book.title, POLL_TIMEOUT_S, progress)
            _verify_item(item, book)

        if progress is not None:
            progress(Progress(stage="deliver", done=1, total=1, message="verified in Audiobookshelf"))

        return DeliveryResult(
            ok=True,
            remote_ref=item["id"],
            url=f"{base_url}/item/{item['id']}",
            bytes=folder_result.bytes,
            message=f"delivered and verified in Audiobookshelf library {library_id}",
        )

    def deliver_fix(
        self,
        config: dict,
        book: DeliverBook,
        progress: Callable[[Progress], None] | None = None,
    ) -> DeliveryResult:
        """Re-deliver after a small correction. APP-CONTRACT 9.5. Same steps
        as `deliver` — the copy, the scan, and the poll are each already
        idempotent, so a Fix re-delivery is the same operation."""
        return self.deliver(config, book, progress=progress)
