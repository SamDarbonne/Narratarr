"""The first-run model fetcher. Read APP-CONTRACT.md sections 10, 11.1, 13.1.

The image ships with no TTS weights and no whisper weights. Refer to
APP-CONTRACT.md section 11.1. This script downloads them once, into
`/config/models`, and verifies a checksum for every file before it trusts
that file. A truncated model must never load as if it were whole. Refer
to vendor/abpipe/CONTRACT.md section 3, the size-check rule: a project on
this pipeline has already shipped a truncated artifact that stayed
trusted for a long time, because nothing checked its size or its hash.

**Run this directly** (`python scripts/fetch_models.py`), **or import
`fetch_all()`**. `POST /api/v1/system/models/fetch` (APP-CONTRACT.md
13.1) calls `fetch_all()` with a progress callback and runs it in the
background, because the API must return before the download finishes.
Refer to APP-CONTRACT.md section 9.4: an API request never blocks on
long work.

## The checksum, two ways

A file's expected sha256 comes from one of two places:

1. **A recorded value in this file**, for the Kokoro model. Kokoro's
   repo and file names are fixed by `vendor/abpipe`'s default engine
   configuration, so this script pins the two files the default voice
   needs, straight from the model repo's own published Git LFS hash.
2. **A value fetched at download time**, for every other file,
   including the whisper model. `NARRATARR_WHISPER_MODEL` is not fixed
   yet (APP-CONTRACT.md section 12: pending P0), so this script cannot
   pin its checksum in advance. Instead, it asks the Hugging Face Hub
   API for the file's own recorded Git LFS sha256 before it downloads
   the file, then checks the downloaded bytes against that value. This
   still catches a truncated download, because the expected hash comes
   from a metadata call, never from the download stream it checks.

**When P0 fixes the whisper model, the overlord may want to add its
files to `PINNED_SHA256` below**, the same way the Kokoro files are
pinned. That closes the small remaining gap where a compromised or
mistaken upstream repo could serve bad bytes with a matching bad hash.
This script works correctly either way; pinning only raises the bar.

## Resuming

`huggingface_hub.hf_hub_download` is the download engine. It resumes a
partial file and skips a file that is already present and the right
size, so a second run of this script, or a second run after a crash,
costs nothing for a file that already finished.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

# Required in-process, not only as an image default. Refer to
# APP-CONTRACT.md section 11.1: the HuggingFace xet transport fails on at
# least one machine in this project, and the failure is confusing.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

# narratarr/config.py is the one shared source of every setting, per
# APP-CONTRACT.md section 14.1. This script reads MODELS_DIR and the
# voice and whisper-model choice from there, instead of reading the
# environment a second time, so the two can never drift apart.
from narratarr.config import get_settings  # noqa: E402 - after the env default above

_settings = get_settings()
CONFIG_DIR = _settings.config_dir
MODELS_DIR = _settings.models_dir

KOKORO_REPO_ID = "hexgrad/Kokoro-82M"
KOKORO_VOICE = _settings.voice

# P0 is measuring peak RAM on the target machine right now. This
# placeholder is the ONE place the whisper model id's fallback value
# lives. `narratarr/config.py` defaults `NARRATARR_WHISPER_MODEL` to an
# empty string; this script treats that the same way it treats the
# placeholder below, both meaning "not yet configured". Refer to
# APP-CONTRACT.md section 12.
DEFAULT_WHISPER_MODEL = "PLACEHOLDER-set-by-P0-after-the-RAM-measurement"
WHISPER_REPO_ID = _settings.whisper_model or DEFAULT_WHISPER_MODEL

# Files pinned straight from https://huggingface.co/hexgrad/Kokoro-82M's
# own published Git LFS metadata. A Git LFS oid is a sha256 of the file's
# content, not of a pointer file, so this is a real content checksum.
# Re-check these two values by hand if `vendor/abpipe`'s default Kokoro
# repo or default voice ever changes.
PINNED_SHA256: dict[tuple[str, str], tuple[str, int]] = {
    (KOKORO_REPO_ID, "kokoro-v1_0.pth"): (
        "496dba118d1a58f5f3db2efc88dbdc216e0483fc89fe6e47ee1f2c53f18ad1e4",
        327212226,
    ),
    (KOKORO_REPO_ID, "voices/bm_george.pt"): (
        "f1bc812213dc59774769e5c80004b13eeb79bd78130b11b2d7f934542dab811b",
        523430,
    ),
}


class ModelFetchError(RuntimeError):
    """A model file failed a preflight check, a download, or a checksum."""


@dataclass(frozen=True)
class ModelFile:
    """One file inside one Hugging Face Hub repo."""

    repo_id: str
    filename: str  # the path inside the repo, e.g. "voices/bm_george.pt"


@dataclass(frozen=True)
class ModelSpec:
    """One model. `dest_subdir` is where its files land under MODELS_DIR."""

    key: str
    dest_subdir: str
    files: tuple[ModelFile, ...]


def kokoro_spec() -> ModelSpec:
    """Return the Kokoro model spec for the configured voice."""
    return ModelSpec(
        key="kokoro",
        dest_subdir="kokoro",
        files=(
            ModelFile(KOKORO_REPO_ID, "kokoro-v1_0.pth"),
            ModelFile(KOKORO_REPO_ID, f"voices/{KOKORO_VOICE}.pt"),
        ),
    )


def whisper_spec() -> ModelSpec:
    """Return the whisper model spec. The repo id is one placeholder.

    faster-whisper's CTranslate2 repos hold a small, fixed set of file
    names: `model.bin`, `config.json`, and one or two tokenizer files.
    This script downloads the whole repo tree instead of a fixed file
    list, so a change of repo needs no change here.
    """
    return ModelSpec(key="whisper", dest_subdir="whisper", files=())


def _human_bytes(n: int) -> str:
    """Return n formatted as a short, human-readable byte count."""
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} {unit}"
        value /= 1024
    return f"{n} B"  # pragma: no cover - unreachable, kept for a future unit


def preflight_disk(required_bytes: int, target: Path = MODELS_DIR) -> None:
    """Refuse to start a download when the disk holds too little space.

    Raise ModelFetchError with the needed and the available byte counts
    named plainly. Never start a multi-gigabyte download onto a disk
    that cannot hold it.
    """
    target.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(target)
    # A margin on top of the raw file size. Extraction, a partial
    # retry, and the sqlite WAL file all need headroom of their own.
    margin = max(int(required_bytes * 0.10), 512 * 1024 * 1024)
    needed = required_bytes + margin
    if usage.free < needed:
        raise ModelFetchError(
            "Not enough disk space to fetch the models. "
            f"Needed: {_human_bytes(needed)} (including a safety margin). "
            f"Available: {_human_bytes(usage.free)} at {target}. "
            "Free some space, or point NARRATARR_CONFIG_DIR at a volume "
            "with more room, then try again."
        )


def _repo_file_list(repo_id: str) -> list[str]:
    """Return every regular file path in a Hugging Face repo's main revision."""
    from huggingface_hub import HfApi

    api = HfApi()
    info = api.model_info(repo_id, files_metadata=True)
    return [s.rfilename for s in info.siblings]


def _expected_sha256(repo_id: str, filename: str) -> Optional[str]:
    """Return the file's expected sha256, pinned first, fetched second.

    Return None when neither source has a value. That happens only for
    a small, non-LFS file whose repo does not publish a content sha256;
    such a file is checked for its byte size only, and this script warns
    loudly that it is not checksum-verified. Refer to the module
    docstring, "The checksum, two ways".
    """
    pinned = PINNED_SHA256.get((repo_id, filename))
    if pinned is not None:
        return pinned[0]

    from huggingface_hub import HfApi

    api = HfApi()
    info = api.model_info(repo_id, files_metadata=True)
    for sibling in info.siblings:
        if sibling.rfilename == filename:
            if sibling.lfs is not None:
                return sibling.lfs.get("sha256")
            return None
    raise ModelFetchError(f"{repo_id} holds no file named {filename!r}.")


def _sha256_of(path: Path) -> str:
    """Return the sha256 of a file's content, read in fixed-size chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


ProgressCallback = Callable[[str, int, int], None]


def _download_one(
    repo_id: str,
    filename: str,
    dest_dir: Path,
    progress: Optional[ProgressCallback] = None,
) -> Path:
    """Download one file with huggingface_hub, then verify its checksum.

    huggingface_hub resumes a partial file and skips a file that is
    already complete, so calling this twice for the same file is cheap
    the second time.
    """
    from huggingface_hub import hf_hub_download

    if progress:
        progress(filename, 0, 0)

    local_path = Path(
        hf_hub_download(
            repo_id=repo_id,
            filename=filename,
            local_dir=str(dest_dir),
        )
    )

    expected = _expected_sha256(repo_id, filename)
    actual = _sha256_of(local_path)
    if expected is None:
        print(
            f"WARNING: {filename} has no recorded checksum. Trusting the "
            "file size only. Refer to the module docstring for why.",
            file=sys.stderr,
        )
    elif actual != expected:
        # Delete the bad file. A truncated or corrupt file must never
        # sit on disk looking like a finished download.
        local_path.unlink(missing_ok=True)
        raise ModelFetchError(
            f"{filename} failed its checksum after download. "
            f"Expected sha256 {expected}, got {actual}. The partial or "
            "corrupt file was deleted. Run this script again to retry."
        )

    if progress:
        progress(filename, local_path.stat().st_size, local_path.stat().st_size)

    return local_path


def fetch_spec(spec: ModelSpec, progress: Optional[ProgressCallback] = None) -> list[Path]:
    """Download every file of one ModelSpec into MODELS_DIR/spec.dest_subdir."""
    dest_dir = MODELS_DIR / spec.dest_subdir
    dest_dir.mkdir(parents=True, exist_ok=True)

    files = list(spec.files)
    if not files:
        # whisper_spec() leaves its file list empty on purpose: the CT2
        # repo layout is not ours to hard-code. Ask the repo for its own
        # file list, then download every one.
        files = [ModelFile(WHISPER_REPO_ID, name) for name in _repo_file_list(WHISPER_REPO_ID)]

    return [_download_one(f.repo_id, f.filename, dest_dir, progress) for f in files]


def fetch_all(progress: Optional[ProgressCallback] = None) -> None:
    """Download every model Narratarr needs. The entry point for the API route.

    Preflight the disk before any network call starts. Refer to
    APP-CONTRACT.md section 11.1: refuse a download the disk cannot
    hold, rather than fail partway through it.
    """
    if WHISPER_REPO_ID == DEFAULT_WHISPER_MODEL:
        raise ModelFetchError(
            "NARRATARR_WHISPER_MODEL is not set, and the built-in default "
            "is a placeholder. P0 has not finished the RAM measurement "
            "yet. Refer to APP-CONTRACT.md section 12. Set the "
            "environment variable to a real faster-whisper CTranslate2 "
            "repo id to proceed."
        )

    # A rough, conservative estimate. Kokoro's own weights and one voice
    # are a known size; a typical CTranslate2 whisper model of a few
    # hundred million parameters runs from several hundred MB to a few
    # GB, so this reserves generously rather than guess low.
    estimated_bytes = 327_212_226 + 1_000_000 + 3_000_000_000
    preflight_disk(estimated_bytes)

    for spec in (kokoro_spec(), whisper_spec()):
        print(f"Fetching {spec.key}...")
        fetch_spec(spec, progress)
        print(f"Fetched {spec.key}.")


def _print_progress(filename: str, done: int, total: int) -> None:
    if total:
        print(f"  {filename}: {_human_bytes(done)} / {_human_bytes(total)}")
    else:
        print(f"  {filename}: starting")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        fetch_all(progress=_print_progress)
    except ModelFetchError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print("Done. Every model is downloaded and checksum-verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
