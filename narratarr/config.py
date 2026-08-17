"""The settings object.

APP-CONTRACT.md section 10 defines every `NARRATARR_*` variable and its default.
This module reads the environment once and gives every other module the same
values. **A path is never hard-coded anywhere else in the app.** A module that
needs a path, a secret, or a tunable value calls `get_settings()`.

Note on scope: three fields below (`watch_interval_s`, `watch_delete_after_ingest`,
`events_per_job_max`) are not in the section 10 table. Section 7 and section 4.5
document their defaults in prose. This module reads them from the environment too,
under the same `NARRATARR_` prefix, so no module hard-codes them either. Flag this
gap to the overlord: section 10 may want these three rows added.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# HF_HUB_DISABLE_XET has no NARRATARR_ prefix. It is a HuggingFace transport
# switch, not a Narratarr setting. Section 11.1 requires the default "1" so that
# a user never has to find the workaround. Set it as early as import time, so
# every later import of `huggingface_hub` sees it.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")


def _env_bool(name: str, default: bool) -> bool:
    """Return the boolean value of an environment variable.

    A value of "true", "1", "yes", or "on" (any case) is True. A value of
    "false", "0", "no", or "off" (any case) is False. An absent variable
    returns the default.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("true", "1", "yes", "on")


def _env_int(name: str, default: int) -> int:
    """Return the integer value of an environment variable, or the default."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class Settings:
    """Every configurable value of Narratarr, read from the environment.

    Refer to APP-CONTRACT.md section 10 for the source of truth. Build one
    with `Settings.from_env()`. Do not construct one field by field outside
    a test.
    """

    config_dir: Path = Path("/config")
    output_dir: Path = Path("/output")
    watch_dir: Path = Path("/watch")
    port: int = 8000
    log_level: str = "info"
    api_key: str = ""
    engine: str = "kokoro_cpu"
    voice: str = "bm_george"
    lang_code: str = "b"
    num_threads: int = 3
    whisper_backend: str = "faster"
    whisper_model: str = ""
    sample_gate: bool = True
    prune: bool = False
    abs_token: str = ""
    hf_hub_disable_xet: str = "1"

    # Documented in prose (section 7, section 4.5), not in the section 10 table.
    # Refer to the module docstring.
    watch_interval_s: int = 60
    watch_delete_after_ingest: bool = False
    events_per_job_max: int = 5000

    @classmethod
    def from_env(cls) -> "Settings":
        """Build the settings object from the current environment."""
        return cls(
            config_dir=Path(os.environ.get("NARRATARR_CONFIG_DIR", "/config")),
            output_dir=Path(os.environ.get("NARRATARR_OUTPUT_DIR", "/output")),
            watch_dir=Path(os.environ.get("NARRATARR_WATCH_DIR", "/watch")),
            port=_env_int("NARRATARR_PORT", 8000),
            log_level=os.environ.get("NARRATARR_LOG_LEVEL", "info"),
            api_key=os.environ.get("NARRATARR_API_KEY", ""),
            engine=os.environ.get("NARRATARR_ENGINE", "kokoro_cpu"),
            voice=os.environ.get("NARRATARR_VOICE", "bm_george"),
            lang_code=os.environ.get("NARRATARR_LANG_CODE", "b"),
            num_threads=_env_int("NARRATARR_NUM_THREADS", 3),
            whisper_backend=os.environ.get("NARRATARR_WHISPER_BACKEND", "faster"),
            whisper_model=os.environ.get("NARRATARR_WHISPER_MODEL", ""),
            sample_gate=_env_bool("NARRATARR_SAMPLE_GATE", True),
            prune=_env_bool("NARRATARR_PRUNE", False),
            abs_token=os.environ.get("NARRATARR_ABS_TOKEN", ""),
            hf_hub_disable_xet=os.environ.get("HF_HUB_DISABLE_XET", "1"),
            watch_interval_s=_env_int("NARRATARR_WATCH_INTERVAL_S", 60),
            watch_delete_after_ingest=_env_bool(
                "NARRATARR_WATCH_DELETE_AFTER_INGEST", False
            ),
            events_per_job_max=_env_int("NARRATARR_EVENTS_PER_JOB_MAX", 5000),
        )

    # ------------------------------------------------------------------ paths
    # Every path below derives from config_dir. Refer to APP-CONTRACT.md 2.1.

    @property
    def db_path(self) -> Path:
        """Return the path of the sqlite database."""
        return self.config_dir / "narratarr.db"

    @property
    def models_dir(self) -> Path:
        """Return the directory that holds the downloaded models."""
        return self.config_dir / "models"

    @property
    def library_dir(self) -> Path:
        """Return the directory that holds the ingested ebook files."""
        return self.config_dir / "library"

    @property
    def work_dir(self) -> Path:
        """Return the directory that holds one subdirectory for each book."""
        return self.config_dir / "work"

    @property
    def logs_dir(self) -> Path:
        """Return the directory that holds the application log."""
        return self.config_dir / "logs"

    def ensure_directories(self) -> None:
        """Make every runtime directory. A present directory is not an error."""
        for path in (
            self.config_dir,
            self.output_dir,
            self.watch_dir,
            self.models_dir,
            self.library_dir,
            self.work_dir,
            self.logs_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the settings object, built once from the environment.

    A test that needs different settings sets the environment first, then
    calls `get_settings.cache_clear()` before this function runs again.
    """
    return Settings.from_env()


# A module-level singleton, per APP-CONTRACT.md section 14.1. Built at import
# time. A module that must see a settings change made after import (every
# test, and the runner) calls `get_settings()` directly instead of this name.
settings: Settings = get_settings()
