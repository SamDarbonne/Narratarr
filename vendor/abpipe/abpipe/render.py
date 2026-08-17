"""Stage 4 — render. CONTRACT.md section 8.

Reads `03-chunks/<id>/index.json`. Writes `04-audio/<id>/<nnnn>.wav`: 24000 Hz,
one channel, 16-bit signed PCM.

Resumability matters: this stage renders hours of audio over hundreds of
chunks in one long background job. Every WAV is written to a `.tmp` file in
its final directory, then moved into place with `os.replace` — a kill mid-write
never leaves a truncated WAV that a later run trusts. The meta file, which
`is_fresh()` checks before anything else, is written only after the WAV is
safely in place.

Failure handling (added after a real Phase 2 run flagged the risk): a bare
per-chunk failure is expected and survivable — one bad chunk must not kill an
8-hour job. A *persistent* fault is a different animal. Two guards:

1. A disk-full or read-only-filesystem error aborts the run immediately,
   instead of grinding through every remaining chunk with the same doomed
   write. See `_is_fatal_disk_error`.
2. A circuit breaker stops the run after `max_consecutive_failures` chunks
   in a row fail for any reason — a persistent fault (a bad engine state, a
   corrupt input) looks the same as "a full disk that soundfile reports as a
   generic error", so both need a backstop, not just the errno check.
"""

from __future__ import annotations

import errno
import os
import re
import time

import numpy as np
import soundfile as sf

from abpipe import homographs
from abpipe.context import Context
from abpipe.engines import get_engine
from abpipe.meta import hash_obj, is_fresh, read_json, write_meta

DEFAULT_MAX_CONSECUTIVE_FAILURES = 5

# Substrings of a disk-full / read-only-filesystem error message, lower
# case. soundfile does not always raise a plain OSError with .errno set —
# on a real write failure it typically raises soundfile.LibsndfileError
# (a RuntimeError subclass) whose message embeds libsndfile's own
# system-error string, not a structured errno. The string check is the
# only reliable cross-exception-type signal for that path; the errno check
# below is the reliable signal for a plain OSError (e.g. from os.replace,
# or the .tmp file's os.write failing, or a test that raises
# OSError(errno.ENOSPC) directly, which is exactly how this is tested).
_FATAL_DISK_MESSAGES = ("no space left", "read-only file system")


def _is_fatal_disk_error(exc: BaseException) -> bool:
    """Return True for a disk-full or read-only-filesystem failure.

    Non-recoverable: continuing the chunk loop after this can only produce
    the same failure again, chunk after chunk, for the rest of the run.
    """
    if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EROFS):
        return True
    msg = str(exc).lower()
    return any(needle in msg for needle in _FATAL_DISK_MESSAGES)


def render_chunk(engine, text: str) -> tuple[np.ndarray, int]:
    """Run one chunk of text through an engine. Return (float32 samples, sample_rate)."""
    audio, sample_rate = engine.synthesize(text)
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    return audio, int(sample_rate)


# --------------------------------------------------------------------------- pronunciations (CONTRACT.md 9.6)
#
# book.json's optional `pronunciations` object respells a word so the engine
# says it correctly -- e.g. {"Gyko": "Gikko"} corrects Kokoro's misaki front
# end reading the invented name with a long i ("JYE-po") instead of the
# short i this book needs ("JIP-oh"). Two callers share this exact
# function, per CONTRACT.md 9.6 ("both use the same table"):
#   1. This module, run() below: applied to the chunk text right before
#      engine.synthesize() -- this is the ONLY thing that changes the
#      audio. The chunk .txt file on disk is never touched.
#   2. abpipe/qc.py: applied to the SOURCE side only, before the QC
#      comparison, and to the text handed to the engine on the remediation
#      ladder's re-render/split rungs (qc.py imports this function lazily,
#      the same way it lazily imports render_chunk/get_engine, so importing
#      abpipe.qc never has to import a still-being-edited abpipe.render).


def apply_pronunciations(text: str, pronunciations: dict[str, str] | None) -> str:
    """Respell `text` per book.json's `pronunciations` map.

    Whole-word match only (regex `\\b` at both ends), case-sensitive -- a
    book's pronunciation entry names a proper noun, which this book always
    capitalises the same way, and case-insensitive matching would risk
    corrupting an unrelated lower-case word. `\\b` also makes the possessive
    correct for free: "Gyko's" has a word boundary right after "Gyko" (the
    apostrophe is not a word character), so a "Gyko" -> "Gikko" entry turns
    it into "Gikko's" with no special-casing needed. The same boundary rule
    guarantees the map never matches inside a larger word ("Gykos",
    "Gykology" survive untouched).

    An empty or absent map returns `text` unchanged -- byte-for-byte, since
    this is the default for every book that has not named an override.

    Every entry is applied in a single compiled regex pass (not one `re.sub`
    call per entry in a loop), so a substitution's own output is never
    re-scanned by another entry's pattern. Entries are ordered longest-key-
    first only to make the alternation's match order deterministic when two
    keys could otherwise tie; `\\b`-bounded whole words practically never
    overlap, so this mostly guards against a coincidence, not a common case.
    """
    if not pronunciations:
        return text
    keys = sorted(pronunciations, key=len, reverse=True)
    pattern = re.compile("|".join(r"\b" + re.escape(k) + r"\b" for k in keys))
    return pattern.sub(lambda m: pronunciations[m.group(0)], text)


def render_input_hash(record: dict, decisions_doc: dict, chapter_id: str) -> str:
    """Return stage 4's per-chunk `input_hash` (CONTRACT.md 18.6).

    A chunk's input is its text AND the homograph decisions that apply to
    that chunk, because a decision changes what the engine is handed just
    as surely as the text does.

    **A chunk with no decision hashes to the bare `record["sha256"]`.**
    That was stage 4's input_hash before homographs existed, so every WAV
    of every book rendered before this rule stays fresh. Only a chunk that
    gains a decision goes stale. Refer to `homographs.chunk_input_hash`.

    Exposed as a standalone function, in the same way and for the same
    reason as `render_config_hash` above: `cli.py`'s `_status_render` and
    `qc.py`'s remediation ladder must ask this module for the real
    formula. A second copy of the rule drifts, and CONTRACT.md 14 records
    the defect that taught this project so.
    """
    chunk_decisions = homographs.decisions_for_chunk(decisions_doc, chapter_id, record["id"])
    return homographs.chunk_input_hash(record["sha256"], chunk_decisions)


def render_config_hash(engine_desc: dict, pronunciations: dict | None = None) -> str:
    """Return stage 4's config_hash (CONTRACT.md 8): `engine.describe()` plus
    the pronunciation map. Both belong in the hash -- a change to either must
    make every WAV it could touch stale, per the idempotence rule
    (CONTRACT.md 3.2), and the map changes what the engine is handed just as
    surely as an engine setting does.

    Exposed as a standalone function (not inlined in run() below) because
    qc.py's remediation ladder (CONTRACT.md 9.3) re-renders a chunk through
    this same substitution and must write that chunk's render meta file with
    a config_hash stage 4 itself would recognise as fresh -- not a second,
    slightly different formula that reads the chunk as stale again the next
    time render.run() runs, undoing the ladder's own work.
    """
    return hash_obj({"engine": engine_desc, "pronunciations": dict(pronunciations or {})})


def _fmt_duration(seconds: float) -> str:
    if seconds < 0 or seconds != seconds:  # negative or NaN
        seconds = 0.0
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _write_wav_atomic(out_path, audio: np.ndarray, sample_rate: int) -> None:
    """Write a WAV to a .tmp file beside `out_path`, then move it into place.

    On any failure the `.tmp` file is removed before the exception
    propagates. Without this a failed write (disk full, mid-write) leaves a
    partial `.tmp` file behind; on a run that fails chunk after chunk that
    is a thousand-plus orphaned files on a disk that is already full.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.parent / (out_path.name + ".tmp")
    try:
        # format="WAV" is explicit: the .tmp suffix defeats soundfile's
        # extension-based format sniffing.
        sf.write(str(tmp_path), audio, sample_rate, subtype="PCM_16", format="WAV")
        os.replace(tmp_path, out_path)
    except BaseException:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def run(
    ctx: Context,
    chapters: list[str] | None = None,
    force: bool = False,
    engine=None,
    max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
    **kw,
) -> dict:
    """Run stage 4. Return the summary dict.

    `engine` is the injection point: pass a fake engine in tests. When it is
    `None`, `get_engine(ctx.engine_config)` loads the real one — this only
    happens when no test-supplied engine is given.

    `max_consecutive_failures` is the circuit breaker (module docstring): the
    run stops after this many chunk failures in a row, rather than grinding
    through every remaining chunk of an 8-hour job on a persistent fault.
    A single bad chunk, surrounded by good ones, never trips it.
    """
    if engine is None:
        engine = get_engine(ctx.engine_config)

    ids = ctx.chapter_ids(chapters)
    # CONTRACT.md 9.6: book.json's optional pronunciation map, empty by
    # default. It is folded into config_hash below (render_config_hash), so
    # a change to it invalidates every WAV it could touch -- see this
    # worker's report for the measured blast radius on a real book.
    pronunciations = dict(ctx.book.get("pronunciations") or {})
    config_hash = render_config_hash(engine.describe(), pronunciations)
    # CONTRACT.md 18: this book's per-occurrence homograph decisions, read
    # once for the whole run. A book that has no decisions gets an empty
    # document, and every input hash below is then the bare chunk sha256 --
    # exactly what it was before this rule existed, so no WAV goes stale.
    decisions_doc = homographs.read_decisions(ctx.book_dir)

    done = 0
    skipped = 0
    failed = 0
    aborted = False
    abort_reason: str | None = None
    consecutive_failures = 0
    chapters_summary: dict = {}

    # Pre-scan: find the work that actually needs doing, and its total chars,
    # so the progress line below can report a meaningful ETA.
    # (chapter_id, chunk_record, total_in_chapter, chunk_decisions, input_hash)
    plan: list[tuple[str, dict, int, list, str]] = []
    total_remaining_chars = 0
    for chapter_id in ids:
        index_path = ctx.stage_dir("chunk") / chapter_id / "index.json"
        index = read_json(index_path)
        if not index or "chunks" not in index:
            failed += 1
            chapters_summary[chapter_id] = {"error": "missing or invalid chunk index"}
            continue

        total_in_chapter = len(index["chunks"])
        chapters_summary[chapter_id] = {"chunks": total_in_chapter}

        for record in index["chunks"]:
            out_path = ctx.stage_dir("render") / chapter_id / f"{record['id']}.wav"
            chunk_decisions = homographs.decisions_for_chunk(
                decisions_doc, chapter_id, record["id"]
            )
            input_hash = homographs.chunk_input_hash(record["sha256"], chunk_decisions)
            if force or not is_fresh(out_path, input_hash, config_hash):
                plan.append((chapter_id, record, total_in_chapter, chunk_decisions, input_hash))
                total_remaining_chars += record["chars"]
            else:
                skipped += 1

    chars_done = 0
    t_start = time.monotonic()

    for chapter_id, record, total_in_chapter, chunk_decisions, input_hash in plan:
        chunk_txt_path = ctx.stage_dir("chunk") / chapter_id / record["file"]
        out_path = ctx.stage_dir("render") / chapter_id / f"{record['id']}.wav"

        try:
            # The chunk file on disk keeps the author's spelling always
            # ("Gyko") -- apply_pronunciations only changes this in-memory
            # copy, which is what actually reaches the engine.
            text = chunk_txt_path.read_text(encoding="utf-8")
            # CONTRACT.md 18.5: the homograph markup goes in FIRST, before
            # the pronunciation map. The markup's offsets are indexed
            # against the on-disk text. Reverse the order and a
            # pronunciation entry can match inside the bracketed word and
            # corrupt the markup. homographs.validate() refuses a book that
            # names one word in both tables, so the two never overlap.
            text = homographs.apply_homographs(text, chunk_decisions)
            text = apply_pronunciations(text, pronunciations)

            t0 = time.monotonic()
            audio, sample_rate = render_chunk(engine, text)
            chunk_elapsed = time.monotonic() - t0

            _write_wav_atomic(out_path, audio, sample_rate)

            duration_s = len(audio) / sample_rate if sample_rate else 0.0
            write_meta(
                out_path,
                "render",
                input_hash,
                config_hash,
                extra={"duration_s": duration_s},
            )

            done += 1
            chars_done += record["chars"]
            total_elapsed = time.monotonic() - t_start
            cps = chars_done / total_elapsed if total_elapsed > 0 else 0.0
            remaining_chars = max(total_remaining_chars - chars_done, 0)
            eta_s = remaining_chars / cps if cps > 0 else 0.0

            print(
                f"[render] {chapter_id} chunk {record['id']}/{total_in_chapter:04d} "
                f"({record['chars']} chars in {chunk_elapsed:.2f}s) "
                f"cps={cps:.1f} eta={_fmt_duration(eta_s)}",
                flush=True,
            )
            consecutive_failures = 0
        except Exception as exc:  # an 8-hour job must survive one bad chunk...
            failed += 1
            consecutive_failures += 1
            print(f"[render] FAILED {chapter_id} chunk {record['id']}: {exc!r}", flush=True)

            if _is_fatal_disk_error(exc):
                # ...but not a full disk. Continuing here just repeats this
                # same failure on every remaining chunk of the run.
                aborted = True
                abort_reason = f"fatal disk error at {chapter_id} chunk {record['id']}: {exc!r}"
                print(f"[render] ABORT: {abort_reason}", flush=True)
                break

            if consecutive_failures >= max_consecutive_failures:
                aborted = True
                abort_reason = (
                    f"{consecutive_failures} consecutive chunk failures, "
                    f"most recently {chapter_id} chunk {record['id']}: {exc!r}"
                )
                print(f"[render] ABORT: {abort_reason}", flush=True)
                break

    return {
        "stage": "render",
        "done": done,
        "skipped": skipped,
        "failed": failed,
        "chapters": chapters_summary,
        "aborted": aborted,
        "abort_reason": abort_reason,
    }
