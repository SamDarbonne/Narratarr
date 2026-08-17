"""Tests for `narratarr/adapter/ingest.py`. APP-CONTRACT section 7.

A test here never loads a model, never renders real audio, and never
touches the network. `check_drm` calls the real, cheap
`abpipe.extract._check_drm` (pure zipfile + XML parsing, no ML dependency)
against small, hand-built EPUB-shaped zip files — never a real book.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from narratarr.adapter.ingest import (
    IngestError,
    WatchFolder,
    check_drm,
    derive_slug,
    ingest_file,
    unique_slug,
)

# --------------------------------------------------------------------------- fixtures: tiny EPUB-shaped zips


def _write_zip(path: Path, files: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)


def _plain_epub(path: Path) -> None:
    """An EPUB-shaped zip with no `META-INF/encryption.xml` at all -- the
    common case, no DRM."""
    _write_zip(path, {"mimetype": b"application/epub+zip", "META-INF/container.xml": b"<container/>"})


def _drm_epub(path: Path) -> None:
    """An EPUB-shaped zip whose encryption.xml encrypts the actual content
    (not a font) -- real DRM, per pipeline CONTRACT.md 5.4."""
    encryption_xml = b"""<?xml version="1.0"?>
<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"
            xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
  <enc:EncryptedData>
    <enc:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes256-cbc"/>
    <enc:CipherData>
      <enc:CipherReference URI="OEBPS/chapter1.xhtml"/>
    </enc:CipherData>
  </enc:EncryptedData>
</encryption>"""
    _write_zip(
        path,
        {
            "mimetype": b"application/epub+zip",
            "META-INF/encryption.xml": encryption_xml,
            "OEBPS/chapter1.xhtml": b"<html/>",
        },
    )


def _font_obfuscated_epub(path: Path) -> None:
    """encryption.xml present, but it only obfuscates a font -- allowed,
    per pipeline CONTRACT.md 5.4 ('Font obfuscation is normal ... and is
    not DRM')."""
    encryption_xml = b"""<?xml version="1.0"?>
<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"
            xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
  <enc:EncryptedData>
    <enc:EncryptionMethod Algorithm="http://www.idpf.org/2008/embedding"/>
    <enc:CipherData>
      <enc:CipherReference URI="fonts/font1.otf"/>
    </enc:CipherData>
  </enc:EncryptedData>
</encryption>"""
    _write_zip(
        path,
        {
            "mimetype": b"application/epub+zip",
            "META-INF/encryption.xml": encryption_xml,
            "fonts/font1.otf": b"fake font bytes",
        },
    )


# --------------------------------------------------------------------------- check_drm: refuses real DRM only


def test_check_drm_allows_a_plain_epub(tmp_path):
    path = tmp_path / "plain.epub"
    _plain_epub(path)
    check_drm(path)  # must not raise


def test_check_drm_allows_font_obfuscation(tmp_path):
    path = tmp_path / "font-obfuscated.epub"
    _font_obfuscated_epub(path)
    check_drm(path)  # must not raise


def test_check_drm_refuses_real_drm(tmp_path):
    path = tmp_path / "drm.epub"
    _drm_epub(path)
    with pytest.raises(IngestError, match="DRM"):
        check_drm(path)


def test_check_drm_refuses_a_non_zip_file(tmp_path):
    path = tmp_path / "not-a-zip.epub"
    path.write_bytes(b"this is not a zip file at all")
    with pytest.raises(IngestError):
        check_drm(path)


# --------------------------------------------------------------------------- ingest_file: extension, DRM, duplicate


def test_ingest_file_refuses_an_unsupported_extension(tmp_path):
    source = tmp_path / "book.pdf"
    source.write_bytes(b"not an epub")
    with pytest.raises(IngestError, match=r"\.pdf"):
        ingest_file(source, tmp_path / "library", existing_slugs=[], existing_hashes=[])


def test_ingest_file_refuses_a_missing_source(tmp_path):
    with pytest.raises(IngestError, match="not found"):
        ingest_file(tmp_path / "missing.epub", tmp_path / "library", existing_slugs=[], existing_hashes=[])


def test_ingest_file_refuses_drm(tmp_path):
    source = tmp_path / "drm.epub"
    _drm_epub(source)
    with pytest.raises(IngestError, match="DRM"):
        ingest_file(source, tmp_path / "library", existing_slugs=[], existing_hashes=[])
    # A refused file must not have been copied into the library.
    assert not (tmp_path / "library").exists() or list((tmp_path / "library").iterdir()) == []


def test_ingest_file_refuses_a_duplicate_hash_by_default(tmp_path):
    from abpipe.meta import hash_file

    source = tmp_path / "book.epub"
    _plain_epub(source)
    existing_hash = hash_file(source)

    with pytest.raises(IngestError, match="allow_duplicate"):
        ingest_file(
            source, tmp_path / "library", existing_slugs=[], existing_hashes=[existing_hash],
        )


def test_ingest_file_allows_a_duplicate_hash_when_told_to(tmp_path):
    from abpipe.meta import hash_file

    source = tmp_path / "book.epub"
    _plain_epub(source)
    existing_hash = hash_file(source)

    result = ingest_file(
        source, tmp_path / "library", existing_slugs=[], existing_hashes=[existing_hash],
        allow_duplicate=True,
    )
    assert result.path.exists()
    assert result.source_sha256 == existing_hash


def test_ingest_file_copies_into_the_library_and_derives_a_slug(tmp_path):
    source = tmp_path / "The Example Book.epub"
    _plain_epub(source)

    result = ingest_file(
        source, tmp_path / "library", existing_slugs=[], existing_hashes=[], slug_hint="Example Book",
    )
    assert result.slug == "example-book"
    assert result.path == tmp_path / "library" / "example-book.epub"
    assert result.path.read_bytes() == source.read_bytes()
    # the source file survives untouched -- ingest copies, never moves.
    assert source.exists()


def test_ingest_file_handles_a_slug_collision(tmp_path):
    source = tmp_path / "book.epub"
    _plain_epub(source)

    result = ingest_file(
        source, tmp_path / "library", existing_slugs=["example-book", "example-book-2"],
        existing_hashes=[], slug_hint="Example Book",
    )
    assert result.slug == "example-book-3"


def test_ingest_file_never_leaves_a_temp_file_on_a_failed_copy(tmp_path, monkeypatch):
    import shutil

    source = tmp_path / "book.epub"
    _plain_epub(source)

    def _boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(shutil, "copyfile", _boom)
    with pytest.raises(OSError):
        ingest_file(source, tmp_path / "library", existing_slugs=[], existing_hashes=[])

    library_dir = tmp_path / "library"
    leftovers = list(library_dir.iterdir()) if library_dir.exists() else []
    assert leftovers == []


# --------------------------------------------------------------------------- slug helpers


def test_derive_slug_lowercases_and_hyphenates():
    assert derive_slug("The Example Book!") == "the-example-book"


def test_derive_slug_falls_back_to_book_when_nothing_usable():
    assert derive_slug("???") == "book"
    assert derive_slug("") == "book"


def test_unique_slug_returns_the_base_when_no_collision():
    assert unique_slug("example-book", []) == "example-book"


def test_unique_slug_appends_a_numeric_suffix_on_collision():
    assert unique_slug("example-book", ["example-book"]) == "example-book-2"
    assert unique_slug("example-book", ["example-book", "example-book-2"]) == "example-book-3"


# --------------------------------------------------------------------------- WatchFolder: wait for the write to finish


def test_watch_folder_does_not_surface_a_file_still_being_written(tmp_path):
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    target = watch_dir / "book.epub"
    target.write_bytes(b"a")

    watcher = WatchFolder(watch_dir)
    assert watcher.poll() == []  # first sighting: nothing surfaces yet

    target.write_bytes(b"ab")  # the file grew -- still being written
    assert watcher.poll() == []


def test_watch_folder_surfaces_a_file_once_its_size_is_stable_across_two_polls(tmp_path):
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    target = watch_dir / "book.epub"
    target.write_bytes(b"a stable file")

    watcher = WatchFolder(watch_dir)
    assert watcher.poll() == []  # poll 1: first sighting
    assert watcher.poll() == [target]  # poll 2: same size as poll 1 -- stable


def test_watch_folder_never_resurfaces_the_same_unchanged_file(tmp_path):
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    target = watch_dir / "book.epub"
    target.write_bytes(b"a stable file")

    watcher = WatchFolder(watch_dir)
    watcher.poll()
    assert watcher.poll() == [target]
    # The file is untouched (never ingested and deleted, the default) --
    # it must not surface again on a third, fourth, ... poll.
    assert watcher.poll() == []
    assert watcher.poll() == []


def test_watch_folder_resurfaces_a_file_that_changes_after_being_surfaced(tmp_path):
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    target = watch_dir / "book.epub"
    target.write_bytes(b"version one")

    watcher = WatchFolder(watch_dir)
    watcher.poll()
    assert watcher.poll() == [target]

    target.write_bytes(b"a completely different, longer version two")
    assert watcher.poll() == []  # size changed: treated as mid-write again
    assert watcher.poll() == [target]  # stable again at the new size


def test_watch_folder_forget_lets_a_reused_name_surface_again(tmp_path):
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    target = watch_dir / "book.epub"
    target.write_bytes(b"version one")

    watcher = WatchFolder(watch_dir)
    watcher.poll()
    assert watcher.poll() == [target]

    target.unlink()
    watcher.forget(target)

    target.write_bytes(b"version one")  # same bytes, dropped again under the same name
    assert watcher.poll() == []
    assert watcher.poll() == [target]


def test_watch_folder_ignores_a_missing_watch_dir(tmp_path):
    watcher = WatchFolder(tmp_path / "does-not-exist")
    assert watcher.poll() == []


def test_watch_folder_surfaces_every_stable_extension_not_only_epub(tmp_path):
    """APP-CONTRACT 7: an unsupported extension makes a FAILED job, never a
    silent skip -- so WatchFolder itself must not filter by extension; that
    is `ingest_file`'s job."""
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    target = watch_dir / "notes.pdf"
    target.write_bytes(b"not an epub")

    watcher = WatchFolder(watch_dir)
    watcher.poll()
    assert watcher.poll() == [target]
