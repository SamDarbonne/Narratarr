"""The folder target. APP-CONTRACT section 8.1. Owner: W2.

The default target. It writes the m4b and the cover under a configurable
layout, under a configurable root. It serves Audiobookshelf, Plex, and any
other reader that watches a directory — this is the target that makes
Narratarr useful to a stranger with no other target configured.

This module imports nothing from `abpipe`. It only reads the finished m4b
and cover paths the adapter already produced.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path

from narratarr.adapter.targets.base import (
    DeliverBook,
    DeliveryResult,
    Progress,
    TargetError,
    copy_atomic,
)

DEFAULT_LAYOUT = "{author}/{title}/{title}.m4b"

# A layout placeholder name is one of these. Any other name in the template
# is a configuration mistake, caught by validate() before a delivery ever
# starts — CONTRACT.md house rule: never touch the network or the disk from
# a bad config.
_ALLOWED_FIELDS = frozenset({"slug", "title", "author", "year", "genre"})

# A character that cannot survive as one path segment. `/` and the OS
# separator would inject an extra path segment out of one placeholder's
# value; a NUL, a control character, or a trailing dot causes real trouble on
# at least one common filesystem. Kept narrow and explicit, not a blocklist
# of "everything risky" — the goal is one placeholder value staying one path
# segment, never zero and never two.
_UNSAFE_CHARS_RE = re.compile(r"[\x00-\x1f/\\]")


def _safe_component(value: str) -> str:
    """Return `value`, made safe as ONE path segment of a layout template.

    A book's title or author name is untrusted external data — it comes
    from the EPUB's own metadata, or from a person typing into a form. Two
    defences, applied in this method and backstopped by `_resolve_dest`'s
    own `root`-containment check below:

    1. Every `/` or `\\` character is replaced with `-`, so a value like
       `"../../etc"` can never inject extra path segments through a single
       placeholder — the template's own literal slashes are the only real
       segment boundaries; a filled-in value must never add more.
    2. A value that is empty, or that is exactly `.` or `..` once
       sanitised, is replaced with `_` — a bare `..` component has no
       slash in it at all, so rule 1 alone does not stop it from walking
       one directory up when the template places it as its own segment.
    """
    cleaned = _UNSAFE_CHARS_RE.sub("-", value or "").strip()
    if cleaned in ("", ".", ".."):
        return "_"
    return cleaned


def _resolve_dest(root: Path, layout: str, book: DeliverBook) -> Path:
    """Return the resolved m4b path for `book`, filled from `layout`.

    Raises TargetError when the resolved path does not stay under `root`.
    `_safe_component` already keeps one placeholder's value to one path
    segment with no `..`, so this check is the backstop, not the only
    defence — a caller must never rely on `_safe_component` alone.
    """
    fields = {
        "slug": _safe_component(book.slug),
        "title": _safe_component(book.title),
        "author": _safe_component(book.author),
        "year": _safe_component(book.year or ""),
        "genre": _safe_component(book.genre or ""),
    }
    try:
        rel = layout.format(**fields)
    except (KeyError, IndexError) as exc:
        raise TargetError(f"folder target: layout {layout!r} names an unknown field: {exc}") from exc

    root_resolved = Path(root).resolve()
    dest = (root_resolved / rel).resolve()
    try:
        dest.relative_to(root_resolved)
    except ValueError:
        raise TargetError(
            f"folder target: layout {layout!r} for book {book.title!r} resolves to "
            f"{dest}, which is outside root {root_resolved} — refusing to write it"
        )
    return dest


class FolderTarget:
    """`kind: "folder"`. APP-CONTRACT 8.1."""

    kind = "folder"

    def validate(self, config: dict) -> None:
        """Raise ValueError when the configuration is wrong. Touches no path, no network."""
        if not isinstance(config, dict):
            raise ValueError("folder target: config must be an object")
        root = config.get("root")
        if not isinstance(root, str) or not root.strip():
            raise ValueError("folder target: 'root' must be a non-empty string")
        layout = config.get("layout", DEFAULT_LAYOUT)
        if not isinstance(layout, str) or not layout.strip():
            raise ValueError("folder target: 'layout' must be a non-empty string")
        # format_map with a permissive dict that echoes back any name asked
        # for: this checks the template names ONLY the fields this target
        # understands, without needing a real DeliverBook to try it against.
        try:
            layout.format(**{f: "" for f in _ALLOWED_FIELDS})
        except (KeyError, IndexError) as exc:
            raise ValueError(f"folder target: layout {layout!r} names an unknown field: {exc}") from exc
        copy_cover = config.get("copy_cover", True)
        if not isinstance(copy_cover, bool):
            raise ValueError("folder target: 'copy_cover' must be a boolean")

    def test(self, config: dict) -> DeliveryResult:
        """Check the root (or its nearest existing ancestor) is writable. Writes nothing."""
        self.validate(config)
        root = Path(config["root"])
        check_dir = root
        while not check_dir.exists():
            parent = check_dir.parent
            if parent == check_dir:
                break
            check_dir = parent
        if not os.access(check_dir, os.W_OK):
            return DeliveryResult(ok=False, message=f"folder target: {check_dir} is not writable")
        return DeliveryResult(ok=True, message=f"{check_dir} is writable")

    def deliver(
        self,
        config: dict,
        book: DeliverBook,
        progress: Callable[[Progress], None] | None = None,
    ) -> DeliveryResult:
        """Write the m4b, and the cover when configured. Idempotent: a repeat
        overwrites the same destination with the same content and reports
        the same result — nothing accumulates, nothing is skipped silently."""
        self.validate(config)
        root = Path(config["root"])
        layout = config.get("layout", DEFAULT_LAYOUT)
        copy_cover = config.get("copy_cover", True)

        dest = _resolve_dest(root, layout, book)
        if progress is not None:
            progress(Progress(stage="deliver", done=0, total=1, message=f"writing {dest}"))

        copy_atomic(book.m4b, dest)
        total_bytes = dest.stat().st_size

        if copy_cover and book.cover is not None and Path(book.cover).exists():
            cover_dest = dest.with_name(dest.stem + Path(book.cover).suffix)
            copy_atomic(book.cover, cover_dest)
            total_bytes += cover_dest.stat().st_size

        if progress is not None:
            progress(Progress(stage="deliver", done=1, total=1, message=f"wrote {dest}"))

        return DeliveryResult(ok=True, remote_ref=str(dest), url=None, bytes=total_bytes, message=f"wrote {dest}")

    def deliver_fix(
        self,
        config: dict,
        book: DeliverBook,
        progress: Callable[[Progress], None] | None = None,
    ) -> DeliveryResult:
        """Re-deliver after a small correction. APP-CONTRACT 9.5.

        The folder target holds no partial-update path — every delivery
        already overwrites the destination atomically with the complete,
        current book — so a Fix re-delivery is the same operation as a
        first delivery.
        """
        return self.deliver(config, book, progress=progress)
