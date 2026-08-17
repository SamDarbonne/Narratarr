"""The Context object that every stage receives.

CONTRACT.md section 13 defines this class.
This module is a kernel file. Only the overlord edits it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from abpipe import STAGE_DIRS
from abpipe.meta import read_json, write_json

# A neutral placeholder. Nothing in this copy depends on either value:
# `Context` is always constructed with an explicit `slug` and `epub`, and
# `cli.py`, the only caller that ever relied on a default, is not vendored.
# The originals named a real book and its author in an executable default,
# where a comment-level redaction would never have found them.
DEFAULT_EPUB = "source/example-book.epub"
DEFAULT_SLUG = "example-book"


def project_root() -> Path:
    """Return the root of the project repository."""
    return Path(__file__).resolve().parent.parent


@dataclass
class Context:
    """The paths and the manifest of one book."""

    root: Path = field(default_factory=project_root)
    slug: str = DEFAULT_SLUG
    epub: Path | None = None
    book: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        if self.epub is None:
            self.epub = self.root / DEFAULT_EPUB
        self.epub = Path(self.epub)
        if not self.epub.is_absolute():
            self.epub = self.root / self.epub
        if not self.book:
            self.load_book()

    # ------------------------------------------------------------------ paths

    @property
    def work_dir(self) -> Path:
        """Return the directory that holds every book."""
        return self.root / "work"

    @property
    def book_dir(self) -> Path:
        """Return the directory of this book."""
        return self.work_dir / self.slug

    @property
    def book_json(self) -> Path:
        """Return the path of book.json."""
        return self.book_dir / "book.json"

    @property
    def cover_path(self) -> Path:
        """Return the path of the cover image."""
        return self.book_dir / (self.book.get("cover") or "cover.jpg")

    @property
    def qc_config_path(self) -> Path:
        """Return the path of qc-config.json."""
        return self.book_dir / "qc-config.json"

    @property
    def log_dir(self) -> Path:
        """Return the log directory. The directory is made if it is absent."""
        path = self.book_dir / "logs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def stage_dir(self, stage: str, make: bool = True) -> Path:
        """Return the directory of a stage. The directory is made if it is absent."""
        if stage not in STAGE_DIRS:
            raise KeyError(f"unknown stage: {stage}")
        path = self.book_dir / STAGE_DIRS[stage]
        if make:
            path.mkdir(parents=True, exist_ok=True)
        return path

    # ------------------------------------------------------------------ manifest

    def load_book(self) -> dict:
        """Read book.json into the context. Return the manifest."""
        data = read_json(self.book_json)
        self.book = data if isinstance(data, dict) else {}
        return self.book

    def save_book(self, book: dict) -> None:
        """Write book.json. Only stage 1 calls this method."""
        self.book_dir.mkdir(parents=True, exist_ok=True)
        write_json(self.book_json, book)
        self.book = book

    def chapters(self) -> list[dict]:
        """Return the chapter records of book.json, in order."""
        return list(self.book.get("chapters") or [])

    def chapter_ids(self, only: list[str] | None = None) -> list[str]:
        """Return the chapter ids, in order.

        The argument `only` restricts the result to the named ids. An unknown id
        raises a KeyError, so a typed chapter name never fails silently.
        """
        ids = [c["id"] for c in self.chapters()]
        if only is None:
            return ids
        unknown = [i for i in only if i not in ids]
        if unknown:
            raise KeyError(f"unknown chapter id: {', '.join(unknown)}")
        return [i for i in ids if i in set(only)]

    def chapter(self, chapter_id: str) -> dict:
        """Return one chapter record."""
        for record in self.chapters():
            if record["id"] == chapter_id:
                return record
        raise KeyError(f"unknown chapter id: {chapter_id}")

    @property
    def engine_config(self) -> dict:
        """Return the engine block of book.json."""
        return dict(self.book.get("engine") or {})

    @property
    def title(self) -> str:
        """Return the corrected title of the book."""
        return self.book.get("title") or self.slug

    @property
    def author(self) -> str:
        """Return the author of the book."""
        return self.book.get("author") or "Unknown"
