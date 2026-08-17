"""The engine registry. CONTRACT.md section 8.1.

`get_engine` imports each engine module lazily, inside its own branch, so a
missing optional dependency for one engine never breaks the others.
"""

from __future__ import annotations

from typing import Any, Protocol


class Engine(Protocol):
    """The interface every engine in this package obeys."""

    name: str

    def __init__(self, config: dict) -> None: ...

    def describe(self) -> dict:
        """Return the configuration that changes the audio."""
        ...

    def synthesize(self, text: str) -> tuple[Any, int]:
        """Return mono float32 samples in [-1.0, 1.0], and the sample rate."""
        ...


def get_engine(config: dict) -> Engine:
    """Return the engine named by config["name"]. Raise ValueError on an unknown name."""
    name = config.get("name")

    if name == "kokoro_mlx":
        from abpipe.engines.kokoro_mlx import KokoroMLXEngine
        return KokoroMLXEngine(config)

    if name == "kokoro_cpu":
        from abpipe.engines.kokoro_cpu import KokoroCPUEngine
        return KokoroCPUEngine(config)

    if name == "chatterbox":
        from abpipe.engines.chatterbox import ChatterboxEngine
        return ChatterboxEngine(config)

    raise ValueError(f"unknown engine: {name!r}")
