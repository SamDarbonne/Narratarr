"""Stage 6 — assemble: join chunk WAVs into a chapter WAV, then a chapter m4a.

CONTRACT.md section 10 defines this stage. Owner: Worker D.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import soundfile as sf

from abpipe import ffmpeg
from abpipe.meta import (
    clear_meta,
    hash_file,
    hash_many,
    hash_obj,
    is_fresh,
    read_json,
    write_meta,
    write_text,
)

STAGE = "assemble"

# --------------------------------------------------------------------------- config

TRIM_THRESHOLD_DBFS = -50.0
FADE_S = 0.005

SILENCE_INSIDE_PARAGRAPH_S = 0.35
SILENCE_ENDS_PARAGRAPH_S = 0.70
SILENCE_AFTER_HEADING_S = 1.20
SILENCE_CHAPTER_END_S = 2.00

LOUDNORM = {"I": -18.0, "TP": -2.0, "LRA": 11.0}


def _config() -> dict:
    """Return the part of the configuration that changes the output of this stage."""
    return {
        "silence": {
            "inside_paragraph": SILENCE_INSIDE_PARAGRAPH_S,
            "ends_paragraph": SILENCE_ENDS_PARAGRAPH_S,
            "after_heading": SILENCE_AFTER_HEADING_S,
            "chapter_end": SILENCE_CHAPTER_END_S,
        },
        "trim_threshold_dbfs": TRIM_THRESHOLD_DBFS,
        "fade_s": FADE_S,
        "loudnorm": LOUDNORM,
    }


def _config_hash() -> str:
    return hash_obj(_config())


# --------------------------------------------------------------------------- audio math


def _db_to_amplitude(db: float) -> float:
    return 10.0 ** (db / 20.0)


def trim_silence(samples: np.ndarray, threshold_dbfs: float = TRIM_THRESHOLD_DBFS) -> np.ndarray:
    """Trim leading and trailing near-silence at a dBFS threshold.

    A sample is "silence" when its absolute value is at or below the
    threshold amplitude. The function keeps everything between the first
    and the last sample that is louder than the threshold.
    """
    if samples.size == 0:
        return samples
    threshold = _db_to_amplitude(threshold_dbfs)
    above = np.flatnonzero(np.abs(samples) > threshold)
    if above.size == 0:
        return samples[:0]
    return samples[above[0] : above[-1] + 1]


def fade_edges(samples: np.ndarray, sample_rate: int, fade_s: float = FADE_S) -> np.ndarray:
    """Apply a linear fade-in and a linear fade-out to the edges of one chunk."""
    n = samples.shape[0]
    if n == 0:
        return samples
    fade_len = min(int(round(fade_s * sample_rate)), n // 2)
    if fade_len <= 0:
        return samples
    out = samples.astype(np.float64, copy=True)
    ramp = np.linspace(0.0, 1.0, fade_len, dtype=np.float64)
    out[:fade_len] *= ramp
    out[-fade_len:] *= ramp[::-1]
    return out


def silence_seconds_after(chunk: dict, is_last: bool) -> float:
    """Return the silence to insert after this chunk, per the CONTRACT.md 10.1 table.

    The rule is a priority order: the end of the chapter wins over the
    heading, the heading wins over `ends_paragraph`, and the paragraph
    default applies otherwise.
    """
    if is_last:
        return SILENCE_CHAPTER_END_S
    if chunk.get("is_heading"):
        return SILENCE_AFTER_HEADING_S
    if chunk.get("ends_paragraph"):
        return SILENCE_ENDS_PARAGRAPH_S
    return SILENCE_INSIDE_PARAGRAPH_S


def silence_samples(seconds: float, sample_rate: int) -> int:
    return int(round(seconds * sample_rate))


# --------------------------------------------------------------------------- paths


def _chunk_index_path(ctx, chapter_id: str) -> Path:
    return ctx.stage_dir("chunk") / chapter_id / "index.json"


def _audio_dir(ctx, chapter_id: str) -> Path:
    return ctx.stage_dir("render") / chapter_id


def _wav_out_path(ctx, chapter_id: str) -> Path:
    return ctx.stage_dir("assemble") / f"{chapter_id}.wav"


def _m4a_out_path(ctx, chapter_id: str) -> Path:
    return ctx.stage_dir("assemble") / f"{chapter_id}.m4a"


def _ffmeta_path(ctx) -> Path:
    return ctx.stage_dir("assemble") / "chapters.ffmeta"


def _tmp_path(path: Path) -> Path:
    """Return the temporary-file path used for an atomic write of `path`."""
    return path.parent / (path.name + ".tmp")


def _cleanup_tmp(tmp_path: Path) -> None:
    """Remove a stray temp file after a failed write. Never raises."""
    try:
        tmp_path.unlink()
    except OSError:
        pass


def _load_chunk_index(ctx, chapter_id: str) -> dict:
    index_path = _chunk_index_path(ctx, chapter_id)
    data = read_json(index_path)
    if not isinstance(data, dict) or not isinstance(data.get("chunks"), list):
        raise RuntimeError(
            f"assemble: missing or unreadable chunk index for {chapter_id}: {index_path}"
        )
    return data


# --------------------------------------------------------------------------- QC gate


def _qc_report_path(ctx) -> Path:
    return ctx.stage_dir("qc") / "qc-report.json"


def find_needs_human(ctx) -> list[str]:
    """Return a sorted list of "<chapter>/<chunk>" ids marked needs_human."""
    offenders: list[str] = []
    qc_dir = ctx.stage_dir("qc")
    for chapter_dir in sorted(qc_dir.glob("ch*")):
        if not chapter_dir.is_dir():
            continue
        for chunk_path in sorted(chapter_dir.glob("*.json")):
            data = read_json(chunk_path)
            if isinstance(data, dict) and data.get("resolution") == "needs_human":
                chapter = data.get("chapter") or chapter_dir.name
                chunk = data.get("chunk") or chunk_path.stem
                offenders.append(f"{chapter}/{chunk}")
    return sorted(offenders)


def _report_is_green(report: dict) -> bool:
    """Return whether a qc-report.json is green.

    Prefers Worker C's `abpipe.qc.report_is_green`, imported lazily so this
    module still works while qc.py is mid-edit. Falls back to the documented
    `status == "green"` rule (CONTRACT.md section 9.4) when that import fails.
    """
    try:
        from abpipe.qc import report_is_green
    except Exception:
        return report.get("status") == "green"
    try:
        return bool(report_is_green(report))
    except Exception:
        return report.get("status") == "green"


def _qc_gate(ctx, allow_unverified: bool) -> None:
    if allow_unverified:
        return
    report_path = _qc_report_path(ctx)
    report = read_json(report_path)
    if not isinstance(report, dict):
        raise RuntimeError(
            "assemble: refusing to run — qc-report.json is missing or unreadable "
            f"at {report_path}. Run `abpipe qc` first, or pass allow_unverified=True."
        )
    if not _report_is_green(report):
        offenders = find_needs_human(ctx)
        names = ", ".join(offenders) if offenders else "(no needs_human chunk found on disk)"
        status = report.get("status")
        raise RuntimeError(
            f"assemble: refusing to run — QC status is {status!r}, not green. "
            f"Offending chunks: {names}. Pass allow_unverified=True to override."
        )


# --------------------------------------------------------------------------- loudnorm


def _parse_loudnorm_json(stderr_text: str) -> dict:
    """Parse the loudnorm measurement JSON out of ffmpeg's stderr.

    ffmpeg prints the JSON block among other lines, so this finds the last
    `{ ... }` region rather than assuming the whole stream is JSON.
    """
    start = stderr_text.rfind("{")
    end = stderr_text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise RuntimeError(
            "assemble: loudnorm pass 1 produced no measurement JSON in ffmpeg "
            f"stderr:\n{stderr_text[-2000:]}"
        )
    blob = stderr_text[start : end + 1]
    try:
        return __import__("json").loads(blob)
    except ValueError as exc:
        raise RuntimeError(
            f"assemble: could not parse loudnorm measurement JSON: {exc}\n{blob}"
        ) from exc


def _loudnorm_pass1(wav_path: Path) -> dict:
    cfg = LOUDNORM
    filt = f"loudnorm=I={cfg['I']}:TP={cfg['TP']}:LRA={cfg['LRA']}:print_format=json"
    # loudnorm's measurement JSON prints at the "info" log level. The default
    # "-loglevel error" of abpipe.ffmpeg.run would silently swallow it, so
    # this call raises it back to "info" for this one pass only.
    proc = ffmpeg.run(["-loglevel", "info", "-i", str(wav_path), "-af", filt, "-f", "null", "-"])
    return _parse_loudnorm_json(proc.stderr)


def _loudnorm_pass2(wav_path: Path, m4a_path: Path, measured: dict) -> None:
    cfg = LOUDNORM
    try:
        filt = (
            f"loudnorm=I={cfg['I']}:TP={cfg['TP']}:LRA={cfg['LRA']}:"
            f"measured_I={measured['input_i']}:measured_TP={measured['input_tp']}:"
            f"measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}:"
            f"offset={measured['target_offset']}"
        )
    except KeyError as exc:
        raise RuntimeError(
            f"assemble: loudnorm pass 1 JSON is missing the field {exc}: {measured}"
        ) from exc
    m4a_path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.run(
        [
            "-i", str(wav_path),
            "-af", filt,
            "-ar", "24000",
            "-ac", "1",
            "-c:a", "aac",
            "-b:a", "64k",
            # Explicit muxer: m4a_path is a .m4a.tmp path during the atomic
            # write (Defect 3), and ffmpeg can only infer a muxer from a
            # recognized extension -- ".tmp" defeats that. "ipod" is the
            # muxer ffmpeg's own extension table maps .m4a/.m4b to.
            "-f", "ipod",
            str(m4a_path),
        ]
    )


# --------------------------------------------------------------------------- per-chapter join


def _join_chapter(ctx, chapter_id: str, chunks: list[dict]) -> tuple[np.ndarray, int]:
    """Read, trim, fade, and join the chunk WAVs of one chapter. Return (samples, sr)."""
    if not chunks:
        raise RuntimeError(f"assemble: chapter {chapter_id} has no chunks in its index")
    audio_dir = _audio_dir(ctx, chapter_id)
    pieces: list[np.ndarray] = []
    sample_rate: int | None = None
    for i, chunk in enumerate(chunks):
        wav_path = audio_dir / f"{chunk['id']}.wav"
        if not wav_path.exists():
            raise RuntimeError(
                f"assemble: missing rendered audio for {chapter_id}/{chunk['id']}.wav "
                "— run render (and qc) first"
            )
        samples, file_sr = sf.read(str(wav_path), dtype="float64", always_2d=False)
        if samples.ndim > 1:
            samples = samples[:, 0]
        if sample_rate is None:
            sample_rate = file_sr
        elif file_sr != sample_rate:
            raise RuntimeError(
                f"assemble: sample rate mismatch in {chapter_id}: "
                f"{chunk['id']}.wav is {file_sr} Hz, chapter started at {sample_rate} Hz"
            )
        processed = fade_edges(trim_silence(samples), sample_rate)
        pieces.append(processed)
        gap_s = silence_seconds_after(chunk, is_last=(i == len(chunks) - 1))
        pieces.append(np.zeros(silence_samples(gap_s, sample_rate), dtype=np.float64))
    joined = np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.float64)
    return joined, sample_rate  # type: ignore[return-value]


def _process_chapter(ctx, chapter_id: str, force: bool) -> str:
    """Do the work of one chapter. Return 'done' or 'skipped'."""
    index = _load_chunk_index(ctx, chapter_id)
    chunks = index["chunks"]
    audio_dir = _audio_dir(ctx, chapter_id)

    chunk_hashes = []
    for chunk in chunks:
        wav_path = audio_dir / f"{chunk['id']}.wav"
        if not wav_path.exists():
            raise RuntimeError(
                f"assemble: missing rendered audio for {chapter_id}/{chunk['id']}.wav "
                "— run render (and qc) first"
            )
        chunk_hashes.append(hash_file(wav_path))
    input_hash = hash_many(chunk_hashes)
    config_hash = _config_hash()

    wav_out = _wav_out_path(ctx, chapter_id)
    m4a_out = _m4a_out_path(ctx, chapter_id)

    if (
        not force
        and is_fresh(wav_out, input_hash, config_hash)
        and is_fresh(m4a_out, input_hash, config_hash)
    ):
        return "skipped"

    if force:
        # CONTRACT.md 3.3: a stage rebuilding with --force calls clear_meta()
        # before it starts, so a stale meta cannot outlive a kill and vouch
        # for a half-written replacement.
        clear_meta(wav_out)
        clear_meta(m4a_out)

    joined, sample_rate = _join_chapter(ctx, chapter_id, chunks)

    # Atomic WAV write: temp file in the same directory, then os.replace.
    # A kill or a full disk during sf.write() leaves only the .tmp file
    # behind, never a half-written chapter WAV at the final path.
    wav_out.parent.mkdir(parents=True, exist_ok=True)
    wav_tmp = _tmp_path(wav_out)
    try:
        # format="WAV" is explicit here (unlike the final-path call sites
        # elsewhere) because the .tmp suffix defeats soundfile's
        # infer-format-from-extension.
        sf.write(str(wav_tmp), joined.astype(np.float32), sample_rate, subtype="PCM_16", format="WAV")
        os.replace(wav_tmp, wav_out)
    except BaseException:
        _cleanup_tmp(wav_tmp)
        raise

    duration_s = joined.shape[0] / sample_rate if sample_rate else 0.0
    write_meta(
        wav_out,
        STAGE,
        input_hash,
        config_hash,
        extra={"duration_s": duration_s, "samples": int(joined.shape[0]), "sample_rate": sample_rate},
    )

    # The loudnorm measure pass reads the now-finalized WAV. The encode pass
    # writes the m4a atomically too, per the same rule.
    measured = _loudnorm_pass1(wav_out)
    m4a_tmp = _tmp_path(m4a_out)
    try:
        _loudnorm_pass2(wav_out, m4a_tmp, measured)
        os.replace(m4a_tmp, m4a_out)
    except BaseException:
        _cleanup_tmp(m4a_tmp)
        raise

    write_meta(m4a_out, STAGE, input_hash, config_hash, extra={"loudnorm_measured": measured})
    return "done"


# --------------------------------------------------------------------------- ffmeta


def _write_ffmeta(ctx, force: bool) -> dict:
    """Write chapters.ffmeta once every chapter's m4a exists. Withhold it otherwise."""
    all_ids = ctx.chapter_ids()
    out_dir = ctx.stage_dir("assemble")
    missing = [cid for cid in all_ids if not (out_dir / f"{cid}.m4a").exists()]
    if missing:
        return {"written": False, "reason": f"missing m4a for: {', '.join(missing)}"}

    m4a_paths = [out_dir / f"{cid}.m4a" for cid in all_ids]
    input_hash = hash_many(hash_file(p) for p in m4a_paths)
    config_hash = _config_hash()
    ffmeta_path = _ffmeta_path(ctx)

    if not force and is_fresh(ffmeta_path, input_hash, config_hash):
        return {"written": False, "reason": "fresh"}

    if force:
        clear_meta(ffmeta_path)

    chapters_by_id = {c["id"]: c for c in ctx.chapters()}
    lines = [";FFMETADATA1"]
    offset_ms = 0
    for cid, m4a_path in zip(all_ids, m4a_paths):
        duration_ms = int(round(ffmpeg.probe_duration(m4a_path) * 1000))
        label = chapters_by_id.get(cid, {}).get("label", cid)
        lines.append("[CHAPTER]")
        lines.append("TIMEBASE=1/1000")
        lines.append(f"START={offset_ms}")
        lines.append(f"END={offset_ms + duration_ms}")
        lines.append(f"title={label}")
        offset_ms += duration_ms

    write_text(ffmeta_path, "\n".join(lines) + "\n")
    write_meta(ffmeta_path, STAGE, input_hash, config_hash, extra={"chapters": len(all_ids)})
    return {"written": True, "chapters": len(all_ids)}


# --------------------------------------------------------------------------- entry point


def run(ctx, chapters: list[str] | None = None, force: bool = False, allow_unverified: bool = False, **kw) -> dict:
    """Run the assemble stage. Return a summary dict.

    Refuses to run (raises RuntimeError) when 05-qc/qc-report.json is missing
    or its status is not 'green', unless `allow_unverified=True`.
    """
    _qc_gate(ctx, allow_unverified)

    ids = ctx.chapter_ids(only=chapters)
    done: list[str] = []
    skipped: list[str] = []
    failed: list[dict] = []

    for chapter_id in ids:
        status = _process_chapter(ctx, chapter_id, force)
        if status == "done":
            done.append(chapter_id)
        else:
            skipped.append(chapter_id)

    ffmeta = _write_ffmeta(ctx, force)

    return {
        "stage": STAGE,
        "done": done,
        "skipped": skipped,
        "failed": failed,
        "ffmeta": ffmeta,
    }
