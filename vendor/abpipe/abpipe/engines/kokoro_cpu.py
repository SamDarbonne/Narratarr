"""KokoroCPUEngine — the fallback TTS engine. CONTRACT.md section 8.

Uses the PyTorch `kokoro` package (`uv pip install kokoro`, version 0.9.4
at capture time; it pulls `torch`, `huggingface_hub`, `loguru`, and
`misaki[en]>=0.9.4`). The confirmed API, read from the installed source at
`kokoro/pipeline.py` and `kokoro/model.py`:

    from kokoro.pipeline import KPipeline
    pipeline = KPipeline(lang_code="b", repo_id="hexgrad/Kokoro-82M", device="cpu")
    for result in pipeline(text, voice="bm_george", speed=1.0):
        result.graphemes   # str, the text of this segment
        result.phonemes    # str, the misaki phoneme string of this segment
        result.audio       # torch.FloatTensor, shape (samples,), or None

`KPipeline.__call__` is a generator (`pipeline.py:351-386`). It first splits
the input on `split_pattern` (default `r"\\n+"`), and misaki's own front end
can further split a long segment at its 510-phoneme limit (`en_tokenize`,
`pipeline.py:195-221`). So one call to a pipeline can yield many `Result`
objects even for a single short chunk. **Every yielded segment must be
concatenated, never just the first** — `kokoro_mlx.py` documents the
identical trap for `mlx-audio`, and this package works the same way.

Dependency note: constructing `KPipeline` with `model=True` (the default)
downloads the acoustic weights through `huggingface_hub.hf_hub_download`,
and each voice pack the same way on first use. **On this Mac the `xet`
transport 404s; export `HF_HUB_DISABLE_XET=1` before any call that loads a
model or a voice.** `KPipeline(model=False)` builds only the misaki text
front end (`g2p`) and calls no downloader at all — `tools/phoneme_parity.py`
uses that mode to capture phonemes without pulling any weights.

--- The espeak fallback hazard (CONTRACT.md 17.1) ---

Read `kokoro/pipeline.py:106-113`:

    try:
        fallback = espeak.EspeakFallback(british=lang_code == 'b')
    except Exception as e:
        logger.warning("EspeakFallback not Enabled: OOD words will be skipped")
        logger.warning({str(e)})
        fallback = None
    self.g2p = en.G2P(trf=trf, british=lang_code == 'b', fallback=fallback, unk='')

The warning string is byte-identical to the one CONTRACT.md 17.1 greps for
in the `mlx-audio` render log. **But this package logs it through `loguru`,
not the stdlib `logging` module CONTRACT.md 17.1's grep assumes, and
`kokoro/__init__.py` calls `logger.disable("kokoro")` at import time** —
which silences every loguru call made from inside the `kokoro` package,
including this one, unless a caller explicitly re-enables it with
`loguru.logger.enable("kokoro")`. This module does not do that on the
caller's behalf: reaching into another package's logger configuration from
a library is intrusive to that package's other users, and the log line is
not a reliable detection surface here regardless. `preflight()` below is
the real check for this engine: it reads `pipeline.g2p.fallback` directly,
which is `None` exactly when the fallback failed, independent of logging.

`unk=""` applies whether or not the fallback works: a word neither misaki's
lexicon nor espeak resolves is still deleted. Refer to `preflight()`.
"""

from __future__ import annotations

import logging

# The phonemizer emits a "words count mismatch" warning on almost every
# call. It is benign; left alone it floods an 800+ chunk render log. Same
# guard as kokoro_mlx.py, against the same underlying phonemizer-fork
# dependency (both packages route through the same misaki install).
logging.getLogger("phonemizer").setLevel(logging.ERROR)

_DEFAULT_REPO_ID = "hexgrad/Kokoro-82M"

# A two-word probe confirmed out-of-lexicon for misaki's `en` front end, in
# both langs this project uses. Read the report of the worker that added
# this module for the measurement: with no espeak fallback and misaki's
# `unk=""`, `en.G2P(fallback=None, unk="")("Zyrkovian Quaddlemorph")`
# returns a bare space, `" "` — both words vanish. With a working fallback
# the same call returns real phonemes, `"zˌɪəkˈQviən kwˈɒddᵊlmˌɔːf"`.
# preflight() renders exactly this text and measures which of the two
# happened.
_OOV_PROBE_TEXT = "Zyrkovian Quaddlemorph"


def _build_pipeline(lang_code: str, repo_id: str, *, load_model: bool):
    """Construct one `kokoro.pipeline.KPipeline`.

    `load_model=False` builds the misaki text front end (`g2p`) only, and
    touches no downloader — the identical G2P construction code path
    `KokoroCPUEngine` uses for real audio, minus the acoustic weights.
    `tools/phoneme_parity.py` uses this mode so a phoneme capture never
    pulls a model.

    `device="cpu"` is explicit and not left to KPipeline's own auto-detect,
    because a class named `KokoroCPUEngine` must not silently pick a GPU
    device on a machine that happens to have one.
    """
    from kokoro.pipeline import KPipeline

    return KPipeline(
        lang_code=lang_code,
        repo_id=repo_id,
        model=load_model,
        device="cpu" if load_model else None,
    )


class KokoroCPUEngine:
    """The fallback engine. Needs `uv pip install kokoro`, not installed by default."""

    name = "kokoro_cpu"

    def __init__(self, config: dict) -> None:
        self._config = dict(config)
        self.model_id = config.get("model", _DEFAULT_REPO_ID)
        self.voice = config.get("voice", "bm_george")
        self.speed = config.get("speed", 1.0)
        self.lang_code = config.get("lang_code", "b")
        self.sample_rate = config.get("sample_rate", 24000)
        # Not in describe() -- deliberately. It changes render speed, never
        # the audio. The deploy container caps this engine at 3 CPUs, and
        # torch does not discover that limit on its own.
        self.num_threads = config.get("num_threads")
        # The one escape hatch from the hazard preflight() guards. Refer to
        # CONTRACT.md 17.1 and this module's docstring.
        self.allow_no_espeak_fallback = bool(config.get("allow_no_espeak_fallback", False))
        self._pipeline = None
        self._threads_set = False

    # ------------------------------------------------------------------ lazy load

    def _ensure_pipeline(self):
        """Load the pipeline once, on first use, and reuse it for every chunk.

        It is reused across 2,000+ chunks in a real render; constructing a
        new pipeline (and re-downloading its model) per chunk is a
        run-killer, the same reason `kokoro_mlx.py`'s `_ensure_model()`
        caches on the instance.
        """
        if self._pipeline is None:
            try:
                import kokoro  # noqa: F401
                import torch
            except ImportError as exc:
                raise ImportError(
                    "KokoroCPUEngine needs PyTorch and the 'kokoro' package, which "
                    "are not installed in this environment. Run: uv pip install kokoro"
                ) from exc

            if self.num_threads is not None and not self._threads_set:
                torch.set_num_threads(int(self.num_threads))
                self._threads_set = True

            self._pipeline = _build_pipeline(self.lang_code, self.model_id, load_model=True)
        return self._pipeline

    # ------------------------------------------------------------------ interface

    def describe(self) -> dict:
        """Return the deterministic configuration. No paths, no timestamps.

        Exactly the six keys this method has always returned. Adding a key
        here stales every WAV of every delivered book -- describe() feeds
        `config_hash` (CONTRACT.md 8) -- so `num_threads` and
        `allow_no_espeak_fallback` are read in `__init__` and never appear
        below. Neither changes the audio.
        """
        return {
            "name": self.name,
            "model": self.model_id,
            "voice": self.voice,
            "speed": self.speed,
            "lang_code": self.lang_code,
            "sample_rate": self.sample_rate,
        }

    def synthesize(self, text: str) -> tuple["numpy.ndarray", int]:
        import numpy as np

        pipeline = self._ensure_pipeline()

        results = list(pipeline(text, voice=self.voice, speed=self.speed))
        # Concatenate EVERY segment the generator yields, never just the
        # first. Refer to this module's docstring for the trap.
        parts = [
            r.audio.detach().cpu().numpy().astype(np.float32)
            for r in results
            if r.audio is not None
        ]
        if not parts:
            raise RuntimeError(f"kokoro_cpu produced no audio segments for text: {text!r}")

        audio = parts[0] if len(parts) == 1 else np.concatenate(parts)
        audio = np.reshape(audio, -1)
        assert audio.ndim == 1
        return audio, self.sample_rate

    # ------------------------------------------------------------------ preflight (not part of the Engine protocol)

    def preflight(self) -> dict:
        """Construct the pipeline, verify the espeak fallback exists, render a
        short warmup text, and return a report dict.

        This is an extra method, not part of the `Engine` protocol in
        `engines/__init__.py`. It exists because a container gives this
        engine a fresh filesystem every time, and the espeak fallback
        hazard (CONTRACT.md 17.1) is exactly the kind of fault that fresh
        filesystem invites: a missing `espeak-ng` binary, or a full disk
        stopping `EspeakFallback` from unpacking its shared library.

        Raises `RuntimeError` when the fallback failed to construct, unless
        the config sets `allow_no_espeak_fallback: true`. CONTRACT.md 17.1:
        with no fallback, misaki's G2P is built with `unk=""`, and **every
        out-of-lexicon word is deleted from the audio with no further
        warning.** QC cannot see the loss, because the transcript and the
        source lose the same word together.

        The warmup text IS the out-of-lexicon probe (`_OOV_PROBE_TEXT`), a
        two-word nonsense phrase confirmed out-of-lexicon for misaki's `en`
        front end in both langs this project uses. One render both warms
        the pipeline and exercises the fallback path for real, because a
        healthy `espeak_fallback: True` is not proof the fallback actually
        RUNS for this book's words -- only a real out-of-lexicon render is.
        """
        pipeline = self._ensure_pipeline()
        fallback = getattr(pipeline.g2p, "fallback", None)
        has_fallback = fallback is not None

        if not has_fallback:
            if not self.allow_no_espeak_fallback:
                raise RuntimeError(
                    "kokoro_cpu built its misaki G2P with NO espeak fallback. "
                    "CONTRACT.md 17.1: 'a word the misaki lexicon does not hold "
                    "is spoken by the espeak fallback. If that fallback fails to "
                    "construct, ... the pipeline ... drops every unknown word "
                    "from the audio, silently.' Every out-of-lexicon word in "
                    "this book will be deleted with no further warning, and QC "
                    "cannot see the loss. Install espeak-ng (the fallback needs "
                    "the `espeak-ng` binary on PATH), or set the engine config "
                    "key allow_no_espeak_fallback: true to render anyway and "
                    "accept the silent word loss."
                )
            logging.getLogger(__name__).warning(
                "kokoro_cpu has NO espeak fallback, and allow_no_espeak_fallback "
                "is set. Every out-of-lexicon word in this book will be deleted "
                "from the audio with no further warning. This is the hazard "
                "CONTRACT.md 17.1 documents."
            )

        audio, sample_rate = self.synthesize(_OOV_PROBE_TEXT)
        rms = float((audio.astype("float64") ** 2).mean() ** 0.5) if audio.size else 0.0

        return {
            "espeak_fallback": has_fallback,
            "warmup_samples": int(audio.shape[0]),
            "warmup_sample_rate": int(sample_rate),
            "oov_probe_word": _OOV_PROBE_TEXT,
            # A working fallback speaks the probe; a broken one renders it
            # from an empty phoneme string, which is near silence. The
            # threshold is set well above measured silence-floor RMS and
            # well below a real spoken phrase's RMS -- refer to the report
            # for both measured numbers.
            "oov_probe_nonempty": rms > 0.01,
        }
