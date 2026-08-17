"""The target interface. APP-CONTRACT section 8. Owner: W2.

A target delivers one finished book to one destination: a folder on disk, or
an Audiobookshelf library. Every target obeys the same small interface, so
the runner and the API never need to know which kind of target they hold.

This module imports nothing from `abpipe`. A target module only ever reads
the finished m4b and the cover image that the adapter already produced; it
never reads `abpipe` internals.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class DeliverBook:
    """The finished book, as a target needs it. APP-CONTRACT section 8."""

    slug: str
    title: str
    author: str
    year: str | None
    genre: str | None
    m4b: Path
    cover: Path | None
    duration_s: float
    chapters: int


@dataclass(frozen=True)
class DeliveryResult:
    """The result of one delivery attempt. APP-CONTRACT section 8."""

    ok: bool
    remote_ref: str | None = None
    url: str | None = None
    bytes: int = 0
    message: str = ""


class TargetError(Exception):
    """A target cannot deliver, verify, or reach its destination.

    Raised by `deliver()`, `deliver_fix()`, and `test()` on a fault they
    cannot recover from. `validate()` raises `ValueError` instead — see
    that method's own contract below — so a caller can tell "the
    configuration itself is wrong" apart from "the configuration is fine,
    but the delivery failed."
    """


@dataclass(frozen=True)
class Progress:
    """One delivery-progress event. Mirrors `narratarr.adapter.Progress`,
    repeated here so `targets/` never has to import `narratarr.adapter`
    (which would create a needless import cycle: `adapter/__init__.py`
    does not import `targets/`, but a target module importing back from
    it would still be a cycle waiting to happen the day it does).
    """

    stage: str
    done: int
    total: int
    message: str = field(default="")


class Target(Protocol):
    """The interface every target obeys. APP-CONTRACT section 8.

    Every target is idempotent: a second delivery of the same book copies
    nothing new and verifies again.
    """

    kind: str

    def validate(self, config: dict) -> None:
        """Raise ValueError when the configuration is wrong. Never touch the network."""
        ...

    def test(self, config: dict) -> DeliveryResult:
        """Check that the target is reachable. Write nothing."""
        ...

    def deliver(
        self,
        config: dict,
        book: DeliverBook,
        progress: Callable[[Progress], None] | None = None,
    ) -> DeliveryResult:
        """Deliver `book` to this target. Idempotent."""
        ...

    def deliver_fix(
        self,
        config: dict,
        book: DeliverBook,
        progress: Callable[[Progress], None] | None = None,
    ) -> DeliveryResult:
        """Re-deliver after a small, post-delivery correction. APP-CONTRACT 9.5."""
        ...


def copy_atomic(source: Path, dest: Path) -> None:
    """Copy `source` to `dest`. The write is atomic.

    A temporary file is written beside `dest`, in `dest`'s own directory,
    then moved into place with `os.replace` (APP-CONTRACT house rule 15.5:
    "Every file write is atomic"). A reader of `dest` never sees a
    half-copied file. A failed copy removes its own temporary file.

    Shared by every target module under `targets/` — one copy routine, so
    the atomicity rule is enforced in exactly one place.
    """
    import os
    import shutil

    dest = Path(dest)
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
