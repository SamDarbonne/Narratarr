"""The build-time espeak-fallback probe. Read APP-CONTRACT.md 11.2.

**WARNING: this step guards against a silent data-loss fault. Do not
remove it to make the build faster.** Read this whole comment before you
touch it.

When the espeak fallback fails to construct, misaki is built with
`unk=""`, and every out-of-lexicon word is deleted from the audio, with
no second warning. QC cannot find the loss, because the transcript and
the source lose the same word. Refer to vendor/abpipe/CONTRACT.md section
17.1.

**WARNING: a log grep cannot detect this fault on the engine Narratarr
uses.** The torch `kokoro` package calls `logger.disable("kokoro")` on
import, through loguru, which silences every log call inside the
package, the fallback warning included. A build check that greps a log
line proves nothing on this engine. Refer to `scripts/espeak_guard.py`
for the secondary, log-based check that still earns its place for other
engines.

**The real check reads the pipeline object.** `KokoroCPUEngine.preflight()`
constructs the pipeline, reads `pipeline.g2p.fallback` directly — `None`
means construction failed — and renders a probe word that is certainly
not in the misaki lexicon, so a near-silent probe means the word was
dropped even though the fallback exists. This script calls that method,
so the build and the runtime share one implementation. Refer to
`vendor/abpipe/abpipe/engines/kokoro_cpu.py`.

This script has a second, load-bearing effect: it forces
`espeakng_loader` to unpack the `libespeak-ng` shared library into the
image now, at build time, with a full build machine's worth of free
disk. A container built from this image then finds that file already
unpacked, so a full disk at run time cannot break the unpack step that
causes the fault above. Refer to vendor/abpipe/CONTRACT.md section 17.1.

**This script needs network access and the real Kokoro weights to run.**
The Dockerfile deletes the Hugging Face cache in the same `RUN`
instruction that calls this script, so the weights never reach a
committed image layer. Refer to APP-CONTRACT.md section 11.1: the image
does not hold the TTS weights.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

try:
    # kokoro silences its own logger on import (refer to the warning
    # above). Re-enable it here only so that a failure of this script
    # prints a useful trace. This has no effect on the application's own
    # runtime logging; that is a separate, in-process concern for the
    # code that starts the server.
    from loguru import logger as _loguru_logger

    _loguru_logger.enable("kokoro")
except ImportError:
    pass

# The probe engine's configuration. It matches the image's default
# engine, so the build checks the same code path a real render uses.
PROBE_CONFIG = {
    "model": "hexgrad/Kokoro-82M",
    "voice": "bm_george",
    "lang_code": "b",
}


def main() -> int:
    try:
        from abpipe.engines.kokoro_cpu import KokoroCPUEngine
    except ImportError as exc:
        print(f"FAIL: cannot import KokoroCPUEngine: {exc}", file=sys.stderr)
        return 1

    engine = KokoroCPUEngine(PROBE_CONFIG)

    if not hasattr(engine, "preflight"):
        print(
            "FAIL: this copy of vendor/abpipe has no "
            "KokoroCPUEngine.preflight() method. The build needs the "
            "re-vendored copy that adds it. Ask the overlord to "
            "re-vendor vendor/abpipe before building this image.",
            file=sys.stderr,
        )
        return 1

    try:
        result = engine.preflight()
    except Exception as exc:  # noqa: BLE001 - a build probe reports every fault
        print(f"FAIL: KokoroCPUEngine.preflight() raised: {exc}", file=sys.stderr)
        return 1

    print(f"preflight() result: {result}")

    if not result.get("espeak_fallback"):
        print(
            "FAIL: the espeak fallback did not construct "
            "(pipeline.g2p.fallback is None). Every out-of-lexicon word "
            "would be silently deleted from every render on this image. "
            "Refer to vendor/abpipe/CONTRACT.md section 17.1.",
            file=sys.stderr,
        )
        return 1

    if not result.get("warmup_samples"):
        print("FAIL: the warmup render produced no audio samples.", file=sys.stderr)
        return 1

    if not result.get("oov_probe_nonempty"):
        word = result.get("oov_probe_word", "<unknown>")
        print(
            f"FAIL: the probe word {word!r} rendered as near-silence. "
            "The fallback object exists but did not speak the word. "
            "Refer to vendor/abpipe/CONTRACT.md section 17.1.",
            file=sys.stderr,
        )
        return 1

    print(
        "OK: the espeak fallback constructed and spoke the "
        f"out-of-lexicon probe word {result.get('oov_probe_word')!r}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
