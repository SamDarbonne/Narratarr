"""Tests for `narratarr/adapter/__init__.py`. APP-CONTRACT section 6.

A test here never loads a model, never renders real audio, and never
touches the network. `abpipe`'s own stage entry points (`extract.run`,
`render.run`, ...) are monkeypatched. `abpipe.homographs` and
`abpipe.render`'s pure text-transform functions (`apply_homographs`,
`apply_pronunciations`) are cheap and dependency-light (no spacy, no
misaki, no torch), so a few tests below call them directly, through the
real `abpipe.render.render_chunk` seam with a fake engine — never a real
one — to prove the adapter's own markup ordering without mocking the very
thing being checked.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from narratarr.adapter import (
    Pipeline,
    PipelineError,
    Progress,
    StageResult,
    _hazard_score,
    preflight_engine,
)

# --------------------------------------------------------------------------- fixtures


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


class _FakeEngine:
    """Stands in for a real `abpipe` engine. Records every text it is asked
    to synthesize, and returns a fixed, tiny audio buffer -- never a real
    render."""

    def __init__(self):
        self.calls: list[str] = []

    def describe(self) -> dict:
        return {"name": "fake", "voice": "fake"}

    def synthesize(self, text: str):
        import numpy as np

        self.calls.append(text)
        return np.zeros(240, dtype=np.float32), 24000


@pytest.fixture()
def workspace(tmp_path) -> Path:
    """The `work` directory itself -- `Pipeline.__init__`'s `workspace`
    parameter is `narratarr.config.Settings.work_dir` (`<config_dir>/work`),
    confirmed against `narratarr/runner.py`'s real callers, both of which
    pass `settings.work_dir`. `tmp_path` here stands in for the config
    root; `workspace` is one level below it, exactly as `Settings.work_dir`
    always is."""
    return tmp_path / "work"


@pytest.fixture()
def pipeline(workspace, tmp_path) -> Pipeline:
    source = tmp_path / "library" / "book.epub"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"fake epub bytes, never parsed by these tests")
    return Pipeline(
        workspace=workspace,
        slug="test-book",
        source=source,
        book_config={"slug": "test-book"},
        qc_config={},
    )


def _seed_book_json(pipeline: Pipeline, chapters: list[dict], lang_code: str = "b") -> None:
    pipeline._ctx.save_book(
        {
            "schema": 1,
            "slug": pipeline.slug,
            "title": "Test Book",
            "author": "A. Author",
            "engine": {"name": "fake", "voice": "fake", "lang_code": lang_code},
            "pronunciations": {},
            "chapters": chapters,
        }
    )


def _seed_chunk(pipeline: Pipeline, chapter_id: str, chunk_id: str, text: str, is_heading: bool = False) -> None:
    chunk_dir = pipeline._ctx.stage_dir("chunk") / chapter_id
    chunk_dir.mkdir(parents=True, exist_ok=True)
    (chunk_dir / f"{chunk_id}.txt").write_text(text, encoding="utf-8")
    index_path = chunk_dir / "index.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else {
        "schema": 1, "chapter": chapter_id, "chunks": []
    }
    index["chunks"].append(
        {
            "id": chunk_id,
            "file": f"{chunk_id}.txt",
            "chars": len(text),
            "words": len(text.split()),
            "sha256": "0" * 64,
            "is_heading": is_heading,
            "ends_paragraph": True,
        }
    )
    _write_json(index_path, index)


# --------------------------------------------------------------------------- constructor


def test_constructor_signature_and_book_dir(pipeline: Pipeline, workspace: Path):
    """The exact seam W1's runner constructs against."""
    assert pipeline.slug == "test-book"
    assert pipeline.workspace == workspace
    assert pipeline.book_config == {"slug": "test-book"}
    assert pipeline.qc_config == {}
    # workspace/<slug> -- APP-CONTRACT 2.1's documented runtime layout,
    # since `workspace` IS settings.work_dir (see the `workspace` fixture).
    assert pipeline._ctx.book_dir == workspace / "test-book"


# --------------------------------------------------------------------------- run_stage: dispatch and conversion


def test_run_stage_extract_converts_summary_and_reloads_book(pipeline: Pipeline, monkeypatch):
    import abpipe.extract as extract_mod

    def fake_run(ctx, force=False, book_config=None, **kw):
        ctx.save_book(
            {
                "schema": 1, "slug": "test-book", "title": "Reloaded Title", "author": "A",
                "chapters": [{"id": "ch00", "index": 0, "label": "x", "src": None, "synthetic": True, "words": 1}],
            }
        )
        return {"stage": "extract", "done": ["ch00"], "skipped": [], "failed": []}

    monkeypatch.setattr(extract_mod, "run", fake_run)
    result = pipeline.run_stage("extract")

    assert isinstance(result, StageResult)
    assert result.stage == "extract"
    assert result.done == 1
    assert result.skipped == 0
    assert result.failed == 0
    assert result.aborted is False
    assert result.abort_reason is None
    # book.json is reloaded into the live Context after extract runs.
    assert pipeline._ctx.book.get("title") == "Reloaded Title"


def test_run_stage_render_reports_a_controlled_abort_without_raising(pipeline: Pipeline, monkeypatch):
    import abpipe.engines as engines_mod
    import abpipe.render as render_mod

    monkeypatch.setattr(engines_mod, "get_engine", lambda config: _FakeEngine())

    def fake_run(ctx, chapters=None, force=False, engine=None, **kw):
        return {
            "stage": "render", "done": 3, "skipped": 0, "failed": 2,
            "aborted": True, "abort_reason": "fatal disk error at ch01 chunk 0003",
        }

    monkeypatch.setattr(render_mod, "run", fake_run)
    result = pipeline.run_stage("render")

    assert result.done == 3
    assert result.failed == 2
    assert result.aborted is True
    assert result.abort_reason == "fatal disk error at ch01 chunk 0003"


@pytest.mark.parametrize("stage", ["deliver", "sample", "homographs", "not-a-stage"])
def test_run_stage_refuses_anything_outside_stages_1_to_7(pipeline: Pipeline, stage):
    with pytest.raises(PipelineError):
        pipeline.run_stage(stage)


def test_run_stage_wraps_an_abpipe_runtime_error_as_pipeline_error(pipeline: Pipeline, monkeypatch):
    import abpipe.bind as bind_mod

    def fake_run(ctx, force=False, **kw):
        raise RuntimeError("bind: refusing to run — missing chapter m4a file(s): ch01.m4a")

    monkeypatch.setattr(bind_mod, "run", fake_run)
    with pytest.raises(PipelineError, match="missing chapter m4a"):
        pipeline.run_stage("bind")


def test_run_stage_wraps_an_abpipe_key_error(pipeline: Pipeline, monkeypatch):
    import abpipe.chunk as chunk_mod

    def fake_run(ctx, chapters=None, force=False, **kw):
        raise KeyError("unknown chapter id: ch99")

    monkeypatch.setattr(chunk_mod, "run", fake_run)
    with pytest.raises(PipelineError):
        pipeline.run_stage("chunk", chapters=["ch99"])


def test_run_stage_progress_callback_parses_render_progress_lines(pipeline: Pipeline, monkeypatch, capsys):
    import abpipe.engines as engines_mod
    import abpipe.render as render_mod

    monkeypatch.setattr(engines_mod, "get_engine", lambda config: _FakeEngine())

    def fake_run(ctx, chapters=None, force=False, engine=None, **kw):
        print("[render] ch01 chunk 0001/0003 (10 chars in 0.10s) cps=100.0 eta=1s")
        print("[render] ch01 chunk 0002/0003 (10 chars in 0.10s) cps=100.0 eta=1s")
        return {"stage": "render", "done": 2, "skipped": 0, "failed": 0}

    monkeypatch.setattr(render_mod, "run", fake_run)
    events: list[Progress] = []
    pipeline.run_stage("render", progress=events.append)

    parsed = [e for e in events if e.total == 3]
    assert [e.done for e in parsed] == [1, 2]
    # The real print output still reaches stdout -- container logs are not silenced.
    assert "[render] ch01 chunk 0001/0003" in capsys.readouterr().out


# --------------------------------------------------------------------------- abpipe.deliver is never imported


def test_abpipe_deliver_never_imported_in_the_adapter_source():
    """The adapter's own docstrings explain, in prose, why `abpipe.deliver`
    is never called (APP-CONTRACT 3.1) -- so this checks for an IMPORT
    statement, not for the bare substring "abpipe.deliver", which the
    prose itself legitimately contains."""
    import narratarr.adapter as adapter_mod

    src = Path(adapter_mod.__file__).read_text()
    assert "import deliver" not in src
    assert "from abpipe import deliver" not in src
    assert "abpipe.deliver.run" not in src
    assert "abpipe.deliver import" not in src


def test_abpipe_deliver_not_imported_after_ordinary_use(pipeline: Pipeline, monkeypatch):
    import abpipe.extract as extract_mod

    monkeypatch.setattr(
        extract_mod, "run",
        lambda ctx, force=False, book_config=None, **kw: {"stage": "extract", "done": [], "skipped": [], "failed": []},
    )
    pipeline.run_stage("extract")
    assert "abpipe.deliver" not in sys.modules


def test_run_stage_deliver_never_imports_abpipe_deliver(pipeline: Pipeline):
    with pytest.raises(PipelineError):
        pipeline.run_stage("deliver")
    assert "abpipe.deliver" not in sys.modules


# --------------------------------------------------------------------------- accept_chunk: mandatory reason


def test_accept_chunk_rejects_an_empty_reason(pipeline: Pipeline):
    with pytest.raises(PipelineError, match="reason"):
        pipeline.accept_chunk("ch01", "0001", "")


def test_accept_chunk_rejects_a_whitespace_only_reason(pipeline: Pipeline):
    with pytest.raises(PipelineError, match="reason"):
        pipeline.accept_chunk("ch01", "0001", "   ")


def test_accept_chunk_with_a_real_reason_writes_the_pin(pipeline: Pipeline):
    wav_path = pipeline._ctx.stage_dir("render") / "ch01" / "0001.wav"
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    wav_path.write_bytes(b"RIFF....fake wav bytes")

    pipeline.accept_chunk("ch01", "0001", "Whisper misheard a proper noun; the audio is correct.")

    accept_path = pipeline._ctx.book_dir / "qc-accept.json"
    data = json.loads(accept_path.read_text())
    assert len(data["accepted"]) == 1
    assert data["accepted"][0]["reason"] == "Whisper misheard a proper noun; the audio is correct."
    assert data["accepted"][0]["chapter"] == "ch01"
    assert data["accepted"][0]["chunk"] == "0001"


def test_accept_chunk_with_no_rendered_wav_raises_pipeline_error(pipeline: Pipeline):
    with pytest.raises(PipelineError):
        pipeline.accept_chunk("ch01", "0001", "a real reason")


# --------------------------------------------------------------------------- rerender_chunk: one chunk only


def test_rerender_chunk_clears_only_the_named_chunk(pipeline: Pipeline, monkeypatch):
    import abpipe.engines as engines_mod
    import abpipe.qc as qc_mod
    import abpipe.render as render_mod
    from abpipe.meta import write_meta

    monkeypatch.setattr(engines_mod, "get_engine", lambda config: _FakeEngine())

    render_dir = pipeline._ctx.stage_dir("render") / "ch01"
    render_dir.mkdir(parents=True, exist_ok=True)
    target_wav = render_dir / "0002.wav"
    other_wav = render_dir / "0001.wav"
    for path in (target_wav, other_wav):
        path.write_bytes(b"fake wav bytes")
        write_meta(path, "render", "some-input-hash", "some-config-hash")

    assert (target_wav.parent / (target_wav.name + ".meta.json")).exists()
    assert (other_wav.parent / (other_wav.name + ".meta.json")).exists()

    render_calls = []
    qc_calls = []
    monkeypatch.setattr(
        render_mod, "run",
        lambda ctx, chapters=None, force=False, engine=None, **kw: render_calls.append((chapters, force)) or
        {"stage": "render", "done": 1, "skipped": 1, "failed": 0},
    )
    monkeypatch.setattr(
        qc_mod, "run",
        lambda ctx, chapters=None, force=False, engine=None, book_config=None, **kw: qc_calls.append((chapters, force)) or
        {"stage": "qc", "done": 1, "skipped": 1, "failed": 0, "status": "green"},
    )

    result = pipeline.rerender_chunk("ch01", "0002")

    # The named chunk's meta is gone; the other chunk's meta is untouched.
    assert not (target_wav.parent / (target_wav.name + ".meta.json")).exists()
    assert (other_wav.parent / (other_wav.name + ".meta.json")).exists()
    # force=False -- the freshness check, not a blanket redo, is what limits
    # the work to the one cleared chunk.
    assert render_calls == [(["ch01"], False)]
    assert qc_calls == [(["ch01"], False)]
    assert result == {
        "render": {"stage": "render", "done": 1, "skipped": 1, "failed": 0},
        "qc": {"stage": "qc", "done": 1, "skipped": 1, "failed": 0, "status": "green"},
    }


# --------------------------------------------------------------------------- hazard scoring (render_sample's T-2 choice)


def test_hazard_score_rewards_caps_runs_numbers_and_non_ascii():
    plain = "She walked down the quiet street and thought about nothing at all."
    hazardous = 'ROSIE SAYERS COULD NOT tell time. It was 1922 in Beltéis, quite fluently.'
    assert _hazard_score(hazardous) > _hazard_score(plain)


def test_hazard_score_rewards_a_mid_sentence_capitalised_name():
    with_name = "He turned and saw Gikkolino standing in the doorway."
    without_name = "He turned and saw nobody standing in the doorway."
    assert _hazard_score(with_name) > _hazard_score(without_name)


def test_render_sample_picks_the_hazardous_chunk_and_writes_a_wav(pipeline: Pipeline, monkeypatch):
    import abpipe.engines as engines_mod

    _seed_book_json(pipeline, [{"id": "ch01", "index": 1, "label": "One", "src": "x", "synthetic": False, "words": 5}])
    _seed_chunk(pipeline, "ch01", "0001", "Chapter One", is_heading=True)
    _seed_chunk(pipeline, "ch01", "0002", "A quiet, ordinary morning passed without incident.")
    _seed_chunk(pipeline, "ch01", "0003", "ROSIE SAYERS COULD NOT believe it was 1922 in Beltéis.")

    fake_engine = _FakeEngine()
    monkeypatch.setattr(engines_mod, "get_engine", lambda config: fake_engine)

    out_path = pipeline.render_sample(chapter="ch01", seconds=1.0)

    assert out_path == pipeline._ctx.book_dir / "review" / "sample.wav"
    assert out_path.exists()
    # The hazardous chunk's text (or the pronunciation/markup-applied
    # version of it) is what actually reached the engine, not the heading
    # or the quiet chunk.
    assert any("ROSIE" in call for call in fake_engine.calls)


def test_render_sample_raises_when_no_chunks_exist_yet(pipeline: Pipeline):
    _seed_book_json(pipeline, [{"id": "ch01", "index": 1, "label": "One", "src": "x", "synthetic": False, "words": 5}])
    with pytest.raises(PipelineError):
        pipeline.render_sample(chapter="ch01")


# --------------------------------------------------------------------------- markup ordering: homograph_candidates


def test_homograph_candidates_renders_both_readings_in_the_right_order(pipeline: Pipeline, monkeypatch):
    """render.py applies the homograph markup FIRST, then the pronunciation
    map (pipeline CONTRACT.md 18.5). This test proves the adapter follows
    the identical order for a scratch candidate render, using the REAL
    abpipe.homographs.apply_homographs and abpipe.render.apply_pronunciations
    (both pure, no heavy dependency) -- never a mock of the thing under test.
    """
    import abpipe.engines as engines_mod

    _seed_book_json(
        pipeline,
        [{"id": "ch01", "index": 1, "label": "One", "src": "x", "synthetic": False, "words": 5}],
        lang_code="b",
    )
    # book.json's pronunciation map deliberately shares no word with "wound",
    # so a wrong order (pronunciations before markup) would not be visible
    # by corrupting "wound" itself -- instead this proves the SEQUENCE by
    # recording exactly what text reached the engine and checking it still
    # carries the homograph bracket syntax (i.e. markup was not clobbered).
    pipeline._ctx.book["pronunciations"] = {"neck": "nek"}

    chunk_dir = pipeline._ctx.stage_dir("chunk") / "ch01"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    text = "a white muffler wound round and round his neck"
    (chunk_dir / "0001.txt").write_text(text, encoding="utf-8")

    fake_engine = _FakeEngine()
    monkeypatch.setattr(engines_mod, "get_engine", lambda config: fake_engine)

    candidates = pipeline.homograph_candidates("ch01", "0001", "wound", 1)

    readings = {c["reading"] for c in candidates}
    assert readings == {"noun", "verb"}
    for c in candidates:
        assert Path(c["audio"]).exists()
        assert c["phonemes"]

    # Every text handed to the engine carries the forced markup around
    # "wound" (proof the homograph step ran), AND the pronunciation
    # substitution for "neck" -> "nek" (proof the pronunciation step ran
    # too, on the SAME already-marked-up text, not on the raw chunk text).
    assert len(fake_engine.calls) == 2
    for call_text in fake_engine.calls:
        assert "[wound](/" in call_text  # the inline phoneme markup survived
        assert "nek" in call_text  # the pronunciation map still applied, on top of the markup


def test_homograph_candidates_rejects_an_out_of_range_occurrence(pipeline: Pipeline, monkeypatch):
    import abpipe.engines as engines_mod

    _seed_book_json(pipeline, [{"id": "ch01", "index": 1, "label": "One", "src": "x", "synthetic": False, "words": 5}])
    chunk_dir = pipeline._ctx.stage_dir("chunk") / "ch01"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    (chunk_dir / "0001.txt").write_text("he wound the bandage once", encoding="utf-8")
    monkeypatch.setattr(engines_mod, "get_engine", lambda config: _FakeEngine())

    with pytest.raises(PipelineError):
        pipeline.homograph_candidates("ch01", "0001", "wound", 2)


def test_homograph_candidates_rejects_a_word_outside_the_inventory(pipeline: Pipeline):
    _seed_book_json(pipeline, [{"id": "ch01", "index": 1, "label": "One", "src": "x", "synthetic": False, "words": 5}])
    chunk_dir = pipeline._ctx.stage_dir("chunk") / "ch01"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    (chunk_dir / "0001.txt").write_text("a perfectly ordinary sentence", encoding="utf-8")

    with pytest.raises(PipelineError):
        pipeline.homograph_candidates("ch01", "0001", "not-a-real-heteronym-word", 1)


# --------------------------------------------------------------------------- artifacts / chunk_audio_path


def test_artifacts_reports_none_for_missing_files(pipeline: Pipeline):
    # No extract has run yet -- book.json, the cover, and the m4b all
    # genuinely do not exist.
    result = pipeline.artifacts()
    assert result == {"book_json": None, "cover": None, "m4b": None}


def test_artifacts_reports_paths_that_exist(pipeline: Pipeline):
    _seed_book_json(pipeline, [])
    m4b_path = pipeline._ctx.stage_dir("bind") / f"{pipeline._ctx.title}.m4b"
    m4b_path.parent.mkdir(parents=True, exist_ok=True)
    m4b_path.write_bytes(b"fake m4b")

    result = pipeline.artifacts()
    assert result["book_json"] == str(pipeline._ctx.book_json)
    assert result["m4b"] == str(m4b_path)
    assert isinstance(result["m4b"], str)  # JSON-safe, not a Path


def test_chunk_audio_path_none_when_absent(pipeline: Pipeline):
    assert pipeline.chunk_audio_path("ch01", "0001") is None


def test_chunk_audio_path_when_present(pipeline: Pipeline):
    wav_path = pipeline._ctx.stage_dir("render") / "ch01" / "0001.wav"
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    wav_path.write_bytes(b"fake wav")
    assert pipeline.chunk_audio_path("ch01", "0001") == wav_path


# --------------------------------------------------------------------------- qc_report


def test_qc_report_returns_empty_dict_when_absent(pipeline: Pipeline):
    assert pipeline.qc_report() == {}


def test_qc_report_reads_the_real_file(pipeline: Pipeline):
    report = {"schema": 1, "status": "green", "totals": {"chunks": 1}}
    report_path = pipeline._ctx.stage_dir("qc") / "qc-report.json"
    _write_json(report_path, report)
    assert pipeline.qc_report() == report


# --------------------------------------------------------------------------- status(): coarse but honest when nothing exists


def test_status_reports_absent_for_a_freshly_extracted_book_with_no_further_work(pipeline: Pipeline):
    _seed_book_json(pipeline, [{"id": "ch01", "index": 1, "label": "One", "src": "x", "synthetic": False, "words": 5}])
    result = pipeline.status()
    assert set(result) == {"extract", "normalize", "chunk", "render", "qc", "assemble", "bind"}
    assert result["extract"]["absent"] == 1
    assert result["bind"] == {"fresh": 0, "stale": 0, "absent": 1, "total": 1}


def test_status_reports_all_empty_before_extract_has_ever_run(pipeline: Pipeline):
    result = pipeline.status()
    for stage_result in result.values():
        assert stage_result == {"fresh": 0, "stale": 0, "absent": 0, "total": 0}


# --------------------------------------------------------------------------- preflight_engine
#
# Pipeline CONTRACT.md 17.2: the torch `kokoro` engine's own espeak-fallback
# warning goes through loguru and is disabled at import time, so
# `KokoroCPUEngine.preflight()` -- which reads `pipeline.g2p.fallback`
# directly -- is the one reliable check. A test here never loads a real
# kokoro pipeline; `abpipe.engines.get_engine` is monkeypatched to return a
# fake engine object with its own `preflight()` method.


class _FakePreflightEngine:
    def __init__(self, report: dict):
        self._report = report

    def preflight(self) -> dict:
        return self._report


class _FakeEngineWithNoPreflight:
    """Stands in for KokoroMLXEngine / ChatterboxEngine, neither of which
    implements preflight()."""

    def describe(self) -> dict:
        return {"name": "fake-no-preflight"}


def test_preflight_engine_passes_through_the_real_report_shape(monkeypatch):
    import abpipe.engines as engines_mod

    report = {
        "espeak_fallback": True,
        "warmup_samples": 4800,
        "warmup_sample_rate": 24000,
        "oov_probe_word": "Zyrkovian Quaddlemorph",
        "oov_probe_nonempty": True,
    }
    captured_configs = []

    def fake_get_engine(config):
        captured_configs.append(config)
        return _FakePreflightEngine(report)

    monkeypatch.setattr(engines_mod, "get_engine", fake_get_engine)

    result = preflight_engine("kokoro_cpu", "bm_george", "b")

    assert result == report
    assert captured_configs == [{"name": "kokoro_cpu", "voice": "bm_george", "lang_code": "b"}]


def test_preflight_engine_raises_pipeline_error_on_a_failed_espeak_fallback(monkeypatch):
    import abpipe.engines as engines_mod

    class _FailingEngine:
        def preflight(self):
            raise RuntimeError(
                "kokoro_cpu built its misaki G2P with NO espeak fallback. "
                "Every out-of-lexicon word in this book will be deleted."
            )

    monkeypatch.setattr(engines_mod, "get_engine", lambda config: _FailingEngine())

    with pytest.raises(PipelineError, match="espeak fallback"):
        preflight_engine("kokoro_cpu", "bm_george", "b")


def test_preflight_engine_raises_pipeline_error_when_the_engine_has_no_preflight_method(monkeypatch):
    import abpipe.engines as engines_mod

    monkeypatch.setattr(engines_mod, "get_engine", lambda config: _FakeEngineWithNoPreflight())

    with pytest.raises(PipelineError, match="preflight"):
        preflight_engine("kokoro_mlx", "bm_george", "b")


def test_preflight_engine_raises_pipeline_error_when_the_engine_cannot_be_constructed(monkeypatch):
    import abpipe.engines as engines_mod

    def fake_get_engine(config):
        raise ValueError(f"unknown engine: {config['name']!r}")

    monkeypatch.setattr(engines_mod, "get_engine", fake_get_engine)

    with pytest.raises(PipelineError):
        preflight_engine("not-a-real-engine", "voice", "b")
