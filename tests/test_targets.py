"""Tests for `narratarr/adapter/targets/`. APP-CONTRACT section 8.

A test here never touches the network. The Audiobookshelf target's HTTP
calls go through `httpx.MockTransport` — a fake server, in-process, with no
socket ever opened.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from narratarr.adapter.targets import registry
from narratarr.adapter.targets.audiobookshelf import AudiobookshelfTarget, find_item_across_pages
from narratarr.adapter.targets.base import DeliverBook, TargetError
from narratarr.adapter.targets.folder import FolderTarget

# Captured before any test monkeypatches `httpx.Client` -- a lambda that
# calls `httpx.Client(...)` AFTER the patch is installed would call itself
# forever (the module attribute it looks up by that point is the patch).
_RealHttpxClient = httpx.Client


def _book(**overrides) -> DeliverBook:
    m4b = overrides.pop("m4b", None)
    defaults = dict(
        slug="book-a",
        title="Book A",
        author="A. Author",
        year="1925",
        genre="Fiction",
        m4b=m4b or Path("/nonexistent/Book A.m4b"),
        cover=None,
        duration_s=3600.0,
        chapters=19,
    )
    defaults.update(overrides)
    return DeliverBook(**defaults)


# --------------------------------------------------------------------------- registry


def test_registry_holds_both_kinds():
    reg = registry()
    assert set(reg) == {"folder", "audiobookshelf"}
    assert reg["folder"].kind == "folder"
    assert reg["audiobookshelf"].kind == "audiobookshelf"


# --------------------------------------------------------------------------- folder target: basics


def test_folder_target_validate_rejects_an_empty_root():
    with pytest.raises(ValueError):
        FolderTarget().validate({"root": ""})


def test_folder_target_validate_rejects_an_unknown_layout_field():
    with pytest.raises(ValueError):
        FolderTarget().validate({"root": "/output", "layout": "{nonsense}/{title}.m4b"})


def test_folder_target_validate_accepts_the_default_config(tmp_path):
    FolderTarget().validate({"root": str(tmp_path)})  # must not raise


def test_folder_target_test_writes_nothing(tmp_path):
    root = tmp_path / "output"
    result = FolderTarget().test({"root": str(root)})
    assert result.ok is True
    # test() must not create the directory it is only checking.
    assert not root.exists()


def test_folder_target_test_reports_unwritable(tmp_path, monkeypatch):
    import os

    root = tmp_path / "output"
    root.mkdir()
    monkeypatch.setattr(os, "access", lambda *a, **kw: False)
    result = FolderTarget().test({"root": str(root)})
    assert result.ok is False


def test_folder_target_delivers_the_m4b_and_the_cover(tmp_path):
    m4b = tmp_path / "source" / "Book A.m4b"
    m4b.parent.mkdir(parents=True)
    m4b.write_bytes(b"fake m4b bytes")
    cover = tmp_path / "source" / "cover.jpg"
    cover.write_bytes(b"fake cover bytes")

    root = tmp_path / "output"
    config = {"root": str(root), "layout": "{author}/{title}/{title}.m4b", "copy_cover": True}
    book = _book(m4b=m4b, cover=cover)

    result = FolderTarget().deliver(config, book)

    assert result.ok is True
    dest = root / "A. Author" / "Book A" / "Book A.m4b"
    assert dest.exists()
    assert dest.read_bytes() == b"fake m4b bytes"
    cover_dest = dest.with_name("Book A.jpg")
    assert cover_dest.exists()
    assert cover_dest.read_bytes() == b"fake cover bytes"


def test_folder_target_delivery_is_idempotent(tmp_path):
    m4b = tmp_path / "source" / "book.m4b"
    m4b.parent.mkdir(parents=True)
    m4b.write_bytes(b"v1")

    root = tmp_path / "output"
    config = {"root": str(root), "layout": "{title}.m4b"}
    book = _book(m4b=m4b, cover=None)

    first = FolderTarget().deliver(config, book)
    second = FolderTarget().deliver(config, book)

    assert first.ok and second.ok
    assert first.remote_ref == second.remote_ref
    dest = Path(first.remote_ref)
    assert dest.read_bytes() == b"v1"


def test_folder_target_deliver_fix_re_delivers(tmp_path):
    m4b = tmp_path / "source" / "book.m4b"
    m4b.parent.mkdir(parents=True)
    m4b.write_bytes(b"corrected bytes")

    root = tmp_path / "output"
    config = {"root": str(root), "layout": "{title}.m4b"}
    book = _book(m4b=m4b, cover=None)

    result = FolderTarget().deliver_fix(config, book)
    assert result.ok is True
    assert Path(result.remote_ref).read_bytes() == b"corrected bytes"


# --------------------------------------------------------------------------- folder target: the path-escape refusal


def test_folder_target_refuses_a_title_that_escapes_root(tmp_path):
    """A `..`-laden title must never let the write land outside `root` --
    whether the target achieves that by refusing outright, or (as this
    implementation does: `_safe_component` neutralises `/` inside one
    placeholder value before the path is ever built) by sanitising the
    value into a safe, contained filename. Either way, `../../../etc/evil`
    must never become a real `..` path segment."""
    m4b = tmp_path / "source" / "book.m4b"
    m4b.parent.mkdir(parents=True)
    m4b.write_bytes(b"x")

    root = tmp_path / "output"
    config = {"root": str(root), "layout": "{title}.m4b"}
    book = _book(m4b=m4b, cover=None, title="../../../etc/evil", author="Author")

    # Nothing must ever land outside root -- proven by containment, whether
    # delivery succeeds (into a sanitised path) or refuses outright.
    try:
        result = FolderTarget().deliver(config, book)
    except TargetError:
        pass
    else:
        Path(result.remote_ref).resolve().relative_to(root.resolve())

    assert not (tmp_path / "etc" / "evil.m4b").exists()


def test_folder_target_refuses_an_author_that_escapes_root(tmp_path):
    m4b = tmp_path / "source" / "book.m4b"
    m4b.parent.mkdir(parents=True)
    m4b.write_bytes(b"x")

    root = tmp_path / "output"
    config = {"root": str(root), "layout": "{author}/{title}.m4b"}
    book = _book(m4b=m4b, cover=None, author="..", title="Fine Title")

    try:
        result = FolderTarget().deliver(config, book)
    except TargetError:
        pass
    else:
        Path(result.remote_ref).resolve().relative_to(root.resolve())

    assert not (tmp_path.parent / "Fine Title.m4b").exists()


def test_folder_target_sanitises_a_bare_dot_dot_field_into_a_safe_component(tmp_path):
    """A field value of exactly '..' must not become a literal path
    component even when it does not, by itself, contain a '/'."""
    m4b = tmp_path / "source" / "book.m4b"
    m4b.parent.mkdir(parents=True)
    m4b.write_bytes(b"x")

    root = tmp_path / "output"
    config = {"root": str(root), "layout": "{author}/{title}.m4b"}
    book = _book(m4b=m4b, cover=None, author="..", title="Fine Title")

    # Whether this raises (the resolver refuses) or sanitises the "author"
    # into a safe literal segment, root containment must hold either way.
    try:
        result = FolderTarget().deliver(config, book)
    except TargetError:
        return
    dest = Path(result.remote_ref)
    dest.resolve().relative_to(root.resolve())


def test_folder_target_deliver_accepts_a_legitimate_title_with_a_colon(tmp_path):
    m4b = tmp_path / "source" / "book.m4b"
    m4b.parent.mkdir(parents=True)
    m4b.write_bytes(b"x")

    root = tmp_path / "output"
    config = {"root": str(root), "layout": "{title}.m4b"}
    book = _book(m4b=m4b, cover=None, title="Example Book: A Novel", author="Author")

    result = FolderTarget().deliver(config, book)
    assert result.ok is True
    dest = Path(result.remote_ref)
    dest.resolve().relative_to(root.resolve())


# --------------------------------------------------------------------------- audiobookshelf target: config


def test_audiobookshelf_validate_requires_an_embedded_folder_config():
    target = AudiobookshelfTarget()
    with pytest.raises(ValueError, match="folder_target"):
        target.validate(
            {"base_url": "http://abs:13378", "library_id": "lib1", "token_env": "NARRATARR_ABS_TOKEN",
             "folder_target": "some-other-target-name"}
        )


def test_audiobookshelf_validate_accepts_an_embedded_folder_config(tmp_path):
    target = AudiobookshelfTarget()
    target.validate(
        {
            "base_url": "http://abs:13378", "library_id": "lib1", "token_env": "NARRATARR_ABS_TOKEN",
            "folder_target": {"root": str(tmp_path)},
        }
    )  # must not raise


def test_audiobookshelf_token_is_read_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("NARRATARR_ABS_TOKEN", "secret-token-value")
    target = AudiobookshelfTarget()
    config = {
        "base_url": "http://abs:13378", "library_id": "lib1", "token_env": "NARRATARR_ABS_TOKEN",
        "folder_target": {"root": str(tmp_path)},
    }
    assert target._token(config) == "secret-token-value"


def test_audiobookshelf_missing_token_env_raises_without_leaking_the_var_name_as_a_value(monkeypatch, tmp_path):
    monkeypatch.delenv("NARRATARR_ABS_TOKEN", raising=False)
    target = AudiobookshelfTarget()
    config = {
        "base_url": "http://abs:13378", "library_id": "lib1", "token_env": "NARRATARR_ABS_TOKEN",
        "folder_target": {"root": str(tmp_path)},
    }
    with pytest.raises(ValueError, match="NARRATARR_ABS_TOKEN"):
        target._token(config)


# --------------------------------------------------------------------------- audiobookshelf target: pagination


def _items_response(items: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"results": items})


def _item(item_id: str, title: str, author: str, chapters: int, duration: float) -> dict:
    return {
        "id": item_id,
        "media": {
            "metadata": {"title": title, "authorName": author},
            "numChapters": chapters,
            "duration": duration,
        },
    }


def test_find_item_across_pages_finds_an_item_on_page_3():
    """The exact fault pipeline CONTRACT.md section 12 records: a fixed page
    size alone would never find an item that lands past the first page."""
    pages = {
        0: [_item(f"decoy-{i}", f"Decoy {i}", "Nobody", 1, 1.0) for i in range(500)],
        1: [_item(f"decoy-{i}", f"Decoy {i}", "Nobody", 1, 1.0) for i in range(500, 1000)],
        2: [_item("real-item-id", "Book A", "A. Author", 19, 3600.0)],
    }

    requested_pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "0"))
        requested_pages.append(page)
        return _items_response(pages.get(page, []))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    found = find_item_across_pages(client, "http://abs:13378", "lib1", "tok", "Book A")

    assert found is not None
    assert found["id"] == "real-item-id"
    assert requested_pages == [0, 1, 2]


def test_find_item_across_pages_stops_on_an_empty_page():
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "0"))
        if page == 0:
            return _items_response([_item("x", "Someone Else", "Author", 1, 1.0)])
        return _items_response([])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    found = find_item_across_pages(client, "http://abs:13378", "lib1", "tok", "Book A")
    assert found is None


def test_find_item_across_pages_stops_on_a_repeated_page():
    """A malformed server that echoes the same page forever must not spin
    the loop -- CONTRACT.md section 12 step 4."""
    same_page = [_item("x", "Someone Else", "Author", 1, 1.0)]
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return _items_response(same_page)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    found = find_item_across_pages(client, "http://abs:13378", "lib1", "tok", "Book A")
    assert found is None
    assert call_count["n"] == 2  # page 0, then page 1 repeats page 0's ids -> stop


def test_find_item_across_pages_respects_the_hard_page_cap():
    from narratarr.adapter.targets.audiobookshelf import MAX_ITEM_PAGES

    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        page = int(request.url.params.get("page", "0"))
        # A different single decoy item on every page: never empty, never
        # repeated, and never the title being searched for.
        return _items_response([_item(f"decoy-{page}", "Decoy", "Nobody", 1, 1.0)])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    found = find_item_across_pages(client, "http://abs:13378", "lib1", "tok", "Book A")
    assert found is None
    assert call_count["n"] == MAX_ITEM_PAGES


# --------------------------------------------------------------------------- audiobookshelf target: deliver end to end


def test_audiobookshelf_deliver_copies_scans_polls_and_verifies(tmp_path, monkeypatch):
    m4b = tmp_path / "source" / "Book A.m4b"
    m4b.parent.mkdir(parents=True)
    m4b.write_bytes(b"fake m4b bytes")

    scan_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/scan"):
            scan_calls.append(1)
            return httpx.Response(200, json={"ok": True})
        page = int(request.url.params.get("page", "0"))
        if page == 0:
            return _items_response([_item("abs-item-1", "Book A", "A. Author", 19, 3600.0)])
        return _items_response([])

    monkeypatch.setenv("NARRATARR_ABS_TOKEN", "tok")
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: _RealHttpxClient(transport=httpx.MockTransport(handler)))

    config = {
        "base_url": "http://abs:13378", "library_id": "lib1", "token_env": "NARRATARR_ABS_TOKEN",
        "folder_target": {"root": str(tmp_path / "output"), "layout": "{title}.m4b"},
    }
    book = _book(m4b=m4b, cover=None, chapters=19, duration_s=3600.0)

    result = AudiobookshelfTarget().deliver(config, book)

    assert result.ok is True
    assert result.remote_ref == "abs-item-1"
    assert result.url == "http://abs:13378/item/abs-item-1"
    assert scan_calls == [1]
    # the folder copy really happened underneath
    assert (tmp_path / "output" / "Book A.m4b").exists()


def test_audiobookshelf_deliver_raises_on_a_duration_mismatch(tmp_path, monkeypatch):
    m4b = tmp_path / "source" / "book.m4b"
    m4b.parent.mkdir(parents=True)
    m4b.write_bytes(b"x")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/scan"):
            return httpx.Response(200, json={"ok": True})
        page = int(request.url.params.get("page", "0"))
        if page == 0:
            # duration is wildly off -- more than 5 percent from 3600.0
            return _items_response([_item("abs-item-1", "Book A", "A. Author", 19, 100.0)])
        return _items_response([])

    monkeypatch.setenv("NARRATARR_ABS_TOKEN", "tok")
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: _RealHttpxClient(transport=httpx.MockTransport(handler)))

    config = {
        "base_url": "http://abs:13378", "library_id": "lib1", "token_env": "NARRATARR_ABS_TOKEN",
        "folder_target": {"root": str(tmp_path / "output"), "layout": "{title}.m4b"},
    }
    book = _book(m4b=m4b, cover=None, title="Book A", chapters=19, duration_s=3600.0)

    with pytest.raises(TargetError, match="duration"):
        AudiobookshelfTarget().deliver(config, book)


def test_audiobookshelf_deliver_raises_when_the_item_never_appears(tmp_path, monkeypatch):
    m4b = tmp_path / "source" / "book.m4b"
    m4b.parent.mkdir(parents=True)
    m4b.write_bytes(b"x")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/scan"):
            return httpx.Response(200, json={"ok": True})
        return _items_response([])  # library never shows the book

    monkeypatch.setenv("NARRATARR_ABS_TOKEN", "tok")
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: _RealHttpxClient(transport=httpx.MockTransport(handler)))

    # Patch out the real sleep so this test does not actually wait 300s.
    import narratarr.adapter.targets.audiobookshelf as abs_mod

    monkeypatch.setattr(abs_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(abs_mod, "POLL_TIMEOUT_S", 0.01)

    config = {
        "base_url": "http://abs:13378", "library_id": "lib1", "token_env": "NARRATARR_ABS_TOKEN",
        "folder_target": {"root": str(tmp_path / "output"), "layout": "{title}.m4b"},
    }
    book = _book(m4b=m4b, cover=None)

    with pytest.raises(TargetError, match="did not appear"):
        AudiobookshelfTarget().deliver(config, book)


def test_audiobookshelf_deliver_never_leaks_the_token_into_a_result(tmp_path, monkeypatch):
    """The token never enters a DeliveryResult (message, url, remote_ref) --
    APP-CONTRACT 10.2. A duration mismatch is a convenient failure to
    trigger: the resulting TargetError message must still name only the
    env var, never the secret value."""
    m4b = tmp_path / "source" / "book.m4b"
    m4b.parent.mkdir(parents=True)
    m4b.write_bytes(b"x")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/scan"):
            return httpx.Response(200, json={"ok": True})
        page = int(request.url.params.get("page", "0"))
        if page == 0:
            return _items_response([_item("abs-item-1", "Book A", "A. Author", 19, 1.0)])
        return _items_response([])

    monkeypatch.setenv("NARRATARR_ABS_TOKEN", "top-secret-value-must-never-leak")
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: _RealHttpxClient(transport=httpx.MockTransport(handler)))

    config = {
        "base_url": "http://abs:13378", "library_id": "lib1", "token_env": "NARRATARR_ABS_TOKEN",
        "folder_target": {"root": str(tmp_path / "output"), "layout": "{title}.m4b"},
    }
    book = _book(m4b=m4b, cover=None, title="Book A", chapters=19, duration_s=3600.0)

    with pytest.raises(TargetError) as excinfo:
        AudiobookshelfTarget().deliver(config, book)
    assert "top-secret-value-must-never-leak" not in str(excinfo.value)


def test_audiobookshelf_test_writes_nothing_and_checks_reachability(monkeypatch, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "lib1"})

    monkeypatch.setenv("NARRATARR_ABS_TOKEN", "tok")
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: _RealHttpxClient(transport=httpx.MockTransport(handler)))

    config = {
        "base_url": "http://abs:13378", "library_id": "lib1", "token_env": "NARRATARR_ABS_TOKEN",
        "folder_target": {"root": str(tmp_path / "output")},
    }
    result = AudiobookshelfTarget().test(config)
    assert result.ok is True
    assert not (tmp_path / "output").exists()


# --------------------------------------------------------------------------- deliver_job / deliver_job_fix
#
# APP-CONTRACT section 8.3, added at the overlord's request. A test here
# builds a real, temporary Narratarr database (W1's own schema, through
# `narratarr.db.init_db()`) and a real, minimal book directory on disk (a
# `book.json` and a tiny fake m4b) -- but `abpipe.ffmpeg.probe_duration` is
# monkeypatched, so no real ffprobe process ever runs, and no target ever
# opens a real socket (the folder target touches only `tmp_path`; the
# Audiobookshelf target, when used, goes through `httpx.MockTransport`).


class _FakeJob:
    """Stands in for `narratarr.models.Job` -- `deliver_job`/`deliver_job_fix`
    read only these five attributes off the object they are given."""

    def __init__(self, job_id: str, slug: str, source_path: str, book_config: dict, qc_config: dict):
        self.id = job_id
        self.slug = slug
        self.source_path = source_path
        self.book_config = json.dumps(book_config)
        self.qc_config = json.dumps(qc_config)


@pytest.fixture()
def deliver_env(tmp_path, monkeypatch):
    """A fresh database at `tmp_path`, plus a finished book on disk at
    `<config_dir>/work/<slug>/`, ready for `deliver_job`."""
    monkeypatch.setenv("NARRATARR_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("NARRATARR_OUTPUT_DIR", str(tmp_path / "output"))

    from narratarr import config as config_mod

    config_mod.get_settings.cache_clear()

    from narratarr import db as db_mod

    db_mod.init_db()

    settings = config_mod.get_settings()
    slug = "book-a"
    book_dir = settings.work_dir / slug
    (book_dir / "07-book").mkdir(parents=True)
    (book_dir / "07-book" / "Book A.m4b").write_bytes(b"fake m4b bytes")
    (book_dir / "book.json").write_text(
        json.dumps(
            {
                "schema": 1, "slug": slug, "title": "Book A", "author": "A. Author",
                "year": "1925", "genre": "Fiction",
                "chapters": [{"id": "ch01", "index": 1, "label": "One", "src": "x", "synthetic": False, "words": 5}],
            }
        )
    )

    source_epub = tmp_path / "library" / f"{slug}.epub"
    source_epub.parent.mkdir(parents=True)
    source_epub.write_bytes(b"fake epub")

    import abpipe.ffmpeg as ffmpeg_mod

    monkeypatch.setattr(ffmpeg_mod, "probe_duration", lambda path: 3600.0)

    job = _FakeJob("job-1", slug, str(source_epub), book_config={}, qc_config={})

    # deliveries.job_id REFERENCES jobs(id), and every connection this app
    # opens runs with PRAGMA foreign_keys=ON (APP-CONTRACT section 4) -- a
    # real jobs row must exist for deliver_job's own INSERT to succeed.
    from narratarr.db import now, transaction

    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                id, slug, title, author, source_path, source_sha256, state,
                book_config, qc_config, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.id, slug, "Book A", "A. Author", str(source_epub), "0" * 64,
                "delivering", "{}", "{}", now(), now(),
            ),
        )

    return job, settings


def _insert_target(conn, name: str, kind: str, config: dict, enabled: bool = True) -> str:
    from narratarr.db import new_id, now

    target_id = new_id()
    conn.execute(
        "INSERT INTO targets (id, name, kind, enabled, config, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (target_id, name, kind, int(enabled), json.dumps(config), now(), now()),
    )
    return target_id


def _deliveries(conn, job_id: str) -> list:
    return conn.execute("SELECT * FROM deliveries WHERE job_id = ?", (job_id,)).fetchall()


def test_deliver_job_delivers_to_every_enabled_target(deliver_env):
    from narratarr.adapter.targets import deliver_job
    from narratarr.db import connect, transaction

    job, settings = deliver_env
    root_a = settings.output_dir / "a"
    root_b = settings.output_dir / "b"
    with transaction() as conn:
        _insert_target(conn, "folder-a", "folder", {"root": str(root_a), "layout": "{title}.m4b"})
        _insert_target(conn, "folder-b", "folder", {"root": str(root_b), "layout": "{title}.m4b"})

    results = deliver_job(job)

    assert len(results) == 2
    assert all(r.ok for r in results)
    assert (root_a / "Book A.m4b").exists()
    assert (root_b / "Book A.m4b").exists()

    conn = connect()
    try:
        rows = _deliveries(conn, job.id)
    finally:
        conn.close()
    assert len(rows) == 2
    assert {row["state"] for row in rows} == {"delivered"}


def test_deliver_job_one_target_failing_does_not_stop_the_others(deliver_env):
    from narratarr.adapter.targets import deliver_job
    from narratarr.db import transaction

    job, settings = deliver_env
    good_root = settings.output_dir / "good"
    # A root under a file (not a directory) makes the folder target's own
    # write fail -- a real, not simulated, per-target fault.
    bad_parent = settings.output_dir / "not-a-directory"
    bad_parent.parent.mkdir(parents=True, exist_ok=True)
    bad_parent.write_bytes(b"x")
    bad_root = bad_parent / "output"

    with transaction() as conn:
        _insert_target(conn, "good", "folder", {"root": str(good_root), "layout": "{title}.m4b"})
        _insert_target(conn, "bad", "folder", {"root": str(bad_root), "layout": "{title}.m4b"})

    results = deliver_job(job)

    assert len(results) == 2
    oks = {r.ok for r in results}
    assert oks == {True, False}
    assert (good_root / "Book A.m4b").exists()


def test_deliver_job_skips_a_disabled_target(deliver_env):
    from narratarr.adapter.targets import deliver_job
    from narratarr.db import transaction

    job, settings = deliver_env
    enabled_root = settings.output_dir / "enabled"
    disabled_root = settings.output_dir / "disabled"

    with transaction() as conn:
        _insert_target(conn, "on", "folder", {"root": str(enabled_root), "layout": "{title}.m4b"}, enabled=True)
        _insert_target(conn, "off", "folder", {"root": str(disabled_root), "layout": "{title}.m4b"}, enabled=False)

    results = deliver_job(job)

    assert len(results) == 1
    assert (enabled_root / "Book A.m4b").exists()
    assert not disabled_root.exists()


def test_deliver_job_upserts_the_same_deliveries_row_on_a_second_call(deliver_env):
    from narratarr.adapter.targets import deliver_job
    from narratarr.db import connect, transaction

    job, settings = deliver_env
    root = settings.output_dir / "out"
    with transaction() as conn:
        target_id = _insert_target(conn, "only", "folder", {"root": str(root), "layout": "{title}.m4b"})

    deliver_job(job)
    deliver_job(job)

    conn = connect()
    try:
        rows = _deliveries(conn, job.id)
    finally:
        conn.close()

    assert len(rows) == 1  # the unique index on (job_id, target_id) held: no duplicate row
    assert rows[0]["target_id"] == target_id
    assert rows[0]["state"] == "delivered"


def test_deliver_job_never_leaks_a_token_into_a_deliveries_row_or_a_result(deliver_env, monkeypatch):
    from narratarr.adapter.targets import deliver_job
    from narratarr.db import connect, transaction

    job, settings = deliver_env
    monkeypatch.setenv("NARRATARR_ABS_TOKEN", "top-secret-value-must-never-leak")

    def handler(request: httpx.Request) -> httpx.Response:
        # No real network call -- always fail the reachability check
        # quickly, forcing an error path that a leaking implementation
        # would be most likely to put the token into.
        return httpx.Response(500, text="server error")

    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: _RealHttpxClient(transport=httpx.MockTransport(handler)))

    # The scan/items 500s never raise (see AudiobookshelfTarget._fetch_
    # items_page: a non-200 response reads as "no items", not an error),
    # so the poll would otherwise retry with a real time.sleep() for up to
    # the real 300s timeout. Shrink both so this test stays fast.
    import narratarr.adapter.targets.audiobookshelf as abs_mod

    monkeypatch.setattr(abs_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(abs_mod, "POLL_TIMEOUT_S", 0.01)

    with transaction() as conn:
        _insert_target(
            conn, "abs", "audiobookshelf",
            {
                "base_url": "http://abs:13378", "library_id": "lib1", "token_env": "NARRATARR_ABS_TOKEN",
                "folder_target": {"root": str(settings.output_dir / "abs-copy"), "layout": "{title}.m4b"},
            },
        )

    results = deliver_job(job)

    assert len(results) == 1
    for result in results:
        assert "top-secret-value-must-never-leak" not in (result.message or "")
        assert "top-secret-value-must-never-leak" not in (result.remote_ref or "")
        assert "top-secret-value-must-never-leak" not in (result.url or "")

    conn = connect()
    try:
        rows = _deliveries(conn, job.id)
    finally:
        conn.close()
    for row in rows:
        row_text = json.dumps({k: row[k] for k in row.keys()})
        assert "top-secret-value-must-never-leak" not in row_text


def test_deliver_job_unknown_target_kind_fails_that_target_only(deliver_env):
    from narratarr.adapter.targets import deliver_job
    from narratarr.db import transaction

    job, settings = deliver_env
    good_root = settings.output_dir / "good"
    with transaction() as conn:
        _insert_target(conn, "good", "folder", {"root": str(good_root), "layout": "{title}.m4b"})
        _insert_target(conn, "mystery", "carrier-pigeon", {})

    results = deliver_job(job)

    assert len(results) == 2
    assert sum(1 for r in results if r.ok) == 1
    assert sum(1 for r in results if not r.ok) == 1


def test_deliver_job_fix_calls_deliver_fix_not_deliver(deliver_env, monkeypatch):
    from narratarr.adapter.targets import deliver_job_fix
    from narratarr.adapter.targets.folder import FolderTarget

    job, settings = deliver_env
    calls = []

    from narratarr.adapter.targets.base import DeliveryResult

    def fake_deliver(self, config, book, progress=None):
        calls.append("deliver")
        return DeliveryResult(ok=True, message="delivered")

    def fake_deliver_fix(self, config, book, progress=None):
        calls.append("deliver_fix")
        return DeliveryResult(ok=True, message="fixed")

    monkeypatch.setattr(FolderTarget, "deliver", fake_deliver)
    monkeypatch.setattr(FolderTarget, "deliver_fix", fake_deliver_fix)

    from narratarr.db import transaction

    with transaction() as conn:
        _insert_target(conn, "only", "folder", {"root": str(settings.output_dir), "layout": "{title}.m4b"})

    results = deliver_job_fix(job)

    assert calls == ["deliver_fix"]
    assert results[0].ok is True
