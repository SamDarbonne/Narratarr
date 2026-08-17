"""The render-log check for the espeak fallback. Read APP-CONTRACT.md 11.2.

Read this whole comment before you touch this file.

**This check is secondary. Do not treat a clean grep as proof.** The
primary defence is the build-time object check in
`scripts/build_warmup_espeak.py`, which reads `pipeline.g2p.fallback`
directly. The reason: the torch `kokoro` package calls
`logger.disable("kokoro")` on import, through loguru. Every log call
inside that package is silent by default, the fallback warning included.
A render on the `kokoro_cpu` engine can lose the fallback with no line in
its log at all.

This grep still earns its place for two reasons:

1. The `kokoro_mlx` engine's underlying library, `mlx-audio`, logs the
   same warning through the stdlib `logging` module, which this process
   does not silence. The grep catches a broken fallback there.
2. A future engine may use stdlib logging too. The grep costs one file
   read and needs no model loaded, so keeping it costs nothing.

**Never treat an absent warning, alone, as proof the fallback worked.**
Call this check in addition to the build-time defence, never in place of
it. Refer to vendor/abpipe/CONTRACT.md section 17.1 for the full mechanism
and the measured cause, a full disk during the library unpack.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

# Warning: this module is the OPERATOR tool, not the runtime check.
#
# The runtime copy lives in narratarr/runner.py, which greps a render log as
# its own demoted secondary defence. Two correct copies of one check is how
# the next blind spot is born, so the two are held together by a test:
# tests/test_espeak_guard_agrees.py asserts that this string and the
# runner's ESPEAK_WARNING are the same string. Change one and that test
# fails.
#
# Use this module from a shell, on a book that already ran. The runner does
# not import it.
FALLBACK_WARNING = "EspeakFallback not Enabled"


class EspeakFallbackError(RuntimeError):
    """A render log holds the espeak-fallback warning.

    The audio from that render may be missing a word. Refer to
    vendor/abpipe/CONTRACT.md section 17.1.
    """


def log_holds_warning(log_text: str) -> bool:
    """Return True when log_text holds the espeak-fallback warning line."""
    return FALLBACK_WARNING in log_text


def check_render_log(log_path: Path | str) -> None:
    """Raise EspeakFallbackError when the log at log_path holds the warning.

    Read the whole file as text. A missing file is a caller error, not a
    clean result, so this function lets FileNotFoundError propagate.
    """
    path = Path(log_path)
    text = path.read_text(encoding="utf-8", errors="replace")
    if log_holds_warning(text):
        raise EspeakFallbackError(
            f"{path} holds the warning '{FALLBACK_WARNING}'. "
            "An out-of-lexicon word may be missing from this render's "
            "audio. Refer to vendor/abpipe/CONTRACT.md section 17.1."
        )


def check_render_logs(log_paths: Iterable[Path | str]) -> None:
    """Call check_render_log on every path in log_paths.

    Check every log of a book, not only the newest one. A failed attempt
    writes its own log, and the warning can sit there while the log of
    the successful run stays clean.
    """
    for log_path in log_paths:
        check_render_log(log_path)
