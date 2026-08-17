"""Stage 7 — bind: join the chapter m4a files into one m4b audiobook.

CONTRACT.md section 11 defines this stage. Owner: Worker D.
"""

from __future__ import annotations

import os
from pathlib import Path

from abpipe import ffmpeg
from abpipe.meta import clear_meta, hash_file, hash_many, hash_obj, is_fresh, write_meta, write_text

STAGE = "bind"

DURATION_TOLERANCE_S = 2.0
DURATION_TOLERANCE_FRACTION = 0.02


def _chapters_dir(ctx) -> Path:
    return ctx.stage_dir("assemble", make=False)


def _ffmeta_path(ctx) -> Path:
    return _chapters_dir(ctx) / "chapters.ffmeta"


def _m4a_paths(ctx, ids: list[str]) -> list[Path]:
    return [_chapters_dir(ctx) / f"{cid}.m4a" for cid in ids]


def _m4b_path(ctx) -> Path:
    return ctx.stage_dir("bind") / f"{ctx.title}.m4b"


def _concat_list_path(ctx) -> Path:
    return ctx.stage_dir("bind") / "concat_list.txt"


def _tags(ctx) -> dict:
    return {
        "title": ctx.title,
        "album": ctx.title,
        "artist": ctx.author,
        "album_artist": ctx.author,
        "date": str(ctx.book.get("year") or ""),
        "genre": str(ctx.book.get("genre") or ""),
        "media_type": "2",
    }


def _config(ctx) -> dict:
    """Return the part of the configuration that changes the output of this stage."""
    cover = ctx.cover_path
    cover_hash = hash_file(cover) if cover.exists() else None
    return {"tags": _tags(ctx), "cover_hash": cover_hash}


def escape_concat_path(path: Path) -> str:
    """Return one line of a concat-demuxer list file, with the path escaped.

    The ffmpeg concat demuxer parses `file '<path>'` with single quotes. A
    single quote inside the path must become `'\\''` — close the quote,
    an escaped literal quote, reopen the quote.
    """
    escaped = str(path).replace("'", "'\\''")
    return f"file '{escaped}'"


def _write_concat_list(ctx, m4a_paths: list[Path]) -> Path:
    concat_list = _concat_list_path(ctx)
    lines = [escape_concat_path(p) for p in m4a_paths]
    write_text(concat_list, "\n".join(lines) + "\n")
    return concat_list


def _build_argv(concat_list: Path, ffmeta_path: Path, cover_path: Path | None, tags: dict, out_path: Path) -> list[str]:
    argv: list[str] = ["-f", "concat", "-safe", "0", "-i", str(concat_list), "-i", str(ffmeta_path)]
    has_cover = cover_path is not None and cover_path.exists()
    if has_cover:
        argv += ["-i", str(cover_path)]

    argv += ["-map", "0:a"]
    if has_cover:
        argv += ["-map", "2:v"]
    argv += ["-map_metadata", "1", "-map_chapters", "1"]
    argv += ["-c:a", "copy"]
    if has_cover:
        argv += ["-c:v", "copy", "-disposition:v:0", "attached_pic"]

    for key, value in tags.items():
        argv += ["-metadata", f"{key}={value}"]

    # Explicit output muxer: out_path is a .m4b.tmp path during the atomic
    # write (Defect 3), and ffmpeg can only infer a muxer from a recognized
    # extension -- ".tmp" defeats that. "ipod" is the muxer ffmpeg's own
    # extension table maps .m4a/.m4b to.
    argv += ["-f", "ipod", str(out_path)]
    return argv


def _verify(ctx, m4b_path: Path, m4a_paths: list[Path], expected_chapters: int) -> dict:
    """Probe the freshly written m4b and assert its shape. Raise loudly on mismatch."""
    probed = ffmpeg.probe_json(m4b_path)
    got_chapters = len(probed.get("chapters") or [])
    if got_chapters != expected_chapters:
        raise RuntimeError(
            f"bind: verification failed — {m4b_path} has {got_chapters} chapter(s), "
            f"expected {expected_chapters}"
        )

    fmt = probed.get("format") or {}
    got_duration = fmt.get("duration")
    if got_duration is None:
        raise RuntimeError(f"bind: verification failed — {m4b_path} reports no duration")
    got_duration = float(got_duration)

    expected_duration = sum(ffmpeg.probe_duration(p) for p in m4a_paths)
    tolerance = max(DURATION_TOLERANCE_S, DURATION_TOLERANCE_FRACTION * expected_duration)
    if abs(got_duration - expected_duration) > tolerance:
        raise RuntimeError(
            f"bind: verification failed — {m4b_path} duration is {got_duration:.2f}s, "
            f"expected close to {expected_duration:.2f}s (tolerance {tolerance:.2f}s)"
        )

    return {"chapters": got_chapters, "duration_s": got_duration}


def run(ctx, force: bool = False, **kw) -> dict:
    """Run the bind stage. Return a summary dict."""
    ids = ctx.chapter_ids()
    if not ids:
        raise RuntimeError("bind: book.json holds no chapters")

    ffmeta_path = _ffmeta_path(ctx)
    if not ffmeta_path.exists():
        raise RuntimeError(
            f"bind: refusing to run — {ffmeta_path} is missing. Run assemble on the "
            "full chapter set first."
        )

    m4a_paths = _m4a_paths(ctx, ids)
    missing = [p for p in m4a_paths if not p.exists()]
    if missing:
        names = ", ".join(p.name for p in missing)
        raise RuntimeError(f"bind: refusing to run — missing chapter m4a file(s): {names}")

    input_hash = hash_many([hash_file(p) for p in m4a_paths] + [hash_file(ffmeta_path)])
    config_hash = hash_obj(_config(ctx))

    m4b_path = _m4b_path(ctx)

    if not force and is_fresh(m4b_path, input_hash, config_hash):
        return {"stage": STAGE, "done": [], "skipped": [ctx.title], "failed": []}

    if force:
        # CONTRACT.md 3.3: a stage rebuilding with --force calls clear_meta()
        # before it starts, so a stale meta cannot outlive a kill and vouch
        # for a half-written replacement.
        clear_meta(m4b_path)

    concat_list = _write_concat_list(ctx, m4a_paths)
    cover_path = ctx.cover_path if ctx.cover_path.exists() else None

    # Atomic write (Defect 3): ffmpeg writes to a temp file in the same
    # directory, this stage verifies THAT file, and only then os.replace's it
    # into place. A kill mid-ffmpeg-run, or a verification failure, never
    # leaves a corrupt or half-written .m4b at the final path -- either the
    # old .m4b survives untouched, or nothing does.
    m4b_path.parent.mkdir(parents=True, exist_ok=True)
    m4b_tmp = m4b_path.parent / (m4b_path.name + ".tmp")
    argv = _build_argv(concat_list, ffmeta_path, cover_path, _tags(ctx), m4b_tmp)
    try:
        ffmpeg.run(argv)
        verified = _verify(ctx, m4b_tmp, m4a_paths, expected_chapters=len(ids))
        os.replace(m4b_tmp, m4b_path)
    except BaseException:
        try:
            m4b_tmp.unlink()
        except OSError:
            pass
        raise

    write_meta(
        m4b_path,
        STAGE,
        input_hash,
        config_hash,
        extra={"chapters": verified["chapters"], "duration_s": verified["duration_s"]},
    )

    return {"stage": STAGE, "done": [ctx.title], "skipped": [], "failed": []}
