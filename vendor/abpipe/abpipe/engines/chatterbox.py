"""ChatterboxEngine — a stub. CONTRACT.md section 8.

`describe()` works so the engine can be selected and hashed. `synthesize()`
raises NotImplementedError; the plan's fallback path is Kokoro (kokoro_mlx,
then kokoro_cpu), not Chatterbox.
"""

from __future__ import annotations


class ChatterboxEngine:
    """A stub engine. Not implemented."""

    name = "chatterbox"

    def __init__(self, config: dict) -> None:
        self._config = dict(config)

    def describe(self) -> dict:
        """Return the deterministic configuration. No paths, no timestamps."""
        extra = {k: v for k, v in self._config.items() if k != "name"}
        return {"name": self.name, **extra}

    def synthesize(self, text: str) -> tuple["numpy.ndarray", int]:
        raise NotImplementedError(
            "ChatterboxEngine is a stub. The fallback path in the plan is "
            "kokoro_mlx, then kokoro_cpu — not chatterbox. Implement this "
            "only if both Kokoro engines are unusable."
        )
