"""The prune stage: reclaim disk space from finished, verified chapters.

CONTRACT.md section 15 defines this stage. Owner: Worker G.

The full book needs about 3.2 GB of intermediate audio that the render never
needs again once a chapter's m4a is built and verified: the per-chunk WAV
files under ``04-audio/<id>/`` and the joined chapter WAV under
``06-chapters/<id>.wav``. This module deletes those, one chapter at a time,
but only when every guard below holds.

**This module deletes files.** A chapter's m4a is the only durable artifact
standing between a delete and hours of lost render time. Every guard here
defaults to refusing, and ``prune_chapter`` never raises for a normal
refusal -- it returns ``{"pruned": False, "reason": "..."}`` instead, so a
caller (the CLI, ``prune_all``, a script run overnight) never has to wrap
every call in a try/except to stay safe.
"""

from __future__ import annotations

from pathlib import Path

from abpipe import ffmpeg
from abpipe.meta import (
    hash_file,
    meta_path,
    read_json,
    read_meta,
    utc_stamp,
    write_json,
)

STAGE = "prune"

# --------------------------------------------------------------------------- config

# NOTES.md, Phase 0 voice calibration: the bm_george sample rendered 1005
# characters of this book's own text into 63.7s of audio -- 15.78 chars/sec.
# book.json's engine.voice is bm_george, so this is the right constant for
# this book. Used only as a *fallback* expected-duration estimate, when a
# chapter's QC report holds no usable duration_s. See
# _expected_duration_floor().
CHARS_PER_SECOND_ESTIMATE = 1005 / 63.7  # ~15.78 chars/sec

# How far short of the expected duration an m4a is allowed to fall before
# prune refuses it as implausibly short.
#
# Assembly (CONTRACT.md 10.1) only *adds* silence between chunks -- 0.35s to
# 2.00s per chunk, easily hundreds of chunks per chapter -- so a correctly
# assembled m4a should always run longer than the raw QC-recorded speech
# duration (the sum of the per-chunk rendered WAV durations), never
# dramatically shorter. A factor of 0.5 gives real headroom for loudnorm and
# AAC container rounding and the leading/trailing trim in
# assemble.trim_silence, while still catching the actual failure this guards
# against: a truncated or corrupt encode standing in for a whole chapter.
DURATION_FLOOR_FACTOR = 0.5


# --------------------------------------------------------------------------- paths


def _m4a_path(ctx, chapter_id: str) -> Path:
    return ctx.stage_dir("assemble", make=False) / f"{chapter_id}.m4a"


def _chapter_wav_path(ctx, chapter_id: str) -> Path:
    return ctx.stage_dir("assemble", make=False) / f"{chapter_id}.wav"


def _audio_dir(ctx, chapter_id: str) -> Path:
    return ctx.stage_dir("render", make=False) / chapter_id


def _qc_report_path(ctx) -> Path:
    return ctx.stage_dir("qc", make=False) / "qc-report.json"


def _chunk_index_path(ctx, chapter_id: str) -> Path:
    return ctx.stage_dir("chunk", make=False) / chapter_id / "index.json"


def _pruned_marker_path(ctx, chapter_id: str) -> Path:
    return ctx.stage_dir("assemble", make=False) / f"{chapter_id}.pruned.json"


def _unsafe_path_reason(ctx, path: Path) -> str | None:
    """Return a refusal reason if `path` is unsafe to read, measure, or
    delete. Otherwise return None.

    Two rules, deliberately stricter than "just don't escape the book dir":

    1. A symlink is always unsafe, full stop -- prune never follows one and
       never deletes one, regardless of where it points. This removes any
       need to reason about a TOCTOU race between the safety check and the
       later unlink() (the symlink's target could change between the two).
    2. The path must resolve to somewhere under ctx.book_dir. This is the
       CONTRACT.md-mandated check, and it also catches a symlink *ancestor*
       directory (an unsafe symlink two directories up that rule 1 alone,
       checked only on the leaf path, would miss).
    """
    if path.is_symlink():
        return f"{path} is a symlink; prune never follows or deletes a symlink"
    try:
        resolved = path.resolve(strict=False)
        book_dir = ctx.book_dir.resolve(strict=False)
    except OSError as exc:
        return f"could not resolve {path}: {exc}"
    if resolved != book_dir and book_dir not in resolved.parents:
        return f"{path} resolves outside the book directory ({resolved})"
    return None


def _candidate_targets(ctx, chapter_id: str) -> list[Path]:
    """Return every path prune_chapter might touch for one chapter, whether
    or not it currently exists.

    Used both to compute what would be deleted and to symlink/escape-check
    every candidate before any of them is touched. Nonexistent paths are
    included on purpose: a dangling symlink named e.g. "0001.wav" still
    needs the safety check, and a chapter re-pruned after a partial delete
    (process killed mid-prune) must not choke on a target that is already
    gone.
    """
    targets: list[Path] = []
    audio_dir = _audio_dir(ctx, chapter_id)
    if audio_dir.is_dir():
        for wav in sorted(audio_dir.glob("*.wav")):
            targets.append(wav)
            targets.append(meta_path(wav))
    chapter_wav = _chapter_wav_path(ctx, chapter_id)
    targets.append(chapter_wav)
    targets.append(meta_path(chapter_wav))
    return targets


# --------------------------------------------------------------------------- duration guard


def _expected_duration_floor(ctx, chapter_id: str, qc_entry: dict) -> tuple[float | None, str]:
    """Return (floor_seconds, source) -- the shortest plausible m4a duration
    for this chapter, and a human-readable note of where the estimate came
    from. Returns (None, "") when neither signal is available, in which case
    the caller skips guard 4 rather than refuse for lack of information.

    Prefers the QC report's recorded duration_s for the chapter (the sum of
    the per-chunk rendered WAV durations) -- CONTRACT.md 9.4's own worked
    example records this per chapter, and it is a real measurement of this
    exact chapter's audio, not an estimate. Falls back to an estimate from
    the chunk index's total character count only when duration_s is absent
    or zero.
    """
    qc_duration = qc_entry.get("duration_s")
    if isinstance(qc_duration, (int, float)) and qc_duration > 0:
        return (
            qc_duration * DURATION_FLOOR_FACTOR,
            f"qc-report.json duration_s={qc_duration:.1f}s",
        )

    index = read_json(_chunk_index_path(ctx, chapter_id))
    if isinstance(index, dict):
        chunks = index.get("chunks") or []
        total_chars = sum(c.get("chars") or 0 for c in chunks)
        if total_chars > 0:
            estimate = total_chars / CHARS_PER_SECOND_ESTIMATE
            return (
                estimate * DURATION_FLOOR_FACTOR,
                f"chunk index char estimate ({total_chars} chars)",
            )

    return None, ""


# --------------------------------------------------------------------------- one chapter


def _refusal(chapter_id: str, reason: str) -> dict:
    return {"chapter": chapter_id, "files": [], "bytes": 0, "pruned": False, "reason": reason}


def prune_chapter(ctx, chapter_id: str, dry_run: bool = False) -> dict:
    """Remove the intermediate audio of one chapter, once its m4a is durable.

    Removes 04-audio/<id>/*.wav and 06-chapters/<id>.wav, plus each file's
    .meta.json. Refuses (returns {"pruned": False, "reason": "..."}, never
    raises) unless every guard in CONTRACT.md section 15 holds:

    1. 06-chapters/<id>.m4a exists and its meta file is fresh (parses,
       schema 1, hashes present).
    2. qc-report.json holds an entry for this chapter, and that entry
       reports zero needs_human.
    3. probe_duration() of the m4a is greater than zero.

    Plus two guards of this module's own:

    4. The m4a duration is not implausibly short versus the chapter's
       expected length (see _expected_duration_floor).
    5. No path this call would read, measure, or delete is a symlink, or
       resolves outside ctx.book_dir (see _unsafe_path_reason).

    An unknown chapter_id raises KeyError (via ctx.chapter_ids), matching
    every other stage's "a typed chapter name never fails silently" rule
    (CONTRACT.md 13) -- that is a caller bug, not a normal refusal.

    When dry_run is True, nothing is deleted and no .pruned.json is
    written; the return value reports exactly what *would* happen
    (reason == "dry_run" on success).

    Calling this on an already-pruned chapter is a no-op, not an error:
    every guard above only inspects the durable m4a, its meta, and the QC
    report -- none of which prune_chapter itself ever touches -- so a
    second call finds nothing left to remove and returns pruned=True with
    an empty file list.
    """
    ctx.chapter_ids(only=[chapter_id])  # KeyError on a typo, not a refusal

    m4a_path = _m4a_path(ctx, chapter_id)

    unsafe = _unsafe_path_reason(ctx, m4a_path)
    if unsafe:
        return _refusal(chapter_id, f"refusing to prune {chapter_id}: {unsafe}")

    # Guard 1: the m4a exists, and its meta file is fresh.
    if not m4a_path.exists():
        return _refusal(chapter_id, f"m4a does not exist: {m4a_path}")
    m4a_meta = read_meta(m4a_path)
    if not m4a_meta:
        return _refusal(chapter_id, f"m4a meta file is missing or does not parse: {meta_path(m4a_path)}")
    if m4a_meta.get("schema") != 1:
        return _refusal(chapter_id, f"m4a meta has schema {m4a_meta.get('schema')!r}, expected 1")
    if not (m4a_meta.get("input_hash") and m4a_meta.get("config_hash") and m4a_meta.get("output_sha256")):
        return _refusal(chapter_id, "m4a meta is missing input_hash, config_hash, or output_sha256")

    # Guard 2: the QC report holds a clean entry for this chapter.
    report = read_json(_qc_report_path(ctx))
    if not isinstance(report, dict):
        return _refusal(chapter_id, f"qc-report.json is missing or does not parse: {_qc_report_path(ctx)}")
    qc_entry = (report.get("chapters") or {}).get(chapter_id)
    if not isinstance(qc_entry, dict):
        return _refusal(chapter_id, f"qc-report.json holds no entry for {chapter_id}")
    needs_human = qc_entry.get("needs_human", 0)
    if needs_human and needs_human > 0:
        return _refusal(chapter_id, f"qc report holds {needs_human} needs_human chunk(s) for {chapter_id}")

    # Guard 3: the m4a actually has audio.
    try:
        duration_s = ffmpeg.probe_duration(m4a_path)
    except Exception as exc:  # never raise for a probe failure -- refuse instead
        return _refusal(chapter_id, f"could not probe m4a duration: {exc}")
    if not duration_s or duration_s <= 0:
        return _refusal(chapter_id, f"m4a duration is {duration_s}s, not greater than zero")

    # Guard 4 (extra): the m4a is not implausibly short.
    floor_s, floor_source = _expected_duration_floor(ctx, chapter_id, qc_entry)
    if floor_s is not None and duration_s < floor_s:
        return _refusal(
            chapter_id,
            f"m4a duration {duration_s:.1f}s is implausibly short "
            f"(expected at least {floor_s:.1f}s, from {floor_source})",
        )

    # Guard 5 (extra): every deletion candidate is safe to touch.
    candidates = _candidate_targets(ctx, chapter_id)
    for candidate in candidates:
        unsafe = _unsafe_path_reason(ctx, candidate)
        if unsafe:
            return _refusal(chapter_id, f"refusing to prune {chapter_id}: {unsafe}")

    files = [{"path": str(p), "bytes": p.stat().st_size} for p in candidates if p.exists()]
    total_bytes = sum(f["bytes"] for f in files)

    if dry_run:
        return {
            "chapter": chapter_id,
            "files": [f["path"] for f in files],
            "bytes": total_bytes,
            "pruned": False,
            "reason": "dry_run",
        }

    for f in files:
        Path(f["path"]).unlink(missing_ok=True)

    write_json(
        _pruned_marker_path(ctx, chapter_id),
        {
            "schema": 1,
            "chapter": chapter_id,
            "pruned_at": utc_stamp(),
            "files": [f["path"] for f in files],
            "bytes": total_bytes,
            "m4a_sha256": hash_file(m4a_path),
        },
    )

    return {
        "chapter": chapter_id,
        "files": [f["path"] for f in files],
        "bytes": total_bytes,
        "pruned": True,
        "reason": "ok",
    }


# --------------------------------------------------------------------------- all chapters


def prune_all(ctx, chapters: list[str] | None = None, dry_run: bool = False) -> dict:
    """Prune every eligible chapter. Return a stage-shaped summary dict.

    Follows CONTRACT.md 13's summary shape (stage/done/skipped/failed), plus
    this stage's own `bytes` (total reclaimed, or that would be reclaimed
    under dry_run) and `results` (the full per-chapter prune_chapter()
    return, for a caller that wants the reason a chapter was skipped).

    `failed` is always empty: prune_chapter never raises for a normal
    refusal, so every chapter this function processes ends up in `done` or
    `skipped`, never `failed`.
    """
    ids = ctx.chapter_ids(only=chapters)
    done: list[str] = []
    skipped: list[dict] = []
    results: dict[str, dict] = {}
    total_bytes = 0

    for chapter_id in ids:
        result = prune_chapter(ctx, chapter_id, dry_run=dry_run)
        results[chapter_id] = result
        succeeded = result["pruned"] or (dry_run and result["reason"] == "dry_run")
        if succeeded:
            done.append(chapter_id)
            total_bytes += result["bytes"]
        else:
            skipped.append({"chapter": chapter_id, "reason": result["reason"]})

    return {
        "stage": STAGE,
        "done": done,
        "skipped": skipped,
        "failed": [],
        "bytes": total_bytes,
        "results": results,
    }


# --------------------------------------------------------------------------- estimate


def estimate_savings(ctx, chapters: list[str] | None = None) -> dict:
    """Report how many bytes are currently reclaimable. Deletes nothing.

    Runs prune_chapter(dry_run=True) under the hood, so a chapter this
    reports as reclaimable really is -- there is exactly one code path that
    decides eligibility, and this is not a second, looser copy of it.
    """
    ids = ctx.chapter_ids(only=chapters)
    per_chapter: dict[str, dict] = {}
    total_bytes = 0
    eligible = 0

    for chapter_id in ids:
        result = prune_chapter(ctx, chapter_id, dry_run=True)
        reclaimable = result["reason"] == "dry_run"
        chapter_bytes = result["bytes"] if reclaimable else 0
        if reclaimable:
            eligible += 1
            total_bytes += chapter_bytes
        try:
            label = ctx.chapter(chapter_id).get("label", chapter_id)
        except KeyError:
            label = chapter_id
        per_chapter[chapter_id] = {
            "label": label,
            "reclaimable": reclaimable,
            "bytes": chapter_bytes,
            "mb": round(chapter_bytes / 1_000_000, 1),
            "reason": result["reason"],
        }

    return {
        "chapters": per_chapter,
        "eligible_chapters": eligible,
        "total_chapters": len(ids),
        "bytes": total_bytes,
        "mb": round(total_bytes / 1_000_000, 1),
        "gb": round(total_bytes / 1_000_000_000, 3),
    }
