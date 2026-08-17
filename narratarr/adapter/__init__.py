"""The pipeline adapter. APP-CONTRACT section 6. Owner: W2.

`Pipeline` is the only class in Narratarr that imports `abpipe`. Every
import of `abpipe` in this module is lazy, inside a function or method body
— never at module level — so importing `narratarr.adapter` never pulls
torch, numpy, or any other pipeline dependency. `vendor/abpipe/abpipe/qc.py`
and `vendor/abpipe/abpipe/engines/kokoro_mlx.py` both use this same pattern;
this module copies it.

**The adapter converts; it does not decide.** Every policy — a threshold, an
ordering rule, a gate condition — lives in `abpipe` or in the runner
(`narratarr/runner.py`, owned by W1). This module turns an `abpipe` summary
dict into a `StageResult`, and nothing more.

**`run_stage()` runs stages 1 to 7 only.** This module never calls
`abpipe.deliver`, and never reads `absdatabase.sqlite`. APP-CONTRACT
section 3.1: `abpipe/deliver.py` is the upstream author's own delivery stage, with a
server address hard-coded, and it must never run in a product a stranger
installs. Stage 8 is `narratarr/adapter/targets/`, a separate package.

**Markup ordering, confirmed by reading `vendor/abpipe/abpipe/render.py`:**
`render.run()` applies the homograph markup first
(`homographs.apply_homographs`), then the pronunciation map
(`render.apply_pronunciations`) — pipeline CONTRACT.md 18.5, because the
markup's character offsets are indexed against the on-disk text, and a
pronunciation substitution running first would shift those offsets or match
inside an already-bracketed word. `render.run()` itself already applies
both, exactly once, in that order, for the real per-book render — this
module never re-applies them there. The only place this module applies that
same pair of functions again is `render_sample()` and
`homograph_candidates()` below, which render SCRATCH audio outside the
per-book `04-audio/` tree for a person to preview — never the production
WAV path — and each applies the pair exactly once, in the same fixed order,
never twice.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- the interface (APP-CONTRACT 6)


@dataclass(frozen=True)
class StageResult:
    stage: str
    done: int
    skipped: int
    failed: int
    aborted: bool = False
    abort_reason: str | None = None
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Progress:
    stage: str
    done: int
    total: int
    message: str = ""


class PipelineError(Exception):
    """A pipeline stage, or an adapter operation, cannot be trusted to have
    produced a real result.

    Raised instead of letting an `abpipe` exception cross the one seam this
    application has with the pipeline (APP-CONTRACT section 3: "The adapter
    is the only place that imports `abpipe`"). House rule 4: "A stage that
    produces nothing and reports success is worse than a crash" — this
    module never turns a real `abpipe` fault into a `StageResult` that
    looks like an ordinary, if imperfect, outcome. A *controlled* abort
    (`abpipe`'s own `aborted`/`abort_reason` fields — pipeline CONTRACT.md
    8.2: a full disk, or too many consecutive chunk failures) is different:
    `abpipe` itself already reports that without raising, so it is
    reported here the same way, as a `StageResult` with `aborted=True` —
    not raised. `PipelineError` is for the case `abpipe` itself treats as
    an exception: `KeyError` (an unknown chapter id), `RuntimeError` /
    `TimeoutError` (a stage that refuses to run at all — assemble on a red
    QC report, bind on missing inputs, a delivery poll timeout), or a bad
    argument this module's own methods reject before ever calling into
    `abpipe`.
    """


# --------------------------------------------------------------------------- small, dependency-free helpers


def _count(value: Any) -> int:
    """Return an integer count for a done/skipped/failed field.

    `abpipe`'s stage modules do not agree on the shape of these fields:
    `chunk.py` and `render.py` return a plain int; `extract.py` and
    `normalize.py` return a list of chapter ids; `assemble.py`/`bind.py`
    return a list of chapter ids or dicts. `len()` reads a collection;
    `int()` reads a number; this function reads both through one path.
    Mirrors `vendor/abpipe/abpipe/cli.py`'s own `_fmt_count`, for the same
    reason that module states it: one coercion rule, used everywhere a
    summary's count fields are read.
    """
    if isinstance(value, (list, tuple, dict, set)):
        return len(value)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _to_stage_result(stage: str, summary: dict) -> StageResult:
    return StageResult(
        stage=stage,
        done=_count(summary.get("done")),
        skipped=_count(summary.get("skipped")),
        failed=_count(summary.get("failed")),
        aborted=bool(summary.get("aborted", False)),
        abort_reason=summary.get("abort_reason"),
        detail=summary,
    )


def _write_wav_atomic(out_path: Path, audio, sample_rate: int) -> None:
    """Write one WAV file atomically: a temporary file beside `out_path`,
    then `os.replace`. A failed write removes its own temporary file.
    APP-CONTRACT house rule 15.5. Mirrors
    `vendor/abpipe/abpipe/render.py`'s `_write_wav_atomic` — copied, not
    imported, because that name is private to that module.
    """
    import soundfile as sf

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.parent / (out_path.name + ".narratarr.tmp")
    try:
        sf.write(str(tmp_path), audio, sample_rate, subtype="PCM_16", format="WAV")
        os.replace(tmp_path, out_path)
    except BaseException:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def _write_wav_concat(out_path: Path, segments: list, sample_rate: int, gap_s: float = 0.35) -> Path:
    """Join `segments` (mono float32 arrays, same sample rate) with a short
    silence gap between each, and write the result atomically to
    `out_path`. Used by `render_sample()` and `homograph_candidates()`
    below — never by a production render, which stays inside
    `abpipe.render` and `abpipe.assemble`'s own, contract-defined silence
    table (pipeline CONTRACT.md 10.1). This gap exists only to make a
    multi-chunk preview listenable; it is not a claim about the finished
    book's own pacing.
    """
    import numpy as np

    gap = np.zeros(int(gap_s * sample_rate), dtype=np.float32)
    parts = []
    for i, seg in enumerate(segments):
        if i:
            parts.append(gap)
        parts.append(np.asarray(seg, dtype=np.float32).reshape(-1))
    audio = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
    _write_wav_atomic(out_path, audio, sample_rate)
    return out_path


# --------------------------------------------------------------------------- live progress: a text-parsing coupling
#
# Pipeline CONTRACT.md section 13's stage entry point,
# `def run(ctx, chapters=None, force=False, **kw) -> dict`, carries no
# progress-callback parameter — the ONLY progress mechanism `render.run()`
# and `qc.run()` offer is a `print()` line per chunk (or every
# `PROGRESS_EVERY` chunks for qc). No other stage module prints a per-item
# line at all. This is a real gap between what APP-CONTRACT 6 promises
# (`progress: Callable[[Progress], None] | None`) and what `abpipe` can
# deliver without a change upstream — flagged in full in this worker's
# report. What follows is the best this seam can do without one: capture
# stdout during a `render`/`qc` call, parse the two stages' own documented
# print formats, and forward each parsed line as a Progress event, while
# still passing every line through to the real stdout unchanged (so
# container logs keep showing them).
#
# This is a coupling to abpipe's print TEXT, not to a real interface. A
# future change to either print format silently stops live progress for
# that stage — run_stage() still returns a correct StageResult regardless,
# since the parse failure only means `progress()` fires less often, never
# that the underlying call is affected.

_RENDER_PROGRESS_RE = re.compile(r"^\[render\] (?P<chapter>\S+) chunk (?P<done>\d+)/(?P<total>\d+)")
_QC_PROGRESS_RE = re.compile(r"^\[qc\] (?P<chapter>\S+) (?P<chunk>\S+) \((?P<done>\d+)/(?P<total>\d+)\)")
_PROGRESS_PATTERNS = {"render": _RENDER_PROGRESS_RE, "qc": _QC_PROGRESS_RE}


class _ProgressTee(io.TextIOBase):
    """Forward every `write()` to the real stream, and parse a matching line
    into a `Progress` event for `callback`. See the module comment above."""

    def __init__(self, real, stage: str, callback: Callable[[Progress], None]) -> None:
        self._real = real
        self._pattern = _PROGRESS_PATTERNS.get(stage)
        self._stage = stage
        self._callback = callback

    def write(self, s: str) -> int:
        self._real.write(s)
        if self._pattern is not None:
            for line in s.splitlines():
                m = self._pattern.match(line)
                if m:
                    self._callback(
                        Progress(
                            stage=self._stage,
                            done=int(m.group("done")),
                            total=int(m.group("total")),
                            message=line.strip(),
                        )
                    )
        return len(s)

    def flush(self) -> None:
        self._real.flush()


# --------------------------------------------------------------------------- hazard selection for render_sample() (T-2)
#
# Pipeline CONTRACT.md section 17 (triage) treats T-2's hazard-passage
# choice as work a PERSON does by hand, for the handful of books an operator
# personally ships: "Triage is overlord work, not worker work. Triage
# writes no code." `abpipe` therefore defines no automatic selector at
# all. Narratarr promises the sample gate to a stranger who is not going to
# hand-pick a passage for every book they drop in (APP-CONTRACT 9.1), so
# this heuristic is new code this worker wrote for that purpose — not a
# duplicate of any `abpipe` policy, because none exists to duplicate.
#
# The score rewards exactly the four hazard kinds APP-CONTRACT 9.1 and
# pipeline CONTRACT.md T-2 both name: "the worst proper noun, a foreign
# term, a number, and a caps run."

_CAPS_RUN_RE = re.compile(r"\b[A-Z]{2,}(?:\s+[A-Z]{2,}){1,}\b")
_NUMBER_RE = re.compile(r"\d+")
_NON_ASCII_RUN_RE = re.compile(r"[^\x00-\x7f]+")
# A capitalised word, not at the very start of the chunk, that is not a
# common closed-class word this dialect-agnostic heuristic would otherwise
# mistake for a proper noun (a sentence-initial "The", a mid-sentence "I").
# Deliberately short and generic — this is a hazard SCORE, not a grammar
# check, and a false positive only nudges chunk selection, never blocks
# anything a person cannot simply re-render past (render_sample() takes an
# explicit `chapter` override for exactly that reason).
_COMMON_CAPITALISED = frozenset(
    {"i", "the", "a", "an", "he", "she", "they", "we", "you", "it", "and", "but", "or", "if", "when", "as"}
)
_WORD_RE = re.compile(r"[A-Za-z']+")

_CHARS_PER_SECOND_ESTIMATE = 14.0  # rough English narration rate; pipeline CONTRACT.md
# section 12 ("the measured performance") holds no measured characters-per-
# second number yet ("P0 fills this section... until then it holds no
# number") — this constant is a placeholder for picking a WINDOW SIZE only,
# not a claim about render speed, and never appears in anything user-facing.


def _hazard_score(text: str) -> int:
    """Return a hazard score for one chunk's on-disk text. Higher means more
    render-hazard-dense — see the module comment above for what counts."""
    score = 0
    score += 5 * len(_CAPS_RUN_RE.findall(text))
    score += 2 * len(_NUMBER_RE.findall(text))
    score += 3 * len(_NON_ASCII_RUN_RE.findall(text))

    words = _WORD_RE.findall(text)
    for i, word in enumerate(words):
        if i == 0:
            continue  # sentence/chunk-initial capital tells us nothing
        if word[0].isupper() and not word.isupper() and word.lower() not in _COMMON_CAPITALISED:
            score += 2
    return score


# --------------------------------------------------------------------------- preflight_engine
#
# Added at the overlord's request, to close an integration gap with W1's
# runner: `narratarr/runner.py` calls this once before a book's first
# render, and refuses to render at all when it raises. Pipeline CONTRACT.md
# 17.2 (added after the overlord's re-vendor of `abpipe` at the P0 commit):
# the torch `kokoro` package's own espeak-fallback warning is emitted
# through loguru, and `kokoro/__init__.py` calls `logger.disable("kokoro")`
# at import time -- so the documented stdlib-`logging` grep this project
# otherwise relies on (pipeline CONTRACT.md 17.1) NEVER FIRES for this
# engine. `KokoroCPUEngine.preflight()` (`vendor/abpipe/abpipe/engines/
# kokoro_cpu.py`) is the one check that cannot be silenced: it reads
# `pipeline.g2p.fallback` off the constructed object directly, and renders
# a real out-of-lexicon probe to prove the fallback actually runs, not just
# that it exists.


def preflight_engine(engine: str, voice: str, lang_code: str) -> dict:
    """Load `engine`, check its espeak fallback, render a warmup probe, and
    return its report.

    Builds a bare engine config from the three arguments (`{"name": engine,
    "voice": voice, "lang_code": lang_code}` -- `model`, `speed`, and
    `sample_rate` are left to that engine class's own defaults), constructs
    the engine through `abpipe.engines.get_engine`, and returns
    `engine.preflight()`'s own report dict, unchanged -- "the adapter
    converts; it does not decide" applies here too: this function is a
    thin, honest pass-through, never a second copy of the check itself.

    Only `KokoroCPUEngine` (`vendor/abpipe/abpipe/engines/kokoro_cpu.py`)
    currently implements `preflight()` — `KokoroMLXEngine` and
    `ChatterboxEngine` do not. Raises `PipelineError`, not `AttributeError`,
    when the named engine exposes no such method, so every fault this
    function can produce shares the one exception type W1's runner already
    catches and refuses to render on.

    Raises `PipelineError` when the engine cannot be constructed at all (a
    missing optional dependency, a bad config), and when `preflight()`
    itself raises (pipeline CONTRACT.md 17.2: no espeak fallback, and the
    engine config does not set `allow_no_espeak_fallback: true`) — in every
    case, the caller must refuse to render, per the module docstring's
    house rule 4: "A stage that produces nothing and reports success is
    worse than a crash."
    """
    config = {"name": engine, "voice": voice, "lang_code": lang_code}
    try:
        from abpipe.engines import get_engine

        instance = get_engine(config)
        preflight = getattr(instance, "preflight", None)
        if preflight is None:
            raise PipelineError(
                f"preflight_engine: engine {engine!r} exposes no preflight() check "
                "(only kokoro_cpu does today, per vendor/abpipe/abpipe/engines/"
                "kokoro_cpu.py). Refusing to render with no way to confirm the "
                "espeak fallback is real."
            )
        return preflight()
    except PipelineError:
        raise
    except Exception as exc:
        raise PipelineError(f"preflight_engine: {engine!r} preflight failed: {exc}") from exc


# --------------------------------------------------------------------------- Pipeline


_PIPELINE_STAGES = ("extract", "normalize", "chunk", "render", "qc", "assemble", "bind")


class Pipeline:
    """One book. One work directory. The adapter over `abpipe`. APP-CONTRACT
    section 6.

    `workspace` is `narratarr.config.Settings.work_dir` — `<NARRATARR_
    CONFIG_DIR>/work`, the directory that holds one subdirectory per book
    (APP-CONTRACT section 2.1: `/config/work/<slug>/`). **Confirmed against
    `narratarr/runner.py`'s real callers, not assumed**: both
    `_default_pipeline_factory` and `_pipeline_for_job` construct a
    `Pipeline` with `settings.work_dir` as the first argument.

    `abpipe.context.Context` — a pipeline kernel file this application
    never edits (pipeline CONTRACT.md 16) — hardcodes `book_dir` as
    `root / "work" / slug`, so `Context.root` cannot be `workspace` itself
    (that would land on `<config_dir>/work/work/<slug>`, one `work` too
    many — a real bug an earlier version of this class had, caught by
    `tests/test_targets.py`'s `deliver_job` tests once they seeded a real
    book directory at `settings.work_dir / slug` and a real `Pipeline` had
    to find it there). `Context.root` is therefore `workspace.parent`,
    which is `NARRATARR_CONFIG_DIR` precisely because `Settings.work_dir`
    is always `config_dir / "work"` — so `Context.root / "work" / slug`
    reconstructs `workspace / slug`, exactly APP-CONTRACT 2.1's path, with
    no assumption beyond the one `Settings.work_dir` itself already
    guarantees.

    `book_config` is the `abpipe` book config dict (pipeline CONTRACT.md
    4.1) — the same shape `source/<slug>.config.json` holds upstream, but
    here it is passed straight from `jobs.book_config` on every call,
    never written to a `source/*.config.json` file (Narratarr holds no
    `source/` directory; a book's config lives in the database row, per
    APP-CONTRACT section 4.2).
    """

    def __init__(self, workspace: Path, slug: str, source: Path, book_config: dict, qc_config: dict) -> None:
        self.workspace = Path(workspace)
        self.slug = slug
        self.source = Path(source)
        self.book_config = dict(book_config or {})
        self.qc_config = dict(qc_config or {})
        self._ctx = self._make_context()
        self._engine = None  # lazily constructed; see _get_engine()

    # ------------------------------------------------------------------ construction

    def _make_context(self):
        from abpipe.context import Context

        # See the class docstring: workspace is the "work" directory
        # itself (settings.work_dir); Context always appends its own
        # "work" segment to whatever root it is given, so root must be
        # workspace's PARENT for the two to land on the same book_dir.
        return Context(root=self.workspace.parent, slug=self.slug, epub=self.source)

    def _get_engine(self):
        """Return the cached engine, constructing it on first use.
        `abpipe.engines.get_engine` reads `book.json`'s `engine` block, so
        this only works once stage 1 (extract) has run."""
        if self._engine is None:
            from abpipe.engines import get_engine

            self._engine = get_engine(self._ctx.engine_config)
        return self._engine

    # ------------------------------------------------------------------ run_stage

    def run_stage(
        self,
        stage: str,
        chapters: list[str] | None = None,
        force: bool = False,
        progress: Callable[[Progress], None] | None = None,
    ) -> StageResult:
        """Run one pipeline stage. APP-CONTRACT section 6.

        Only `extract`, `normalize`, `chunk`, `render`, `qc`, `assemble`,
        and `bind` are accepted — stages 1 through 7. `"deliver"` is
        refused outright (APP-CONTRACT 3.1: Narratarr never calls
        `abpipe.deliver`; use `narratarr.adapter.targets` instead).
        `"sample"` and `"homographs"` are refused too — those are
        `render_sample()` and `homograph_audit()`, not `abpipe` stage
        modules (APP-CONTRACT section 5.1 names them Narratarr's own
        steps, mapped onto pipeline triage steps T-2 and T-2.5).
        """
        if stage not in _PIPELINE_STAGES:
            raise PipelineError(
                f"run_stage: {stage!r} is not a stage this adapter runs. "
                f"Valid stages are {_PIPELINE_STAGES!r}. 'deliver' is Narratarr's own "
                "target layer (APP-CONTRACT 3.1); 'sample' and 'homographs' are "
                "render_sample() and homograph_audit()."
            )

        try:
            summary = self._run_stage_module(stage, chapters, force, progress)
        except (KeyError, RuntimeError, TimeoutError) as exc:
            raise PipelineError(f"run_stage {stage}: {exc}") from exc

        if stage == "extract":
            # extract may have (re)written book.json; every later stage,
            # and this adapter's own ctx.book reads (pronunciations, the
            # engine config), must see the fresh copy.
            self._ctx.load_book()

        return _to_stage_result(stage, summary)

    def _run_stage_module(
        self,
        stage: str,
        chapters: list[str] | None,
        force: bool,
        progress: Callable[[Progress], None] | None,
    ) -> dict:
        ctx = self._ctx

        if progress is not None:
            progress(Progress(stage=stage, done=0, total=0, message=f"starting {stage}"))

        cm = contextlib.redirect_stdout(_ProgressTee(sys.stdout, stage, progress)) if progress is not None else contextlib.nullcontext()
        with cm:
            if stage == "extract":
                from abpipe import extract

                summary = extract.run(ctx, force=force, book_config=self.book_config)
            elif stage == "normalize":
                from abpipe import normalize

                summary = normalize.run(ctx, chapters=chapters, force=force, book_config=self.book_config)
            elif stage == "chunk":
                from abpipe import chunk

                summary = chunk.run(ctx, chapters=chapters, force=force, book_config=self.book_config)
            elif stage == "render":
                from abpipe import render

                summary = render.run(ctx, chapters=chapters, force=force, engine=self._get_engine())
            elif stage == "qc":
                from abpipe import qc

                summary = qc.run(
                    ctx, chapters=chapters, force=force, engine=self._get_engine(), book_config=self.book_config
                )
            elif stage == "assemble":
                from abpipe import assemble

                summary = assemble.run(ctx, chapters=chapters, force=force)
            elif stage == "bind":
                from abpipe import bind

                summary = bind.run(ctx, force=force)
            else:  # pragma: no cover - _PIPELINE_STAGES already guards this
                raise AssertionError(f"unreachable stage: {stage!r}")

        if progress is not None:
            done = _count(summary.get("done"))
            total = done + _count(summary.get("skipped")) + _count(summary.get("failed"))
            progress(Progress(stage=stage, done=done, total=total, message=f"finished {stage}"))

        return summary

    # ------------------------------------------------------------------ status

    def status(self) -> dict:
        """Return the fresh/stale/absent count of each stage. APP-CONTRACT
        section 6.

        `extract`, `normalize`, `chunk`, and `render` use each stage's own
        public `config_hash`/`input_hash` functions (`extract.
        extract_config_hash`, `normalize.rules_config_hash`, `render.
        render_config_hash` + `render.render_input_hash`), the same
        formulas the stages themselves use to decide fresh vs. stale.
        `chunk._config_hash()` is technically private (a leading
        underscore), but `vendor/abpipe/abpipe/cli.py`'s own `_status_chunk`
        already reaches across the same module boundary the same way, so
        this is no new coupling.

        `qc`, `assemble`, and `bind` have no such public formula to ask
        for — `vendor/abpipe/abpipe/cli.py`'s own `_qc_config_hash` says so
        directly ("qc.py exposes no public 'give me your config_hash'
        helper... Flagged to the overlord as a case for exposing a public
        qc_config_hash()"). This method approximates those three stages the
        same way `cli.py`'s own `_status_qc`/`_status_assemble` already do:
        "output present with a meta that parses" stands in for exact
        freshness. This worker's report flags the gap; it is upstream, in
        `abpipe`, not something this adapter can close without
        reimplementing a formula `abpipe` itself calls fragile.
        """
        from abpipe import extract as extract_mod
        from abpipe import homographs
        from abpipe import normalize as normalize_mod
        from abpipe import render as render_mod
        from abpipe.meta import hash_file, is_fresh, read_json, read_meta

        ctx = self._ctx
        empty = {"fresh": 0, "stale": 0, "absent": 0, "total": 0}
        try:
            chapter_ids = ctx.chapter_ids()
        except Exception:
            chapter_ids = []
        if not chapter_ids:
            return {name: dict(empty) for name in ("extract", "normalize", "chunk", "render", "qc", "assemble", "bind")}

        result: dict[str, dict] = {}

        # extract
        try:
            source_hash: str | None = hash_file(ctx.epub)
        except OSError:
            source_hash = None
        extract_conf = extract_mod.extract_config_hash(self.book_config)
        extract_dir = ctx.stage_dir("extract", make=False)
        fresh = stale = absent = 0
        for cid in chapter_ids:
            out_path = extract_dir / f"{cid}.txt"
            if not out_path.exists():
                absent += 1
            elif source_hash is not None and is_fresh(out_path, source_hash, extract_conf):
                fresh += 1
            else:
                stale += 1
        result["extract"] = {"fresh": fresh, "stale": stale, "absent": absent, "total": len(chapter_ids)}

        # normalize
        normalize_config = self.book_config.get("normalize") or normalize_mod.DEFAULT_NORMALIZE_CONFIG
        normalize_conf = normalize_mod.rules_config_hash(normalize_config)
        norm_dir = ctx.stage_dir("normalize", make=False)
        fresh = stale = absent = 0
        for cid in chapter_ids:
            out_path = norm_dir / f"{cid}.txt"
            in_path = extract_dir / f"{cid}.txt"
            if not out_path.exists():
                absent += 1
            elif not in_path.exists():
                stale += 1
            elif is_fresh(out_path, hash_file(in_path), normalize_conf):
                fresh += 1
            else:
                stale += 1
        result["normalize"] = {"fresh": fresh, "stale": stale, "absent": absent, "total": len(chapter_ids)}

        # chunk
        chunk_dir = ctx.stage_dir("chunk", make=False)
        chunk_conf = self._chunk_config_hash()
        fresh = stale = absent = 0
        for cid in chapter_ids:
            index_path = chunk_dir / cid / "index.json"
            in_path = norm_dir / f"{cid}.txt"
            if not index_path.exists():
                absent += 1
            elif not in_path.exists():
                stale += 1
            elif chunk_conf is not None and is_fresh(index_path, hash_file(in_path), chunk_conf):
                fresh += 1
            else:
                stale += 1
        result["chunk"] = {"fresh": fresh, "stale": stale, "absent": absent, "total": len(chapter_ids)}

        # render (per chunk, not per chapter)
        render_dir = ctx.stage_dir("render", make=False)
        fresh = stale = absent = total = 0
        try:
            engine = self._get_engine()
            pronunciations = dict(ctx.book.get("pronunciations") or {})
            render_conf = render_mod.render_config_hash(engine.describe(), pronunciations)
            decisions_doc = homographs.read_decisions(ctx.book_dir)
            for cid in chapter_ids:
                index = read_json(chunk_dir / cid / "index.json")
                if not isinstance(index, dict):
                    continue
                for rec in index.get("chunks", []):
                    total += 1
                    out_path = render_dir / cid / f"{rec['id']}.wav"
                    if not out_path.exists():
                        absent += 1
                    elif is_fresh(out_path, render_mod.render_input_hash(rec, decisions_doc, cid), render_conf):
                        fresh += 1
                    else:
                        stale += 1
        except Exception:
            fresh = stale = absent = total = 0
        result["render"] = {"fresh": fresh, "stale": stale, "absent": absent, "total": total}

        # qc: approximate. See this method's docstring.
        qc_dir = ctx.stage_dir("qc", make=False)
        fresh = stale = absent = total = 0
        for cid in chapter_ids:
            index = read_json(chunk_dir / cid / "index.json")
            if not isinstance(index, dict):
                continue
            for rec in index.get("chunks", []):
                total += 1
                out_path = qc_dir / cid / f"{rec['id']}.json"
                if not out_path.exists():
                    absent += 1
                    continue
                meta = read_meta(out_path)
                if meta and meta.get("schema") == 1:
                    fresh += 1
                else:
                    stale += 1
        result["qc"] = {"fresh": fresh, "stale": stale, "absent": absent, "total": total}

        # assemble: approximate. A pruned chapter's m4a is the durable
        # artifact (pipeline CONTRACT.md 15) and counts as fresh.
        assemble_dir = ctx.stage_dir("assemble", make=False)
        fresh = stale = absent = 0
        for cid in chapter_ids:
            if (assemble_dir / f"{cid}.pruned.json").exists():
                fresh += 1
                continue
            m4a_path = assemble_dir / f"{cid}.m4a"
            if not m4a_path.exists():
                absent += 1
                continue
            meta = read_meta(m4a_path)
            if meta and meta.get("schema") == 1:
                fresh += 1
            else:
                stale += 1
        result["assemble"] = {"fresh": fresh, "stale": stale, "absent": absent, "total": len(chapter_ids)}

        # bind: one output for the whole book.
        m4b_path = ctx.stage_dir("bind", make=False) / f"{ctx.title}.m4b"
        if not m4b_path.exists():
            result["bind"] = {"fresh": 0, "stale": 0, "absent": 1, "total": 1}
        else:
            meta = read_meta(m4b_path)
            if meta and meta.get("schema") == 1:
                result["bind"] = {"fresh": 1, "stale": 0, "absent": 0, "total": 1}
            else:
                result["bind"] = {"fresh": 0, "stale": 1, "absent": 0, "total": 1}

        return result

    def _chunk_config_hash(self) -> str | None:
        from abpipe import chunk as chunk_mod

        try:
            return chunk_mod._config_hash()
        except Exception:
            return None

    # ------------------------------------------------------------------ render_sample (triage T-2)

    def render_sample(self, chapter: str | None = None, seconds: float = 90.0) -> Path:
        """Render a hazard passage. Return the WAV path. Triage step T-2.
        APP-CONTRACT section 9.1 / section 6.

        **How the passage is picked**, since none of this exists in
        `abpipe` (see the module comment above `_hazard_score`): every
        non-heading chunk of the selected chapter(s) — every chapter, when
        `chapter` is omitted — is scored by `_hazard_score()`, which
        rewards the four hazard kinds APP-CONTRACT 9.1 names by name: an
        ALL-CAPS run of two or more words, a run of digits, a run of
        non-ASCII characters (a foreign term's diacritics), and a
        capitalised word that is not chunk-initial and not a common
        closed-class word (a proper-noun stand-in — the closest a
        dialect-agnostic, `abpipe`-free heuristic can get to "the worst
        name" without a full NLP tagger, which pipeline CONTRACT.md 17
        reserves for the *homograph* audit, not for T-2's passage choice).
        The single highest-scoring chunk anchors the sample; `render_sample`
        then extends forward, chunk by chunk within the same chapter, until
        the accumulated character count reaches a rough `seconds`-to-chars
        estimate (`_CHARS_PER_SECOND_ESTIMATE`) — a placeholder, since
        pipeline CONTRACT.md section 12 ("the measured performance") holds
        no measured characters-per-second number yet.

        Every chunk in the window is rendered with the SAME markup order
        `render.run()` itself uses (homograph markup, then the
        pronunciation map — see this module's docstring), so the sample is
        honest about what a real render of that passage would say. The
        segments are joined with a short silence gap and written to
        `<book_dir>/review/sample.wav`, overwriting any earlier sample —
        APP-CONTRACT 9.1's gate examines the CURRENT sample only; an old one
        has no standing once a new one exists.

        Raises PipelineError when the book holds no chapters yet (extract
        has not run), or the chunk stage has not produced any chunk text
        for the selected chapter(s) yet.
        """
        from abpipe import homographs, render
        from abpipe.meta import read_json

        ctx = self._ctx
        try:
            chapter_ids = [chapter] if chapter else ctx.chapter_ids()
        except KeyError as exc:
            raise PipelineError(f"render_sample: {exc}") from exc
        if not chapter_ids:
            raise PipelineError("render_sample: book.json holds no chapters; run extract first")

        chunk_dir = ctx.stage_dir("chunk", make=False)
        best: tuple[int, str, int] | None = None  # (score, chapter_id, index in that chapter's records)
        per_chapter_records: dict[str, list[dict]] = {}

        for cid in chapter_ids:
            index = read_json(chunk_dir / cid / "index.json")
            if not isinstance(index, dict) or not index.get("chunks"):
                continue
            records = index["chunks"]
            per_chapter_records[cid] = records
            for i, rec in enumerate(records):
                if rec.get("is_heading"):
                    continue
                try:
                    text = (chunk_dir / cid / rec["file"]).read_text(encoding="utf-8")
                except OSError:
                    continue
                score = _hazard_score(text)
                if best is None or score > best[0]:
                    best = (score, cid, i)

        if best is None:
            raise PipelineError(
                "render_sample: no chunk text found for the selected chapter(s); "
                "run the chunk stage first"
            )

        _, cid, start_i = best
        records = per_chapter_records[cid]
        target_chars = max(int(seconds * _CHARS_PER_SECOND_ESTIMATE), records[start_i].get("chars", 0))
        window: list[dict] = []
        total_chars = 0
        i = start_i
        while i < len(records) and total_chars < target_chars:
            window.append(records[i])
            total_chars += records[i].get("chars", 0)
            i += 1

        decisions_doc = homographs.read_decisions(ctx.book_dir)
        pronunciations = dict(ctx.book.get("pronunciations") or {})
        engine = self._get_engine()

        segments = []
        sample_rate = None
        for rec in window:
            text = (chunk_dir / cid / rec["file"]).read_text(encoding="utf-8")
            chunk_decisions = homographs.decisions_for_chunk(decisions_doc, cid, rec["id"])
            # Markup first, then pronunciations -- pipeline CONTRACT.md 18.5.
            text = homographs.apply_homographs(text, chunk_decisions)
            text = render.apply_pronunciations(text, pronunciations)
            audio, sr = render.render_chunk(engine, text)
            sample_rate = sr
            segments.append(audio)

        out_path = ctx.book_dir / "review" / "sample.wav"
        return _write_wav_concat(out_path, segments, sample_rate)

    # ------------------------------------------------------------------ homographs (triage T-2.5)

    def homograph_audit(self, write: bool = False, llm: bool = True) -> dict:
        """Run the audit. Return the decisions and the open occurrences.
        APP-CONTRACT section 6 / section 9.2.

        `{"summary": ..., "decisions": [...], "open_occurrences": [...]}`.
        `summary` is `abpipe.homographs.run()`'s own return value —
        pass-through, per "the adapter converts; it does not decide."
        `decisions` is the full, current content of `work/<slug>/
        homographs.json` after this call (empty when `write=False` and no
        prior audit has ever written one). `open_occurrences` is the
        content of `work/<slug>/homograph-review.json` — the tier-3
        escalations `abpipe.homograph_tiers.disambiguate()` could not
        resolve at all.

        **A known gap, flagged in full in this worker's report:**
        `homographs.run()`'s own return dict carries only aggregate counts
        (`unresolved`, `conflicts`, `stale`, ...) — the per-occurrence
        table it prints (`_print_report`'s `rows`) is never returned, so
        this method cannot surface a `conflict` or `stale` occurrence
        (APP-CONTRACT section 9.2's blocking statuses beyond a bare
        `unresolved`) as a structured review item. Reimplementing that
        classification here would duplicate `homographs.py`'s own gate
        logic — exactly what "the adapter converts; it does not decide"
        forbids. `open_occurrences` is therefore a partial view: every
        tier-3-unresolved occurrence, but not every occurrence that blocks
        the gate. `summary["failed"]` is still the accurate, complete
        count of everything that blocks (unresolved-in-class, conflict, and
        stale together) — a caller (`narratarr/runner.py`) should gate on
        that number, not on `len(open_occurrences)`.
        """
        from abpipe import homographs
        from abpipe.meta import read_json

        ctx = self._ctx
        try:
            summary = homographs.run(ctx, write=write, use_llm=llm, book_config=self.book_config)
        except (KeyError, RuntimeError, TimeoutError) as exc:
            raise PipelineError(f"homograph_audit: {exc}") from exc

        decisions_doc = homographs.read_decisions(ctx.book_dir)
        review_data = read_json(ctx.book_dir / "homograph-review.json")
        open_occurrences = review_data.get("occurrences", []) if isinstance(review_data, dict) else []

        # The audit is the only stage that needs the transformer. Free it
        # before the render, or it sits in memory for the whole book.
        _release_homograph_tagger()
        return {
            "summary": summary,
            "decisions": decisions_doc.get("decisions", []),
            "open_occurrences": open_occurrences,
        }

    def homograph_candidates(self, chapter: str, chunk: str, word: str, occurrence: int) -> list[dict]:
        """Render BOTH readings of one occurrence. APP-CONTRACT section 6 /
        section 9.2.

        A person cannot choose a pronunciation from a phoneme string, so
        each of the word's readings in `heteronyms.json` (pipeline
        CONTRACT.md 18.2 — almost always exactly two) is force-rendered
        with the inline markup mechanism (pipeline CONTRACT.md 18.1,
        `[word](/phonemes/)`) and written to its own scratch WAV, so the
        caller can play each one.

        The chunk's OTHER existing homograph decisions (if any) are held
        constant while this one occurrence is swept across its readings —
        a candidate should sound like this chunk really would, not like a
        chunk with every other correction stripped out.

        Returns
        `[{"reading": "verb", "phonemes": "wˈWnd", "audio": "<path>"}, ...]`.
        Raises PipelineError when `word` is not in the heteronym inventory,
        when the book's dialect has no readings for it, or when
        `occurrence` does not exist in the chunk's current text.
        """
        from abpipe import homographs, render

        ctx = self._ctx
        word_l = word.lower()
        inventory = homographs.load_inventory()
        entry = inventory.get(word_l)
        if not entry:
            raise PipelineError(f"homograph_candidates: {word!r} is not in the heteronym inventory")

        dialect = homographs.dialect_for_lang_code(ctx.engine_config.get("lang_code", "b"))
        readings = (entry.get("readings") or {}).get(dialect) or {}
        if not readings:
            raise PipelineError(f"homograph_candidates: no {dialect!r} readings recorded for {word!r}")

        chunk_path = ctx.stage_dir("chunk", make=False) / chapter / f"{chunk}.txt"
        if not chunk_path.exists():
            raise PipelineError(f"homograph_candidates: no chunk text at {chunk_path}")
        text = chunk_path.read_text(encoding="utf-8")

        match_count = homographs.count_matches(word_l, text)
        if occurrence < 1 or occurrence > match_count:
            raise PipelineError(
                f"homograph_candidates: {word!r} occurrence {occurrence} not found in "
                f"{chapter}/{chunk} (the text holds {match_count})"
            )

        decisions_doc = homographs.read_decisions(ctx.book_dir)
        other_decisions = [
            d
            for d in homographs.decisions_for_chunk(decisions_doc, chapter, chunk)
            if not (str(d.get("word", "")).lower() == word_l and int(d.get("occurrence", 0) or 0) == occurrence)
        ]
        pronunciations = dict(ctx.book.get("pronunciations") or {})
        engine = self._get_engine()

        out_dir = ctx.book_dir / "review" / "homographs"
        candidates: list[dict] = []
        try:
            for reading, phonemes in readings.items():
                decision = {"word": word_l, "occurrence": occurrence, "phonemes": phonemes}
                # Markup first, then pronunciations -- pipeline CONTRACT.md 18.5.
                marked = homographs.apply_homographs(text, [*other_decisions, decision])
                marked = render.apply_pronunciations(marked, pronunciations)
                audio, sr = render.render_chunk(engine, marked)
                out_path = out_dir / f"{chapter}-{chunk}-{word_l}-{occurrence}-{reading}.wav"
                _write_wav_atomic(out_path, audio, sr)
                candidates.append({"reading": reading, "phonemes": phonemes, "audio": str(out_path)})
        except homographs.HomographError as exc:
            raise PipelineError(f"homograph_candidates: {exc}") from exc

        return candidates

    # ------------------------------------------------------------------ QC

    def qc_report(self) -> dict:
        """Return `05-qc/qc-report.json`, or `{}` when it does not exist yet."""
        from abpipe.meta import read_json

        data = read_json(self._ctx.stage_dir("qc", make=False) / "qc-report.json")
        return data if isinstance(data, dict) else {}

    def accept_chunk(self, chapter: str, chunk: str, reason: str) -> None:
        """Write `qc-accept.json`. The reason is mandatory. APP-CONTRACT
        section 6 / section 9.3. Pins the acceptance to the chunk's
        CURRENT on-disk WAV hash — pipeline CONTRACT.md 9.3/9.7: every pin
        voids on every re-render, because Kokoro is not deterministic.
        """
        if not isinstance(reason, str) or not reason.strip():
            raise PipelineError("accept_chunk: reason must not be empty")

        from abpipe import qc

        try:
            qc.accept_chunk(self._ctx, chapter, chunk, reason)
        except (ValueError, FileNotFoundError) as exc:
            raise PipelineError(f"accept_chunk {chapter}/{chunk}: {exc}") from exc

    def rerender_chunk(self, chapter: str, chunk: str) -> dict:
        """Force one chunk to render again, then re-run QC on it alone.

        `abpipe.render.run()` and `abpipe.qc.run()` both work at chapter
        granularity, with no per-chunk argument — `force=True` would
        re-render every chunk of the chapter, defeating the whole point of
        the Fix flow's per-chunk cost (APP-CONTRACT 9.5: "a one-chunk fix
        costs the whole chapter" is exactly the fault pruning causes, which
        this method must not reproduce on its own). Instead, this method
        clears only this one chunk's render and qc meta files
        (`abpipe.meta.clear_meta` — the same sanctioned "make this look
        absent" primitive `abpipe`'s own `--force` path uses, pipeline
        CONTRACT.md 3.3) and then calls both stages with `force=False`,
        scoped to the one chapter: every other chunk's meta is untouched
        and still fresh, so only the cleared chunk actually re-renders and
        re-scores.

        Pipeline CONTRACT.md 9.3: Kokoro is not deterministic, so the new
        render is not merely a retry of the same audio — it is rung 1 of
        the QC remediation ladder, run standalone, on demand, from the
        review UI's "rerender" action (APP-CONTRACT 13.3:
        `POST /review/items/{id}/rerender`).

        Returns `{"render": <render summary>, "qc": <qc summary>}` — both
        pass-through `abpipe` dicts, per "the adapter converts; it does not
        decide." APP-CONTRACT section 6 gives this method's signature with
        no further docstring; this behaviour is this worker's own reading,
        flagged in the report.
        """
        from abpipe import qc, render
        from abpipe.meta import clear_meta

        ctx = self._ctx
        render_wav = ctx.stage_dir("render", make=False) / chapter / f"{chunk}.wav"
        qc_out = ctx.stage_dir("qc", make=False) / chapter / f"{chunk}.json"
        clear_meta(render_wav)
        clear_meta(qc_out)

        try:
            engine = self._get_engine()
            render_summary = render.run(ctx, chapters=[chapter], force=False, engine=engine)
            qc_summary = qc.run(ctx, chapters=[chapter], force=False, engine=engine, book_config=self.book_config)
        except (KeyError, RuntimeError, TimeoutError) as exc:
            raise PipelineError(f"rerender_chunk {chapter}/{chunk}: {exc}") from exc

        return {"render": render_summary, "qc": qc_summary}

    # ------------------------------------------------------------------ artifacts

    def artifacts(self) -> dict:
        """Return the paths of book.json, the cover, and the m4b — each a
        string, or None when the file does not exist yet. Strings, not
        `Path` objects: this dict is the natural shape for an API response
        (APP-CONTRACT `GET /jobs/{id}/artifacts`), and a `Path` is not
        JSON-serialisable."""
        ctx = self._ctx
        m4b_path = ctx.stage_dir("bind", make=False) / f"{ctx.title}.m4b"
        return {
            "book_json": str(ctx.book_json) if ctx.book_json.exists() else None,
            "cover": str(ctx.cover_path) if ctx.cover_path.exists() else None,
            "m4b": str(m4b_path) if m4b_path.exists() else None,
        }

    def chunk_audio_path(self, chapter: str, chunk: str) -> Path | None:
        """Return the rendered WAV path of one chunk, or None when it does
        not exist yet."""
        path = self._ctx.stage_dir("render", make=False) / chapter / f"{chunk}.wav"
        return path if path.exists() else None

    # ------------------------------------------------------------------ deliver_book (stage 8's input)

    def deliver_book(self):
        """Return this book as a `narratarr.adapter.targets.base.DeliverBook`
        — the one shape every target's `deliver()` needs. APP-CONTRACT
        section 8.3, added at the overlord's request to feed
        `narratarr.adapter.targets.deliver_job()`.

        Requires `bind` (stage 7) to have already produced the m4b — raises
        `PipelineError` otherwise, per house rule 4: a delivery of nothing
        is worse than a crash.

        `narratarr.adapter.targets.base` is imported lazily here, not at
        module level, for the same reason every `abpipe` import in this
        module is lazy: `targets/base.py` is a plain, dependency-free
        dataclass module, so this import costs nothing real, but keeping
        the rule uniform (every cross-package import in this class body,
        never at module top) means a reader never has to remember an
        exception to the pattern.
        """
        from abpipe import ffmpeg

        from narratarr.adapter.targets.base import DeliverBook

        ctx = self._ctx
        m4b_path = ctx.stage_dir("bind", make=False) / f"{ctx.title}.m4b"
        if not m4b_path.exists():
            raise PipelineError(f"deliver_book: no finished m4b at {m4b_path}; run bind first")
        cover_path = ctx.cover_path if ctx.cover_path.exists() else None

        try:
            duration_s = ffmpeg.probe_duration(m4b_path)
        except Exception as exc:
            raise PipelineError(f"deliver_book: could not probe the duration of {m4b_path}: {exc}") from exc

        try:
            chapters = len(ctx.chapter_ids())
        except KeyError as exc:
            raise PipelineError(f"deliver_book: {exc}") from exc

        return DeliverBook(
            slug=ctx.slug,
            title=ctx.title,
            author=ctx.author,
            year=ctx.book.get("year"),
            genre=ctx.book.get("genre"),
            m4b=m4b_path,
            cover=cover_path,
            duration_s=duration_s,
            chapters=chapters,
        )

    # ------------------------------------------------------------------ prune (pipeline CONTRACT.md section 15)

    def prune_chapters(self, chapters: list[str] | None = None, dry_run: bool = False) -> dict:
        """Remove the intermediate audio of finished, verified chapters.
        APP-CONTRACT section 6, added at the overlord's request.

        Wraps `abpipe.prune.prune_all()` — every guard pipeline CONTRACT.md
        section 15 documents (the m4a exists and its meta is fresh, the QC
        report holds a clean entry with zero `needs_human`, the probed
        duration is greater than zero, plus that module's own two extra
        guards: the duration is not implausibly short, and no candidate
        path is a symlink or escapes the book directory) lives in
        `abpipe.prune` alone. This method does not re-check, weaken, or
        duplicate a single one of them — "the adapter converts; it does
        not decide" applies here as everywhere else in this class.
        `prune_chapter` itself never raises for an ordinary refusal (a
        chapter that is not yet eligible); this method only translates an
        actual fault (an unknown chapter id) into `PipelineError`.

        **Never extend pruning to the m4a file.** `abpipe.prune` already
        keeps this rule — it removes only `04-audio/<id>/*.wav` and
        `06-chapters/<id>.wav`, never the `.m4a` beside them — and this
        method must never work around it. The m4a is the one durable
        artifact a later Fix (APP-CONTRACT 9.5) can still reuse.

        **Warning, pipeline CONTRACT.md 15.1 — pruning is genuinely
        dangerous, with a measured scar:** a real render used
        `--prune`, and 9 wrong heteronym readings were found after
        delivery. Because prune had removed all 4,011 chunk WAV files
        across the affected chapters, the fix cost **2,936 chunks
        re-rendered, about 55 minutes, to correct 9 chunks** — 71 percent
        of a whole book rebuild for a few seconds of audio. Pruning is
        safe only when a book is truly finished and no fix is pending.

        **This method does not decide whether pruning is safe to run right
        now.** The caller (`narratarr/runner.py`'s `_maybe_prune`) owns
        that outer decision — the job is `done`, its review queue is
        empty, and `NARRATARR_PRUNE` is on (APP-CONTRACT section 5.2 rule
        4) — before this method is ever called. This method only performs
        the prune abpipe's own per-chapter guards allow; it holds no
        opinion about whether the moment is right to call it at all.
        """
        from abpipe import prune

        try:
            return prune.prune_all(self._ctx, chapters=chapters, dry_run=dry_run)
        except KeyError as exc:
            raise PipelineError(f"prune_chapters: {exc}") from exc


def _release_homograph_tagger() -> None:
    """Free the homograph audit's transformer. Never raise.

    **Warning: this is a memory fix, and it was measured, not guessed.**
    `abpipe.homograph_tiers` caches `en_core_web_trf` in a module global and
    frees it nowhere. That is right for the command line, whose process ends
    a moment later, and wrong for a service that runs for hours.

    Measured on the server, in a container capped at 5 GB: with the
    transformer resident the render stage held 4.2 to 4.6 GB, against 2.5 GB
    without it. The QC stage then loads a whisper model of about 2.1 GB on
    top. The job would have run out of memory part of the way through a
    render of several hours.

    The audit runs once, before the render. The render and the QC stage never
    need the tagger. So it goes as soon as the audit is done, and the next
    audit loads it again.
    """
    try:
        from abpipe import homograph_tiers
    except Exception:  # the pipeline extra is not installed
        return
    release = getattr(homograph_tiers, "release_transformer", None)
    try:
        if release is not None:
            release()
        else:
            # An older vendored copy has no public function. Clear the cache
            # it really uses, then collect.
            homograph_tiers._TRF_NLP = None
            import gc

            gc.collect()
    except Exception:
        return
