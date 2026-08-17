"""The single ffmpeg/ffprobe runner for the pipeline.

CONTRACT.md section 12.1 defines this module. Stages 6, 7, and 8 call ffmpeg
and ffprobe only through this module. A test stubs this module. A test never
runs ffmpeg on real audio, except one narrow synthetic-audio smoke test.

A silent ffmpeg failure in an 8-hour pipeline is the worst outcome, so every
failure here raises loudly with the tail of stderr attached.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

_ffmpeg_bin: str | None = None
_ffprobe_bin: str | None = None


class FFmpegError(RuntimeError):
    """Raised when an ffmpeg or ffprobe invocation fails, or produces bad output."""


def _resolve_binary(env_var: str, name: str) -> str:
    override = os.environ.get(env_var)
    if override:
        return override
    found = shutil.which(name)
    if not found:
        raise FFmpegError(
            f"{name} was not found on PATH. Set {env_var} to its location."
        )
    return found


def ffmpeg_bin() -> str:
    """Return the resolved path of the ffmpeg binary. Resolved once, then cached."""
    global _ffmpeg_bin
    if _ffmpeg_bin is None:
        _ffmpeg_bin = _resolve_binary("ABPIPE_FFMPEG", "ffmpeg")
    return _ffmpeg_bin


def ffprobe_bin() -> str:
    """Return the resolved path of the ffprobe binary. Resolved once, then cached."""
    global _ffprobe_bin
    if _ffprobe_bin is None:
        _ffprobe_bin = _resolve_binary("ABPIPE_FFPROBE", "ffprobe")
    return _ffprobe_bin


def reset_binaries() -> None:
    """Forget the cached binary paths. A test calls this after changing the env."""
    global _ffmpeg_bin, _ffprobe_bin
    _ffmpeg_bin = None
    _ffprobe_bin = None


def _tail(text: str, n: int = 30) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-n:])


def run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run ffmpeg with the given argument list. Return the completed process.

    `args` holds only the ffmpeg-specific arguments: inputs, filters, and the
    output path. This function prepends `-nostdin -hide_banner -loglevel
    error`, and adds `-y` unless the caller already passed `-y` or `-n`.
    Both stdout and stderr are captured as text. When `check` is True and the
    process exits non-zero, this function raises FFmpegError with the last
    ~30 lines of stderr in the message.
    """
    binary = ffmpeg_bin()
    prefix = ["-nostdin", "-hide_banner"]
    if "-loglevel" not in args:
        prefix = prefix + ["-loglevel", "error"]
    if "-y" not in args and "-n" not in args:
        prefix = prefix + ["-y"]
    argv = [binary, *prefix, *args]
    proc = subprocess.run(argv, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise FFmpegError(
            f"ffmpeg failed (exit {proc.returncode}): {' '.join(argv)}\n"
            f"--- last {min(30, len(proc.stderr.splitlines()))} line(s) of stderr ---\n"
            f"{_tail(proc.stderr)}"
        )
    return proc


def probe_json(path: str | os.PathLike) -> dict:
    """Return the ffprobe JSON output (format, streams, chapters) of a media file."""
    binary = ffprobe_bin()
    argv = [
        binary,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        "-show_chapters",
        str(path),
    ]
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FFmpegError(
            f"ffprobe failed (exit {proc.returncode}): {' '.join(argv)}\n"
            f"--- last stderr ---\n{_tail(proc.stderr)}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise FFmpegError(
            f"ffprobe produced unparsable JSON for {path}: {exc}\n"
            f"--- stdout ---\n{proc.stdout[:2000]}"
        ) from exc


def probe_duration(path: str | os.PathLike) -> float:
    """Return the duration of a media file in seconds, via ffprobe's format block."""
    data = probe_json(path)
    fmt = data.get("format") or {}
    duration = fmt.get("duration")
    if duration is None:
        raise FFmpegError(f"ffprobe returned no duration for {path}")
    return float(duration)
