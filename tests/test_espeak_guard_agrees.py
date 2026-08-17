"""The two espeak log checks must look for the same string.

APP-CONTRACT.md 11.2 and the pipeline contract 17.2 describe one secondary
check. Two modules implement it: `narratarr.runner` at run time, and
`scripts.espeak_guard` for an operator. Both are correct today.

**Two correct copies of one check is how the next blind spot is born.** The
whole fault this check exists for is a warning that was never written. A
second copy that greps a slightly different string would fail in exactly the
same silent way. This test holds the two together.
"""
import importlib.util
import pathlib

from narratarr import runner

_GUARD = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "espeak_guard.py"


def _load_guard():
    spec = importlib.util.spec_from_file_location("espeak_guard", _GUARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_two_espeak_checks_grep_the_same_string():
    guard = _load_guard()
    assert runner.ESPEAK_WARNING == guard.FALLBACK_WARNING


def test_the_string_is_the_one_the_library_really_writes():
    """The string comes from kokoro/pipeline.py and mlx_audio's pipeline.py.

    Refer to the pipeline contract 17.1 and 17.2. A typo here disables the
    check and nothing else fails.
    """
    assert runner.ESPEAK_WARNING == "EspeakFallback not Enabled"
