"""KokoroMLXEngine — the primary TTS engine. CONTRACT.md section 8.

Uses `mlx-audio`. The confirmed API, read from the installed source at
`mlx_audio/tts/utils.py` and `mlx_audio/tts/models/kokoro/kokoro.py`:

    from mlx_audio.tts.utils import load_model
    model = load_model("mlx-community/Kokoro-82M-bf16")
    for seg in model.generate(text=TEXT, voice="bm_george", speed=1.0, lang_code="b"):
        seg.audio          # mx.array, float32, shape (samples,)
        seg.sample_rate     # 24000

`generate()` is a generator. It splits the input on `split_pattern` (default
`r"\\n+"`) and yields one `GenerationResult` per segment, so every yielded
segment must be concatenated — never just the first — even though a
single-paragraph chunk is almost always one segment.

Dependency note: Kokoro's text front end needs the `misaki` package with its
`en` extra (`uv pip install "misaki[en]"`, which pulls spacy, num2words, and
phonemizer-fork). Without it `model.generate()` raises
`ImportError: Kokoro requires the optional 'misaki' package for text processing.`
That message under-states the fix (plain `misaki` is not enough — the `en`
extra is required), so `synthesize()` re-raises it with the exact command.
"""

from __future__ import annotations

import logging

# The phonemizer emits a "words count mismatch" warning on almost every call.
# It is benign; left alone it floods an 800+ chunk render log.
logging.getLogger("phonemizer").setLevel(logging.ERROR)

_VOICE_LANG_PREFIX = {"a": ("af_", "am_"), "b": ("bf_", "bm_")}


class KokoroMLXEngine:
    """The primary engine. Loads `mlx-community/Kokoro-82M-bf16` via mlx-audio."""

    name = "kokoro_mlx"

    def __init__(self, config: dict) -> None:
        self._config = dict(config)
        self.model_id = config.get("model", "mlx-community/Kokoro-82M-bf16")
        self.voice = config.get("voice", "bm_george")
        self.speed = config.get("speed", 1.0)
        self.lang_code = config.get("lang_code", "b")
        self.sample_rate = config.get("sample_rate", 24000)
        self._model = None
        self._warned_lang_mismatch = False

    # ------------------------------------------------------------------ lazy load

    def _ensure_model(self):
        """Load the model once, on first use, and reuse it for every chunk."""
        if self._model is None:
            from mlx_audio.tts.utils import load_model
            self._model = load_model(self.model_id)
        return self._model

    def _check_lang_code(self) -> None:
        prefixes = _VOICE_LANG_PREFIX.get(self.lang_code)
        if prefixes and not self.voice.startswith(prefixes) and not self._warned_lang_mismatch:
            logging.getLogger(__name__).warning(
                "voice %r does not match lang_code %r (expected a prefix in %r)",
                self.voice, self.lang_code, prefixes,
            )
            self._warned_lang_mismatch = True

    # ------------------------------------------------------------------ interface

    def describe(self) -> dict:
        """Return the deterministic configuration. No paths, no timestamps."""
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
        import mlx.core as mx

        self._check_lang_code()
        model = self._ensure_model()

        try:
            segments = list(model.generate(
                text=text,
                voice=self.voice,
                speed=self.speed,
                lang_code=self.lang_code,
            ))
        except ImportError as exc:
            raise ImportError(
                "kokoro_mlx needs the 'en' extra of misaki for text processing "
                "(spacy, num2words, phonemizer-fork). Run: "
                'uv pip install "misaki[en]"'
            ) from exc

        if not segments:
            raise RuntimeError(f"kokoro_mlx produced no audio segments for text: {text!r}")

        audio_parts = [mx.reshape(seg.audio, (-1,)) for seg in segments]
        audio = audio_parts[0] if len(audio_parts) == 1 else mx.concatenate(audio_parts)
        arr = np.asarray(audio, dtype=np.float32)
        sr = int(segments[0].sample_rate)
        return arr, sr
