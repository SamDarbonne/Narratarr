"""Ingest: the watch folder, the upload, and the path. APP-CONTRACT section 7.

Owner: W2.

Three ways a book enters Narratarr, per APP-CONTRACT section 7: the watch
folder, an upload through `POST /api/v1/jobs`, and a `source_path` on that
same route. All three converge on `ingest_file()`: copy the source into
`/config/library`, and never render from `/watch` — the copy is what makes
the source stable while a render runs, minutes or hours later.

This module imports `abpipe` only for `abpipe.meta.hash_file` (the one
sanctioned hash function, pipeline CONTRACT.md section 3.2 — this module
never writes its own hash code) and for the DRM check, both lazily, inside
function bodies, so importing this module never pulls torch.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

SUPPORTED_EXTENSION = ".epub"

# A slug segment: lower case, digits, and hyphens only. Pipeline CONTRACT.md
# section 4: "slug: The name of the book directory. Lower case. Hyphens
# only."
_SLUG_UNSAFE_RE = re.compile(r"[^a-z0-9]+")


class IngestError(Exception):
    """A file cannot be ingested. The message names the reason — APP-CONTRACT
    section 7: "never a silent skip." """


@dataclass(frozen=True)
class IngestResult:
    """One file, successfully copied into `/config/library`."""

    path: Path
    slug: str
    source_sha256: str


# --------------------------------------------------------------------------- slugs


def derive_slug(text: str) -> str:
    """Return `text` as a slug: lower case, hyphens only.

    Every run of characters that is not a lower-case letter or a digit
    becomes one hyphen; a leading or trailing hyphen is stripped. An input
    that leaves nothing usable (an EPUB filename of pure punctuation, or an
    empty title) falls back to `"book"`, so a slug is never empty — an
    empty slug would collide with every other book that also produced one.
    """
    lowered = (text or "").lower()
    slug = _SLUG_UNSAFE_RE.sub("-", lowered).strip("-")
    return slug or "book"


def unique_slug(base_slug: str, existing: Iterable[str]) -> str:
    """Return `base_slug`, or `base_slug-2`, `base_slug-3`, ... on a collision.

    APP-CONTRACT section 4.2: "A collision gets a numeric suffix." The
    search starts at 2, not 1 — `base_slug` itself is the first, unnumbered
    attempt.
    """
    existing_set = set(existing)
    if base_slug not in existing_set:
        return base_slug
    n = 2
    while f"{base_slug}-{n}" in existing_set:
        n += 1
    return f"{base_slug}-{n}"


# --------------------------------------------------------------------------- DRM


def check_drm(epub_path: Path) -> None:
    """Raise IngestError when `epub_path` holds real DRM.

    APP-CONTRACT section 7: "`abpipe/extract.py` already refuses one
    (pipeline contract 5.4). Narratarr never circumvents DRM. Reuse the
    upstream check; do not write your own." `abpipe.extract._check_drm` is
    that check — it reads `META-INF/encryption.xml` and allows font
    obfuscation, which is normal in a retail EPUB and is not DRM (pipeline
    CONTRACT.md 5.4). It is not exported as a public name; this worker's
    report flags that to the overlord as a case for a public
    `abpipe.extract.check_drm(zf, epub_path)` the day a second caller (this
    one) needs it standalone, ahead of any other stage 1 work.

    Raises IngestError, not abpipe's own ValueError, so every fault this
    module raises has the one exception type ingest.py's callers need to
    catch.
    """
    import zipfile

    from abpipe.extract import _check_drm

    try:
        with zipfile.ZipFile(epub_path, "r") as zf:
            _check_drm(zf, epub_path)
    except ValueError as exc:
        raise IngestError(str(exc)) from exc
    except zipfile.BadZipFile as exc:
        raise IngestError(f"{epub_path}: not a valid EPUB (not a zip archive): {exc}") from exc


# --------------------------------------------------------------------------- copy


def _copy_atomic(source: Path, dest: Path) -> None:
    """Copy `source` to `dest`. The write is atomic — a temporary file in
    `dest`'s own directory, then `os.replace`. A failed copy removes its
    own temporary file. APP-CONTRACT house rule 15.5."""
    import shutil

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / (dest.name + ".narratarr.tmp")
    try:
        shutil.copyfile(source, tmp)
        os.replace(tmp, dest)
    except BaseException:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


# --------------------------------------------------------------------------- ingest


def ingest_file(
    source: Path,
    library_dir: Path,
    existing_slugs: Iterable[str],
    existing_hashes: Iterable[str],
    slug_hint: str | None = None,
    allow_duplicate: bool = False,
) -> IngestResult:
    """Copy `source` into `library_dir`. APP-CONTRACT section 7.

    `source` must already be a STABLE file — a finished upload, a finished
    watch-folder write (see `WatchFolder` below), or a caller-given path.
    This function does no waiting of its own.

    Refuses, and raises `IngestError`, for:
      - an extension other than `.epub` (APP-CONTRACT 7: "the supported
        input is EPUB. An unsupported extension makes a `failed` job with a
        message that names the extension, never a silent skip");
      - a missing source file;
      - a DRM-protected EPUB (`check_drm`, above);
      - a source whose sha256 already appears in `existing_hashes`, unless
        `allow_duplicate` is set (APP-CONTRACT 4.2: "the API refuses the
        second one unless the caller sets `allow_duplicate`").

    The returned slug is derived from `slug_hint` (a caller-given title) or,
    when absent, from the source file's own stem, and is made unique
    against `existing_slugs`.
    """
    from abpipe.meta import hash_file

    source = Path(source)
    if source.suffix.lower() != SUPPORTED_EXTENSION:
        raise IngestError(
            f"{source.name}: unsupported file type {source.suffix or '(none)'!r}. "
            f"Narratarr accepts {SUPPORTED_EXTENSION} files only."
        )
    if not source.exists():
        raise IngestError(f"{source}: file not found")

    check_drm(source)

    source_sha256 = hash_file(source)
    if not allow_duplicate and source_sha256 in set(existing_hashes):
        raise IngestError(
            f"{source.name}: a job already holds this exact file "
            f"(sha256={source_sha256}); pass allow_duplicate to ingest it again"
        )

    base_slug = derive_slug(slug_hint or source.stem)
    slug = unique_slug(base_slug, existing_slugs)

    library_dir = Path(library_dir)
    dest = library_dir / f"{slug}.epub"
    _copy_atomic(source, dest)

    return IngestResult(path=dest, slug=slug, source_sha256=source_sha256)


# --------------------------------------------------------------------------- the watch folder


class WatchFolder:
    """Poll `/watch` for a stable, not-yet-surfaced file. APP-CONTRACT section 7.

    `poll()` is meant to be called once per `NARRATARR_WATCH_INTERVAL_S`
    tick, by the runner's own timer — this class never sleeps and never
    blocks.

    Two rules, both from APP-CONTRACT section 7:

    1. **Wait for the write to finish.** A file is surfaced only once its
       size is the same on two consecutive polls — the first poll that
       sees a given size just records it; the file surfaces on the *next*
       poll, if the size held. A file mid-copy is never surfaced.
    2. **Never re-surface the same file twice**, unless it changes. Once
       `poll()` has returned a path, that exact size is remembered as
       "already surfaced" — a `watch_delete_after_ingest=false` book (the
       default) stays in `/watch` forever, and without this a stable file
       would be returned again on every future poll. A file that later
       changes size (a person drops a new version under the same name) is
       treated as new and surfaces again once it re-stabilises.
    """

    def __init__(self, watch_dir: Path) -> None:
        self.watch_dir = Path(watch_dir)
        self._sizes: dict[str, int] = {}
        self._surfaced: dict[str, int] = {}

    def poll(self) -> list[Path]:
        """Return every file that is stable and not already surfaced at its
        current size, in sorted order. Returns `[]`, never raises, when
        `watch_dir` cannot be listed (absent, or a permission fault) — a
        missing watch folder is not this class's fault to report."""
        current: dict[str, int] = {}
        try:
            entries = sorted(self.watch_dir.iterdir())
        except OSError:
            return []

        result: list[Path] = []
        for path in entries:
            if not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            key = str(path)
            current[key] = size

            previous_size = self._sizes.get(key)
            if previous_size is None or previous_size != size:
                continue  # first time seen, or still being written
            if self._surfaced.get(key) == size:
                continue  # already surfaced this exact version
            self._surfaced[key] = size
            result.append(path)

        self._sizes = current
        # A vanished file (moved, or deleted by watch_delete_after_ingest)
        # loses its "already surfaced" record, so a future file dropped
        # under the same name is treated as new.
        for key in list(self._surfaced):
            if key not in current:
                del self._surfaced[key]

        return result

    def forget(self, path: Path) -> None:
        """Drop bookkeeping for `path`. Call after a caller-driven delete
        (`watch_delete_after_ingest`), so a future file dropped under the
        same name surfaces as new without waiting for `poll()` to notice
        the removal on its own. This method touches no file — it is
        bookkeeping only; the actual delete is the caller's decision."""
        key = str(Path(path))
        self._sizes.pop(key, None)
        self._surfaced.pop(key, None)
