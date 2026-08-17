"""Stage 5 -- QC.

CONTRACT.md section 9 defines every rule in this module. The stage transcribes
each chunk WAV, compares the transcript to the chunk text, and flags a chunk
that does not match closely enough. A flagged chunk runs the remediation
ladder (section 9.3) automatically before it is marked ``needs_human``.

Two transcribers back the transcription step, chosen by ``whisper_backend``
(CONTRACT.md 9.2): ``WhisperTranscriber`` wraps mlx-whisper, Apple-silicon
only; ``FasterWhisperTranscriber`` wraps faster-whisper (CTranslate2), for a
Linux CPU host where mlx-whisper cannot run. ``run()``'s transcriber
construction and ``_select_transcriber_backend()`` hold the selection rule.

Worker C owns this file exclusively.
"""

from __future__ import annotations

import difflib
import os
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

from abpipe import normalize
from abpipe.meta import (
    clear_meta,
    hash_file,
    hash_many,
    hash_obj,
    is_fresh,
    read_json,
    utc_stamp,
    write_json,
    write_meta,
)

# --------------------------------------------------------------------------- config

DEFAULT_QC_CONFIG: dict[str, Any] = {
    "schema": 1,
    "wer_max": 0.15,
    "coverage_min": 0.90,
    "token_similarity_min": 0.85,
    "duration_outlier_factor": 3.0,
    "min_tokens_for_wer": 8,
    # Defect 1 fix (CONTRACT.md 9.2): a chunk shorter than this many
    # characters is exempt from the duration-outlier test outright, the
    # same shape of guard min_tokens_for_wer already is for the wer test.
    # Belt-and-braces alongside the intercept+slope model below: it protects
    # the smallest chunks (a one-word exclamation) even in a chapter whose
    # per-chapter fit happens to be poorly conditioned. See
    # _fit_duration_model()'s module comment for the real incident that
    # motivated this (a 7-character chunk, "Gyko!", whose clean 1.43s render
    # measured 3.1x the chapter's chars-only median ratio).
    "min_chars_for_duration_test": 15,
    "max_token_repeat": 2,
    "whisper_model": "mlx-community/whisper-large-v3-turbo",
    "condition_on_previous_text": False,
    # Overlord addition (real ch08/0128, "Aha!" -> whisper's "oh thats not
    # good"): a source chunk with fewer tokens than this cannot be validated
    # by transcript comparison at all -- whisper has no context and invents
    # freely. Below this floor the stage skips wer/coverage and instead
    # checks the audio is present and plausible; see _score()'s audio_only
    # branch and this module's "short chunk" section.
    "min_tokens_for_coverage": 3,
    # The RMS floor a short chunk's audio must clear to count as "not
    # silent". Measured against two real short chunks of this book
    # (ch08/0128 "Aha!" rms=0.054, ch08/0059 rms=0.058): set an order of
    # magnitude below that, so genuine quiet speech is never mistaken for a
    # failed (silent) render.
    "min_rms_for_short_chunk": 0.005,
    # The lower bound of the "generous band" _score()'s audio_only branch
    # checks a short chunk's duration against, as a multiple of the chapter's
    # fitted expected duration. The upper bound reuses duration_outlier_factor
    # -- the same runaway detector every other chunk gets.
    "short_chunk_duration_factor_low": 0.2,
    # Worker C addition (linux-engines): mlx_whisper only runs on Apple
    # silicon. "auto" picks WhisperTranscriber (mlx_whisper) when that
    # package imports, and FasterWhisperTranscriber otherwise -- the Linux
    # CPU container has no mlx_whisper, so it always falls to "faster". Set
    # "mlx" or "faster" to force one backend regardless of what imports. See
    # run()'s transcriber construction and _select_transcriber_backend().
    "whisper_backend": "auto",
    # The faster-whisper (CTranslate2) model id, used only when the backend
    # is "faster". "small.en" is a starting point, not a measurement: an
    # English-only model (this pipeline's books are English) is smaller and
    # more accurate than the multilingual model of the same size, and
    # small.en's int8 footprint is light enough for a 4-core CPU with
    # limited RAM. The overlord fixes this from a real RAM measurement on
    # the target machine -- change it in this one place only. See this
    # module's report to the overlord for the full candidate table.
    "faster_whisper_model": "small.en",
    # CTranslate2's quantization for CPU. int8 halves memory and disk
    # against float32 with a small, well-documented accuracy cost -- the
    # standard choice for CPU inference.
    "faster_whisper_compute_type": "int8",
    # 0 means "let the library pick" (faster_whisper.WhisperModel defaults
    # to 4 CPU threads). Set explicitly once the target container's core
    # count is known.
    "faster_whisper_cpu_threads": 0,
    # The library's own default (faster_whisper.WhisperModel.transcribe's
    # beam_size). A lower value decodes faster at a cost to accuracy -- see
    # FasterWhisperTranscriber's docstring.
    "faster_whisper_beam_size": 5,
}

# Worker C addition (config_hash stability across the linux-engines change):
# CONTRACT.md 9.2's config_hash covers the whole merged qc-config, so adding
# a key to DEFAULT_QC_CONFIG can flip config_hash for every chunk of every
# already-delivered book, staling four green, shipped audiobooks for zero
# behaviour change. The rule that prevents it: a key in this tuple never
# affects config_hash while it holds its DEFAULT_QC_CONFIG value. A
# qc-config.json that never heard of the key (the untouched, already-shipped
# case) and one that explicitly repeats the default hash identically. A
# qc-config.json that sets the key to something else still changes hash,
# correctly, because that book's QC behaviour did change. Every future key
# added to DEFAULT_QC_CONFIG that must not disturb an already-shipped book's
# hash belongs in this tuple too. See _config_for_hash() and run()'s
# cfg_hash line.
NEW_QC_CONFIG_KEYS_SINCE_LINUX_ENGINES = (
    "whisper_backend",
    "faster_whisper_model",
    "faster_whisper_compute_type",
    "faster_whisper_cpu_threads",
    "faster_whisper_beam_size",
)


def _config_for_hash(config: dict) -> dict:
    """Return the qc-config dict CONTRACT.md 9.2's config_hash should cover.

    A copy of `config` with each key in NEW_QC_CONFIG_KEYS_SINCE_LINUX_ENGINES
    dropped when it still holds its DEFAULT_QC_CONFIG value -- see the
    comment above that tuple. `config` itself is never mutated: the caller
    still uses the original dict for every ordinary lookup.
    """
    hashed = dict(config)
    for key in NEW_QC_CONFIG_KEYS_SINCE_LINUX_ENGINES:
        if hashed.get(key, DEFAULT_QC_CONFIG[key]) == DEFAULT_QC_CONFIG[key]:
            hashed.pop(key, None)
    return hashed


def qc_config_hash(
    config: dict, engine_desc: dict, pronunciations: dict | None = None
) -> str:
    """Return stage 5's config_hash (CONTRACT.md 9.2): the qc-config, plus
    `engine.describe()`, plus the pronunciation map of 9.6.

    Exposed as a standalone function for the same reason render.py exposes
    `render_config_hash()`. `cli.py` needs this exact number to answer
    `abpipe status` and to decide whether a chapter is complete. Before this
    function existed, `cli.py` wrote the formula out a second time by hand.
    Two copies of one formula is a fault that this module has already shipped
    once: refer to the warning in CONTRACT.md section 14 and to
    `_status_render`. A second copy went stale the moment
    NEW_QC_CONFIG_KEYS_SINCE_LINUX_ENGINES arrived, because the copy hashed
    the raw config and this one drops a defaulted new key. `status` would
    then report every chapter stale for ever, on a book that is correct.

    **A caller never writes this formula again. A caller calls this
    function.**
    """
    return hash_obj(
        {
            "qc_config": _config_for_hash(config),
            "engine": engine_desc,
            "pronunciations": dict(pronunciations or {}),
        }
    )

# The closed set of values `resolution` (CONTRACT.md 9.4) may hold.
# "audio_only" and "accepted" are this worker's two additions:
#   - "audio_only": the chunk was too short for a transcript comparison to
#     mean anything (see min_tokens_for_coverage above) and cleared instead
#     on audio presence/plausibility. Kept distinct from "ok" on purpose --
#     "ok" must always mean "the transcript matched", never "we didn't
#     check".
#   - "accepted": the chunk ended needs_human, and a human recorded, in
#     work/<slug>/qc-accept.json, that the audio is correct anyway (see
#     accept_chunk() / load_accept_records() below).
RESOLUTIONS = ("ok", "re_rendered", "split", "needs_human", "audio_only", "accepted")

# CONTRACT.md 8.2's two guards, mirrored here (a defect fix: this stage used
# to detect a fatal disk error and then re-raise it as a bare OSError, which
# cli.py's `_run_stage` does not catch -- a full disk mid-QC-run, plausible
# on this Mac since QC writes one JSON file per chunk across 2,000+ chunks,
# ended the run in a raw traceback instead of a clean abort). Both stages now
# obey the identical contract: never raise, always return `aborted` +
# `abort_reason` in the summary, so cli.py's already-stage-generic abort
# handling (CONTRACT.md 8.2, mirrored in cli.py's _finish/_report_abort)
# covers this stage for free.
DEFAULT_MAX_CONSECUTIVE_FAILURES = 5

# --------------------------------------------------------------------------- dialect equivalences
#
# CONTRACT.md 9.1 says the comparison must measure coverage and order, not
# orthography, and that normalization must make dialect never false-flag. A
# real Phase 2 run on Chapter IV found two systemic sources of false flags
# that fuzzy Levenshtein matching alone does not cover:
#
#  (a) Kokoro renders the protagonist's invented name "Gyko" with a soft g
#      (English's own "gym"/"gyro"/"gypsy" rule: G before Y is soft), and
#      whisper faithfully transcribes what it hears: "jaipo". That is a
#      phonetic match, not an orthographic one -- see phonetic_key() below.
#  (b) whisper normalizes this book's Dublin dialect to standard English
#      spelling: "ye" -> "you", "yer" -> "your", "an'" -> "and". These are
#      different *words*, not misheard sounds, so no phonetic algorithm
#      should be expected to unify them -- they need an explicit map. This
#      table is that map, seeded from this book's measured dialect (NOTES.md
#      "The text hazards" + the Phase 2 QC run): "ye" 360x, "an'" 258x,
#      "yer" 137x.
#
# THE DEFECT THIS SECTION FIXES: the table used to be a one-to-one map
# ("o" -> "of"), applied by REWRITING the source text before comparison
# (qc_normalize used to do this at step 6). That is lossy: real chunk
# ch07/0147 is `"O Lord! What is it, Dan?"`, where `O` is the vocative --
# it means "oh", not "of". The old rewrite turned the source into
# "of lord what is it dan", which whisper's "oh lord..." can never match --
# false-flagged, needs_human, on audio that was completely correct. One
# token legitimately has two meanings ("o" is both the elided "of" in
# "no way o' findin'" AND the vocative "oh" in "O Lord!"), so a single
# destructive rewrite can never be right for both at once.
#
# THE FIX: nothing is rewritten. Instead, DEFAULT_EQUIVALENCES lists, for
# each dialect spelling, every standard phrase it can mean; build_equiva-
# lence_index() below unions each entry into an equivalence CLASS via
# union-find, so "o" ends up in ONE class together with both "of" and "oh".
# tokens_equivalent() -- the single predicate coverage() and wer() both
# call -- treats any two members of the same class as the same token, at
# MATCH time, without ever deciding in advance which of the two meanings a
# given "o" must be. Nothing is destroyed, so neither meaning can ever
# clobber the other.
#
# A representation of explicit classes (not a token->token dict) is what
# makes this possible: a dict can only ever point "o" at one canonical
# spelling, but a class has no such limit -- "o", "of", and "oh" just are
# the same class, and asking "are token equivalent" is symmetric and
# transitive the way English speakers' own sense of synonymy is.
#
# Applied only at token-comparison time (never as a text rewrite), so it
# can only ever make two different real words compare equal -- it can
# never make two genuinely different transcripts compare equal, because
# both sides are compared as themselves, exactly as written and as heard.
#
# "me" -> "my" is deliberately NOT here. "yer"/"ye"/"an'" have no standard-
# English meaning that collides with the dialect one; "me" does -- it is an
# ordinary, extremely common object pronoun ("give me the book") wherever
# this book uses standard English, so classing it with "my" would corrupt
# every real occurrence, not just the dialect one. Left out; a future book
# with an unambiguous "me"->"my" dialect can add it through qc-config.json's
# "equivalences" override (resolve_equivalences() below) instead.
#
# Each value is a TUPLE of one or more target phrases the key is
# equivalent to -- almost always one, "o" being the deliberate exception
# that motivated this whole redesign. A target phrase may itself be more
# than one word ("let me", "do you"); _member_of() below turns that into a
# multi-token class member, the same shape the old rewrite produced by
# literally inserting two tokens, but built directly as data now instead
# of as a text mutation.
DEFAULT_EQUIVALENCES: dict[str, tuple[str, ...]] = {
    "ye": ("you",),
    "yer": ("your",),
    "yeh": ("you",),
    "an": ("and",),
    # The chunk ch07/0147 fix: "o" means BOTH "of" ("no way o' findin'") and
    # the vocative "oh" ("O Lord!"). Both targets union into one class.
    "o": ("of", "oh"),
    # "Dh'ye" ("d'ye", "do you") loses its apostrophe in qc_normalize step 3
    # and concatenates to "dhye" (no space was ever there to collapse to).
    # Not present in this book's first 19 chapters (checked); kept per the
    # brief's seed list for whichever chapter or future book uses it.
    "dhye": ("do you",),
    "shure": ("sure",),
    # Added for the real Chapter I QC run (the wer/coverage-drift fix): each
    # entry below was checked against phonetic_key() first (see that
    # function's module comment) and is here only because the phonetic rule
    # does NOT already unify it -- ordinary -in'/-ing dialect endings
    # ("nothin"/"nothing", "somethin"/"something", "drivin"/"driven" or
    # "driving", "watchin"/"watching", "findin"/"finding") and "ould"/"old"
    # all already collide under phonetic_key() and are deliberately NOT
    # duplicated here.
    #
    # "lemme"/"atall" hold a MULTI-TOKEN target ("let me", "at all"):
    # _member_of() turns each into a 2-token class member, so the class
    # {"lemme", ("let","me")} lines up "lemme" with whisper's own two-token
    # transcription directly, at match time, via EquivalenceIndex.phrase_
    # matches_token() -- no text rewrite and no separate join-handling
    # needed in wer()/coverage() for these two specific words.
    "lemme": ("let me",),
    "atall": ("at all",),
    # "t'" (elided "to": "somethin' t' eat") loses its apostrophe in step 3
    # and becomes the single token "t". phonetic_key() is guarded to tokens
    # of 2+ letters (CONTRACT.md 9.1), so a lone "t" can never reach the
    # phonetic fallback; this is the only way to join it to "to".
    "t": ("to",),
    "mesel": ("myself",),
    "messel": ("myself",),
    "fellah": ("fella",),
}


def resolve_equivalences(config: dict | None) -> dict[str, tuple[str, ...]]:
    """Return the dialect equivalence table: for each dialect spelling, the
    tuple of one or more standard phrases it is equivalent to. The module
    defaults, with a book's qc-config.json ``equivalences`` object layered
    on top (each key added or wholesale REPLACED, never merged word-by-word
    with that key's default) -- so the next book can bring its own dialect,
    or override one entry (e.g. disagree with "o"'s two targets), without
    editing this file.

    A qc-config.json value may be a single string ("sure"), a multi-word
    phrase ("let me"), or a JSON array of either (`["of", "oh"]`) for a
    dialect spelling that legitimately carries more than one standard
    meaning -- see DEFAULT_EQUIVALENCES's "o" entry for why that shape
    exists. Keys and values are lower-cased so they line up with
    qc_normalize's already-lower-cased tokens regardless of how the config
    file capitalizes them.
    """
    table = dict(DEFAULT_EQUIVALENCES)
    if isinstance(config, dict):
        extra = config.get("equivalences")
        if isinstance(extra, dict):
            for key, value in extra.items():
                if isinstance(value, (list, tuple)):
                    table[str(key).lower()] = tuple(str(v).lower() for v in value)
                else:
                    table[str(key).lower()] = (str(value).lower(),)
    return table


# A class member is either a bare token ("sure") or a tuple of tokens for a
# multi-word phrase (("let", "me")) -- see _member_of().
_Member = str | tuple[str, ...]


def _member_of(phrase: str) -> _Member:
    """Turn one equivalence-table entry (a key or a target phrase) into a
    class member: a bare token for a single word, or a tuple of tokens for
    a multi-word phrase ("let me" -> ("let", "me")) -- built directly as
    data, where the old design built the same shape by rewriting text."""
    parts = phrase.split()
    return parts[0] if len(parts) == 1 else tuple(parts)


class EquivalenceIndex:
    """A resolved, query-ready view of the equivalence classes, built by
    build_equivalence_index(). tokens_equivalent() and _run_equivalent()
    are the only two things that read this -- so coverage() and wer(),
    which both bottom out in those two functions, always see the identical
    classes (see tokens_equivalent()'s docstring: "the single shared
    predicate" this whole fix depends on).
    """

    __slots__ = ("token_class", "phrase_class")

    def __init__(self, token_class: dict[str, int], phrase_class: dict[tuple[str, ...], int]):
        self.token_class = token_class
        self.phrase_class = phrase_class

    def same_class(self, a: str, b: str) -> bool:
        """True when two single tokens are members of the same class."""
        cls_a = self.token_class.get(a)
        return cls_a is not None and cls_a == self.token_class.get(b)

    def phrase_matches_token(self, phrase: tuple[str, ...], token: str) -> bool:
        """True when a multi-token phrase and a single token are members of
        the same class -- e.g. ("let", "me") and "lemme"."""
        cls_phrase = self.phrase_class.get(phrase)
        return cls_phrase is not None and cls_phrase == self.token_class.get(token)


def build_equivalence_index(table: dict[str, tuple[str, ...]]) -> EquivalenceIndex:
    """Union `table` -- dialect spelling -> one or more standard phrases it
    means -- into equivalence CLASSES, via union-find over every key and
    every one of its targets.

    Each key unions with EVERY one of its targets, so two targets of the
    same key end up in the same class as each other too: this is what
    gives "o" a three-way class with both "of" and "oh" (see this module's
    "dialect equivalences" comment for the real defect this fixes). Any two
    tokens NOT linked by a table entry, directly or transitively, land in
    different classes and stay unequal -- e.g. "of" and "sure" never
    collide just because both happen to appear somewhere in the table.
    """
    parent: dict[_Member, _Member] = {}

    def find(x: _Member) -> _Member:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: _Member, b: _Member) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for key, targets in table.items():
        key_member = _member_of(key)
        for target in targets:
            union(key_member, _member_of(target))

    groups: dict[_Member, list[_Member]] = {}
    for member in parent:
        groups.setdefault(find(member), []).append(member)

    token_class: dict[str, int] = {}
    phrase_class: dict[tuple[str, ...], int] = {}
    for class_id, members in enumerate(groups.values()):
        for member in members:
            if isinstance(member, tuple):
                phrase_class[member] = class_id
            else:
                token_class[member] = class_id

    return EquivalenceIndex(token_class, phrase_class)


# The module-default index, built once at import time from
# DEFAULT_EQUIVALENCES -- what every caller gets when it passes no
# `equivalences` argument at all (tests calling tokens_equivalent()/
# coverage()/wer() directly, mainly). _score() builds and passes its own,
# resolved from qc-config.json, for every real run.
_DEFAULT_EQUIVALENCE_INDEX = build_equivalence_index(DEFAULT_EQUIVALENCES)

# How often run() prints a progress line, in chunks. 900 chunks means one line
# per chunk floods a supervised log; print every N instead.
PROGRESS_EVERY = 20


def _load_or_write_qc_config(ctx, equivalences_seed: dict | None = None) -> dict:
    """Read qc-config.json. Write the defaults and continue when it is absent.

    Writing the defaults keeps the file out of git ownership disputes -- the
    file lives under work/<slug>/, which nobody commits.

    CONTRACT.md 9.2 (the per-book file): when the file is written for the
    first time, `equivalences_seed` -- the book config's `qc.equivalences`
    (CONTRACT.md 4.1) -- seeds the new file's "equivalences" key, so a new
    book's dialect or foreign-term table starts in qc-config.json rather
    than as a code constant. `run()` below resolves this from its optional
    `book_config` argument and passes it down; this function takes it as a
    plain dict rather than loading the book config itself, so qc.py never
    has to import extract.py (Worker A's file) to get it -- ownership stays
    clean (CONTRACT.md 16). Empty/falsy seeds add nothing, so the written
    file is byte-for-byte the old `dict(DEFAULT_QC_CONFIG)` in that case --
    see test_missing_qc_config_is_written_with_defaults.

    **A file that already exists is returned completely untouched, no
    matter what seed is passed.** CONTRACT.md 9.2: "the stage never
    overwrites a file that exists, because a human may have tuned it." That
    is also what keeps a shipped book's qc-config.json (e.g.
    work/book-a/qc-config.json) byte-identical across every future
    run of this stage, even once callers start passing book_config.
    """
    path = ctx.qc_config_path
    data = read_json(path)
    if isinstance(data, dict):
        return data
    data = dict(DEFAULT_QC_CONFIG)
    if equivalences_seed:
        data["equivalences"] = dict(equivalences_seed)
    write_json(path, data)
    return data


# --------------------------------------------------------------------------- comparison

# CONTRACT.md 9.1 step 1 / 6.1: expand every number to words, with num2words,
# before anything else touches the text -- this step needs the digits still
# intact. Whisper writes "1920" and "15th"; normalize.py's own expand_numbers
# switch may or may not have already turned the source's numbers into words
# (CONTRACT.md 6.1's 2026-08-15 course correction: it defaults to false), but
# the transcript side never does, so without this step the two sides can
# never match.
#
# Migrated 2026-08-15 (the overlord's course correction) to import Worker A's
# one shared implementation -- abpipe.normalize.NUMBER_RE / expand_number()
# -- rather than keeping a second, private copy that could silently drift
# from it (CONTRACT.md 6.1: "the rule must give the same words ... or every
# number in the book false-flags"). This is a plain top-level import, not
# this module's usual lazy in-function idiom (_get_engine(), the
# abpipe.render imports inside _apply_pronunciations() and friends): those
# exist to defer a HEAVY or ML-backed dependency (mlx_whisper, the render
# engines) past collection time, and/or because render.py sits the other
# side of a real potential import cycle. Neither applies here --
# normalize.py's own imports are just `re`, `num2words`, and abpipe.meta,
# and normalize.py imports nothing from qc.py, so there is no cycle to
# avoid. A lazy import would also leave `qc._NUMBER_RE`/`qc._expand_number`
# unpopulated as module attributes until the first qc_normalize() call --
# tests/test_normalize.py (Worker A's file) reads both names directly, so a
# lazy population would make that cross-file check depend on test
# collection/execution order. Keeping `_NUMBER_RE`/`_expand_number` as the
# names here (now aliases onto the shared implementation, not a
# reimplementation) is what keeps every existing reference to them, in
# either module, valid without editing tests/test_normalize.py, which is
# Worker A's file, not this worker's to touch. The import itself lives in
# the top-of-file import block, alongside the rest of this module's
# unconditional imports, since it is a plain top-level import and not
# actually deferred.
_NUMBER_RE = normalize.NUMBER_RE
_expand_number = normalize.expand_number


def qc_normalize(
    text: str,
    pronunciations: dict[str, str] | None = None,
) -> str:
    """Normalize text for comparison, per CONTRACT.md 9.1 steps 1-4, plus a
    pronunciation-map step.

    0. Apply `pronunciations` (source side only -- see qc_normalize's
       callers in _score()), CONTRACT.md 9.6: book.json's optional per-book
       map of a written word to how the engine was actually told to say it,
       e.g. {"Gyko": "Gikko"}. Empty by default. This runs FIRST, on the raw
       text, before case-folding or punctuation-stripping -- it shares
       abpipe.render.apply_pronunciations (see _apply_pronunciations below),
       which matches a whole word with regex `\\b` at both ends and is
       case-sensitive. Running it here, not on the already-stripped token
       list, is what makes the possessive work: "Gyko's" still has its
       apostrophe (and so still has a `\\b` right after "Gyko") at this
       point in the pipeline; by step 3 the apostrophe is long gone and
       "Gyko's" has become the single token "gykos", which no whole-word
       match could ever find. Deliberately not applied to the transcript
       side: it exists to correct the *source*, not to give the transcript
       a second, looser pass.
    1. Expand every number to words (digits must still be intact, so this
       runs before any case-folding).
    2. Lower case.
    3. Remove every character that is not a letter, a digit, or a space.
       Unicode-aware via str.isalnum(), so an accented letter (e, oe-ligature,
       ash, ...) survives while the dialect apostrophe ("an'") and every
       other punctuation mark are stripped. That deliberately makes "an'" ==
       "an".
    4. Collapse a run of spaces.

    THE DIALECT EQUIVALENCE TABLE IS DELIBERATELY NOT APPLIED HERE ANY
    MORE. It used to be step 5, rewriting text ("o" -> "of") before either
    side was tokenized -- see this module's "dialect equivalences" comment
    for the real chunk (ch07/0147, "O Lord!") that rewrite broke. The table
    is now consulted at MATCH time instead, token by token, by
    tokens_equivalent() (via the `equivalences` argument coverage()/wer()
    thread down to it) -- so qc_normalize() only ever does orthography-
    neutral cleanup, never a lossy word substitution. Both sides still run
    through this exact same function, so nothing about "compare the two
    sides the same way" changes.

    Step 5 of CONTRACT.md 9.1 (collapse a degenerate repeat) is not here
    either: it needs max_token_repeat from qc-config.json, which this
    function does not take, so it runs on the token list instead -- see
    collapse_degenerate_repeats().
    """
    if pronunciations:
        text = _apply_pronunciations(text, pronunciations)

    expanded = _NUMBER_RE.sub(_expand_number, text)
    lowered = expanded.lower()
    kept = "".join(ch for ch in lowered if ch.isalnum() or ch.isspace())
    return " ".join(kept.split())


def _tokens(normalized: str) -> list[str]:
    return normalized.split()


def collapse_degenerate_repeats(tokens: list[str], max_repeat: int = 2) -> list[str]:
    """Cap a run of the same token at max_repeat consecutive occurrences.

    CONTRACT.md 9.1 step 5 / 9.5: whisper occasionally falls into a decode
    failure loop that repeats one token dozens or hundreds of times in a row
    (a real render of this book produced "stir" about 200 times at the end
    of a transcript). That is a transcription artifact, not real content;
    uncapped it wrecks the WER of an otherwise-perfect chunk and can trip the
    remediation ladder on good audio. This is belt and braces on top of
    condition_on_previous_text=False (9.5), which prevents most of these
    loops outright.
    """
    out: list[str] = []
    run_token = None
    run_len = 0
    for tok in tokens:
        if tok == run_token:
            run_len += 1
        else:
            run_token = tok
            run_len = 1
        if run_len <= max_repeat:
            out.append(tok)
    return out


def _edit_distance(a, b) -> int:
    """Generic Levenshtein edit distance over any two indexable sequences
    (works for a pair of strings, char by char, or a pair of token lists)."""
    n, m = len(a), len(b)
    if n == 0:
        return m
    if m == 0:
        return n
    prev = list(range(m + 1))
    for i in range(1, n + 1):
        cur = [i] + [0] * m
        ai = a[i - 1]
        for j in range(1, m + 1):
            cost = 0 if ai == b[j - 1] else 1
            cur[j] = min(
                prev[j] + 1,  # deletion
                cur[j - 1] + 1,  # insertion
                prev[j - 1] + cost,  # substitution / match
            )
        prev = cur
    return prev[m]


def wer(
    source_tokens: list[str],
    hyp_tokens: list[str],
    token_similarity_min: float = DEFAULT_QC_CONFIG["token_similarity_min"],
    equivalences: "EquivalenceIndex | None" = None,
) -> float:
    """Return the word error rate: Levenshtein distance / len(source_tokens).

    THE DEFECT THIS FIXES: this used to run plain `_edit_distance()`, which
    compares tokens with `==`. `coverage()` (below) has always compared
    tokens with a much richer relation -- exact, then the dialect
    equivalence classes, then fuzzy Levenshtein at `token_similarity_min`,
    then the phonetic consonant-skeleton match, plus a rule that joins
    adjacent hyp tokens. `wer`'s plain `==` counted every one of those
    forgiven differences as a substitution, so the two numbers disagreed
    about the same audio: a real Chapter I run scored coverage 1.000 and wer
    0.600 on the same chunk (0120: "Nothin' atall" / "somethin' t' eat"
    heard correctly, spelled differently). `wer_max` was the gate that
    failed, on audio that was correct.

    The fix: `_edit_distance_tokens()` below runs the identical
    exact/class/fuzzy/phonetic relation `coverage()` uses -- `tokens_equivalent()`
    is the one function both call, so they can never drift apart again --
    as its match/substitution cost, plus the same adjacent-hyp-token join
    `coverage()` allows (CONTRACT.md 9.1's compound-split rule), so a source
    token that matches two joined hyp tokens costs 0, not "1 substitution +
    1 insertion".

    `equivalences` is the resolved EquivalenceIndex (see
    build_equivalence_index()) tokens_equivalent() consults for the dialect
    table; defaults to the module's DEFAULT_EQUIVALENCES index when not
    given, so a bare `wer(src, hyp)` call still gets the dialect fix.
    `_score()` passes the book's resolved index explicitly.

    Guarded against division by zero: an empty source scores 0.0 when the
    hypothesis is also empty (nothing to get wrong), else 1.0 (total
    mismatch, since there is no source to divide by).
    """
    n = len(source_tokens)
    if n == 0:
        return 0.0 if not hyp_tokens else 1.0
    return _edit_distance_tokens(source_tokens, hyp_tokens, token_similarity_min, equivalences) / n


def _token_similarity(a: str, b: str) -> float:
    """Return 1 - levenshtein(a, b) / max(len(a), len(b)), per CONTRACT.md 9.1."""
    if not a and not b:
        return 1.0
    return 1.0 - _edit_distance(a, b) / max(len(a), len(b))


# --------------------------------------------------------------------------- phonetic fallback
#
# Job 1's other false-flag source: Kokoro renders this book's invented name
# "Gyko" with a soft g (English already has this rule -- "gym", "gyro",
# "gypsy" -- G before Y is soft), and whisper faithfully writes what it
# hears: "jaipo". Levenshtein similarity of "gyko"/"jaipo" is ~0.4, nowhere
# near token_similarity_min (0.85); this is a phonetic match, not an
# orthographic one, and "Gyko" occurs 439 times in the book, so left
# unfixed this alone would flag hundreds of chunks.
#
# Soundex is not enough here: it is anchored on the first letter of the raw
# spelling (G100 vs J100 -- still different), so it cannot see that a soft G
# and a J are the same sound. What follows is a small, purpose-built
# metaphone-style consonant-skeleton encoder -- not a port of the published
# Double Metaphone algorithm, but built the same way it is: strip silent
# letters, collapse digraphs to the sound they represent, apply the
# soft-c/soft-g rule, and drop vowels other than a leading one. It is
# intentionally narrow: only the rules this book's dialect and this book's
# invented name actually exercise, each one measured against a real
# whisper transcript (see phonetic_key()'s docstring and test_qc.py).

_VOWELS = frozenset("AEIOU")

_INITIAL_SILENT_PAIRS = (
    ("KN", "N"),
    ("GN", "N"),
    ("PN", "N"),
    ("WR", "R"),
    ("PS", "S"),
)


def _reduce_final_ng(word: str) -> str:
    """Drop a word-final silent G after N.

    English's "-ing"/"-ang"/"-ong" endings are one nasal consonant sound
    (/ŋ/), not two -- which is exactly why this book's dropped-g dialect
    spelling ("lookin", "cryin", "trainin") and the standard spelling
    ("looking", "crying", "training") are the same word said the same way.
    Restricted to the literal end of the word, so a mid-word NG where the g
    is a real, separately pronounced stop ("angry", "England") is untouched.
    """
    if word.endswith("NG") and len(word) > 2:
        return word[:-1]
    return word


def _reduce_final_stop_cluster(word: str) -> str:
    """Drop a lone word-final T or D that closes a consonant cluster.

    This is final consonant-cluster simplification ("t/d-deletion"): "hist"
    said as "hiss", "and" said as "an" -- a well documented, extremely
    common feature of casual and dialect English, and exactly the register
    of this book. Guarded to a single trailing T/D preceded by a *different*
    consonant, so a doubled letter ("add") or a T/D straight after a vowel
    ("cat", "sad") is untouched.
    """
    if len(word) >= 3 and word[-1] in "TD" and word[-2] not in _VOWELS and word[-2] != word[-1]:
        return word[:-1]
    return word


def phonetic_key(word: str) -> str:
    """Return a coarse phonetic key for one alphabetic token, or "" for
    empty/non-alphabetic input.

    Verified against every real src/hyp pair CONTRACT.md 9.1 and the Phase 2
    QC run measured on this book: gyko/jaipo, hist/his, cryin/crying (also
    lookin/looking, trainin/training -- same -in/-ing shape). Deliberately
    does NOT try to unify "ye"/"you", "yer"/"your", or "an"/"and": those are
    different words, not the same sound spelled two ways, and belong to
    DEFAULT_EQUIVALENCES instead.
    """
    w = "".join(ch for ch in word.upper() if ch.isalpha())
    if not w:
        return ""

    for pair, repl in _INITIAL_SILENT_PAIRS:
        if w.startswith(pair):
            w = repl + w[len(pair):]
            break
    if w.startswith("X"):
        w = "S" + w[1:]

    w = _reduce_final_ng(w)
    w = _reduce_final_stop_cluster(w)

    out: list[str] = []
    n = len(w)
    i = 0
    first = True
    while i < n:
        ch = w[i]
        nxt = w[i + 1] if i + 1 < n else ""

        if ch in _VOWELS:
            if first:
                out.append(ch)
            i += 1
            first = False
            continue

        # digraphs whose sound is not "the two letters separately"
        if ch == "P" and nxt == "H":
            out.append("F")
            i += 2
            first = False
            continue
        if ch == "C" and nxt == "K":
            i += 1  # silent C; the K two lines down emits the sound
            first = False
            continue
        if ch == "S" and nxt == "H":
            out.append("S")
            i += 2
            first = False
            continue
        if ch == "C" and nxt == "H":
            out.append("K")
            i += 2
            first = False
            continue
        if ch == "T" and nxt == "H":
            out.append("T")
            i += 2
            first = False
            continue
        if ch == "G" and nxt == "H":
            if first:  # "ghost"-style hard g; mid/end-word GH is silent
                out.append("K")
            i += 2
            first = False
            continue

        if ch == "C":
            out.append("S" if nxt in ("E", "I", "Y") else "K")
        elif ch == "G":
            out.append("J" if nxt in ("E", "I", "Y") else "G")  # the gyko/jaipo rule
        elif ch == "J":
            out.append("J")
        elif ch == "Q":
            out.append("K")
        elif ch == "X":
            out.append("K")
            out.append("S")
        elif ch == "Z":
            out.append("S")
        elif ch == "V":
            out.append("F")
        elif ch == "W":
            if nxt in _VOWELS:
                out.append("W")
            # else silent ("sword", "who")
        elif ch == "Y":
            if first:
                out.append("Y")
            # else treated as a vowel sound, not a consonant: silent
        elif ch == "H":
            if first or w[i - 1] in _VOWELS:
                out.append("H")
            # else silent (an H right after a consonant is not pronounced)
        else:
            out.append(ch)

        if len(out) >= 2 and out[-1] == out[-2]:  # collapse a doubled code
            out.pop()

        i += 1
        first = False

    return "".join(out)


def _phonetic_equivalent(a: str, b: str) -> bool:
    """True when two tokens' phonetic keys match and are non-trivial.

    Guarded to tokens of length >= 2: a phonetic key from a single letter
    carries almost no information (many unrelated one-letter tokens reduce
    to the same key), so this only ever fires for something with an actual
    syllable to compare.
    """
    if len(a) < 2 or len(b) < 2:
        return False
    ka, kb = phonetic_key(a), phonetic_key(b)
    return bool(ka) and ka == kb


def tokens_equivalent(
    src: str,
    hyp: str,
    token_similarity_min: float = DEFAULT_QC_CONFIG["token_similarity_min"],
    equivalences: "EquivalenceIndex | None" = None,
) -> bool:
    """THE single definition of "same token" for this stage. `coverage()`
    and `wer()` both call this, and nothing else defines a competing notion
    of "same token" -- that is the fix for the wer/coverage disagreement
    (see wer()'s docstring): the two numbers can no longer describe two
    different relations, because there is only one relation now.

    True when:
      1. `src == hyp` (exact);
      2. `src` and `hyp` are members of the same dialect equivalence CLASS
         (build_equivalence_index(); "o" and "oh" both being classed with
         "of" is the fix for real chunk ch07/0147 -- see this module's
         "dialect equivalences" comment). Checked BEFORE the fuzzy/phonetic
         tests below because a table entry is an assertion of fact ("this
         dialect spelling means this word"), not a graded guess -- it
         should not be at the mercy of `token_similarity_min` or of
         phonetic_key()'s narrower rules;
      3. `1 - levenshtein(src, hyp) / max(len)` is `token_similarity_min` or
         more (fuzzy orthographic match: "grey"/"gray", "McAllister"/
         "McAlister");
      4. the two tokens' phonetic consonant skeletons match (phonetic_key(),
         guarded there to tokens of 2+ letters): "gyko"/"jaipo",
         "watchin"/"watching".

    `hyp` may be a single hyp token, or the concatenation of a RUN of
    adjacent hyp tokens -- that is the caller's job (see _run_equivalent(),
    _match_flags(), and _edit_distance_tokens()'s join transitions below),
    and is what makes CONTRACT.md 9.1's compound-split rule ("coalheaver" ->
    "coal" + "heaver", or "I. R. B." -> "irb") a call into this same
    function rather than a fifth relation. The mirror direction (a run of
    SOURCE tokens joined against one HYP token, e.g. "ye an'" -> "yen") is
    the same trick with the arguments swapped -- see _run_equivalent().

    `equivalences` defaults to the module's DEFAULT_EQUIVALENCES index
    (_DEFAULT_EQUIVALENCE_INDEX) when not given, so a bare
    `tokens_equivalent(a, b)` call still gets the dialect fix; `_score()`
    passes the book's resolved index (from qc-config.json) explicitly.
    """
    if src == hyp:
        return True
    if not src or not hyp:
        return False
    eq = _DEFAULT_EQUIVALENCE_INDEX if equivalences is None else equivalences
    if eq.same_class(src, hyp):
        return True
    if _token_similarity(src, hyp) >= token_similarity_min:
        return True
    return _phonetic_equivalent(src, hyp)


# Overlord addition (real ch08/0140, "I. R. B." -> whisper's "irb"): the
# original compound rule only ever joined a PAIR of tokens. An initialism
# collapses THREE (or more) source tokens into one hyp token, so the join
# is generalized from a fixed pair to a run of up to this many adjacent
# tokens on either side. 4 gives one token of headroom over the worst case
# measured in this book ("I", "R", "B").
_MAX_JOIN_RUN = 4


def _run_equivalent(
    run: tuple[str, ...],
    other: str,
    token_similarity_min: float = DEFAULT_QC_CONFIG["token_similarity_min"],
    equivalences: "EquivalenceIndex | None" = None,
) -> bool:
    """True when a RUN of 2+ adjacent tokens, joined, is equivalent to a
    single token `other`. Generalizes the old two-token-only join to a run
    of up to `_MAX_JOIN_RUN` tokens (CONTRACT.md 9.1's compound-split rule,
    plus the initialism case, "i"+"r"+"b" -> "irb", that rule now also
    covers).

    THE ONE JOIN PREDICATE both directions of CONTRACT.md 9.1's compound
    rule, and both coverage() and wer(), call -- so a merge or a split is
    only ever forgiven by the SAME underlying relation, never by a second,
    competing notion of "close enough":

      - whisper SPLITS a compound word or an initialism: `run` is 2+
        adjacent HYP tokens, `other` is one SOURCE token ("coalheaver" ->
        "coal"+"heaver").
      - whisper MERGES several words into one (real Chapter I failure,
        chunk ch01/0081; real Chapter VIII, ch08/0140): `run` is 2+ adjacent
        SOURCE tokens, `other` is one HYP token. The engine elides "ye an'"
        into one spoken syllable and whisper transcribes it as the single
        token "yen" -- authentic Dublin elision, not a transcription error;
        "I. R. B." collapses to the single token "irb" the same way an
        initialism is naturally read aloud.

    Checked two ways:
      1. class-based: `run`, as a tuple, is a registered multi-token member
         of the same equivalence class as `other` (EquivalenceIndex.
         phrase_matches_token()) -- this is what joins "lemme" to
         ("let","me") and "dhye" to ("do","you") without any fuzzy
         concatenation guesswork, since those pairs are not orthographically
         close as raw concatenations ("letme" vs "lemme" is only 0.8
         similar, under the default 0.85 threshold).
      2. fallback: tokens_equivalent("".join(run), other, ...) -- no
         separate threshold, no separate table beyond what tokens_
         equivalent() already grants (exact concatenation match: "coal" +
         "heaver" == "coalheaver"; "i"+"r"+"b" == "irb"; or the phonetic
         match that joins "ye"+"an'" to "yen"). That is what keeps a merge
         from being forgiven any more easily than an ordinary single-token
         match already is -- see _match_flags()'s "the quick brown fox"/
         "the fox" and "hat cat"/"bat" guards for what this must NOT
         forgive.
    """
    eq = _DEFAULT_EQUIVALENCE_INDEX if equivalences is None else equivalences
    if eq.phrase_matches_token(run, other):
        return True
    return tokens_equivalent("".join(run), other, token_similarity_min, eq)


def _match_flags(
    src_slice: list[str],
    hyp_slice: list[str],
    token_similarity_min: float = DEFAULT_QC_CONFIG["token_similarity_min"],
    equivalences: "EquivalenceIndex | None" = None,
) -> list[bool]:
    """Return, for each source token in src_slice, whether it is "matched"
    against hyp_slice -- coverage()'s per-opcode-block worker.

    Three ways a source token at index i can be matched:
      1. tokens_equivalent() to a single hyp token in hyp_slice (exact,
         class, fuzzy, or phonetic -- see tokens_equivalent()'s docstring);
      2. _run_equivalent() to the join of a RUN of up to `_MAX_JOIN_RUN`
         adjacent hyp tokens in hyp_slice (whisper splits a compound or an
         initialism: "coalheaver" -> "coal heaver") -- CONTRACT.md 9.1's
         compound rule, generalized from a fixed pair to a run;
      3. together with a run of its immediate NEIGHBOURS (up to
         `_MAX_JOIN_RUN` - 1 more, in either direction), the join of that
         run of source tokens is _run_equivalent() to a single hyp token
         (the mirror: whisper merges several words into one, "ye an'" ->
         "yen", or "I R B" -> "irb"). Every source token in the run is
         marked matched -- CONTRACT.md 9.1 requires a merge that IS
         phonetically/orthographically/class justified to cost the run
         nothing, same as a split costs the source token nothing.

    Scoped to one SequenceMatcher opcode block, same as the pre-fix
    single-direction version was -- that scoping is what keeps an
    unrelated, non-adjacent run from ever being tried against an unrelated
    hyp token, and is why "the quick brown fox" against "the fox" still
    fails: "quick" and "brown" fall in a block whose hyp_slice is empty (a
    pure deletion), so direction 3 has no hyp token to test against at all,
    and they stay unmatched.

    Direction 3 does not require that direction 1/2 have failed first for a
    given index -- an index already matched individually can still take
    part in a run (harmless: coverage only asks "matched at least once",
    and _edit_distance_tokens() is a proper monotonic alignment, so no hyp
    or source token is ever double-charged there). A run is skipped only
    when EVERY one of its tokens is already matched -- trying it could only
    relax further, never tighten, so there is nothing to gain.
    """
    n = len(src_slice)
    m = len(hyp_slice)
    flags = [False] * n

    # Directions 1 + 2: each source token against a single hyp token, or
    # against the join of a run of up to _MAX_JOIN_RUN adjacent hyp tokens.
    for i, src_tok in enumerate(src_slice):
        if any(tokens_equivalent(src_tok, htok, token_similarity_min, equivalences) for htok in hyp_slice):
            flags[i] = True
            continue
        for k in range(m):
            matched = False
            for run_len in range(2, min(_MAX_JOIN_RUN, m - k) + 1):
                if _run_equivalent(
                    tuple(hyp_slice[k:k + run_len]), src_tok, token_similarity_min, equivalences
                ):
                    matched = True
                    break
            if matched:
                flags[i] = True
                break

    # Direction 3: a run of up to _MAX_JOIN_RUN adjacent SOURCE tokens,
    # joined, matches a single hyp token.
    for start in range(n):
        for run_len in range(2, min(_MAX_JOIN_RUN, n - start) + 1):
            end = start + run_len
            if all(flags[start:end]):
                continue  # already matched individually; a run check can only relax further
            run = tuple(src_slice[start:end])
            if any(_run_equivalent(run, htok, token_similarity_min, equivalences) for htok in hyp_slice):
                for idx in range(start, end):
                    flags[idx] = True
                break  # this start already has a match; a longer run adds nothing more
    return flags


def coverage(
    source_tokens: list[str],
    hyp_tokens: list[str],
    token_similarity_min: float = DEFAULT_QC_CONFIG["token_similarity_min"],
    equivalences: "EquivalenceIndex | None" = None,
) -> float:
    """Return matched source tokens / total source tokens.

    Uses difflib.SequenceMatcher on the two token lists as the alignment. A
    source token inside an "equal" block matches outright; a source token
    inside a non-equal block is scored by _match_flags() -- matched when it
    is tokens_equivalent() (per CONTRACT.md 9.1) to a single hyp token in
    that block, to the join of a run of adjacent hyp tokens in that block
    (whisper splits a compound or an initialism), or when it and a run of
    adjacent source tokens together join to match a single hyp token in
    that block (the mirror: whisper merges several words into one). The
    exact-match case is the fuzzy-match case with token_similarity_min ==
    1.0, so this one function covers all of it.

    `equivalences` defaults to the module's DEFAULT_EQUIVALENCES index when
    not given; `_score()` passes the book's resolved index explicitly.
    """
    n = len(source_tokens)
    if n == 0:
        return 1.0
    matcher = difflib.SequenceMatcher(a=source_tokens, b=hyp_tokens, autojunk=False)
    matched = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            matched += i2 - i1
            continue
        hyp_slice = hyp_tokens[j1:j2]
        src_slice = source_tokens[i1:i2]
        matched += sum(_match_flags(src_slice, hyp_slice, token_similarity_min, equivalences))
    return matched / n


def _edit_distance_tokens(
    a: list[str],
    b: list[str],
    token_similarity_min: float = DEFAULT_QC_CONFIG["token_similarity_min"],
    equivalences: "EquivalenceIndex | None" = None,
) -> int:
    """Levenshtein distance over two token lists, using tokens_equivalent()
    -- the exact relation coverage() uses -- as the match/substitution cost
    predicate, instead of plain `==`.

    Same deletion/insertion/substitution recurrence as _edit_distance()
    above, plus two extra transitions, both routed through the single
    _run_equivalent() predicate coverage()'s _match_flags() also uses, so
    wer() can never forgive a merge or a split that coverage() would not:

      - a source token that is _run_equivalent() to a RUN of up to
        `_MAX_JOIN_RUN` hyp tokens ending at j costs 0 to "substitute", not
        the N-1 a plain edit distance would charge for "1 substitution
        against the first + (N-1) insertions of the rest". Whisper splits a
        compound or an initialism: "coalheaver" against "coal", "heaver";
        "irb" against "i", "r", "b".
      - the mirror: a RUN of up to `_MAX_JOIN_RUN` source tokens ending at
        i, together, are _run_equivalent() to the one hyp token at j --
        also cost 0, not the N-1 a plain edit distance would charge.
        Whisper merges several words into one (real Chapter I failure,
        ch01/0081: "ye an'" heard as "yen"; real Chapter VIII, ch08/0140:
        "I. R. B." heard as "irb").

    The second transition needs dp[i-L][j-1] for a run length L of 2 up to
    `_MAX_JOIN_RUN` -- L ROWS back, not just L columns back the way the
    first transition only needs the same row. `rows` keeps every row
    computed so far (row i-1 through row 0) rather than a fixed rolling
    window: chunk-scale token counts (tens, not thousands) make the O(n*m)
    memory this costs irrelevant next to the whisper pass that dominates
    this stage's real runtime. Each transition consumes exactly the tokens
    it names (a run of one side + 1 of the other) and nothing else, so the
    DP alignment stays monotonic: no source or hyp token is ever charged
    into two different transitions at once.
    """
    n, m = len(a), len(b)
    if n == 0:
        return m
    if m == 0:
        return n
    rows: list[list[int]] = [list(range(m + 1))]  # rows[0] is the dp row for i=0
    for i in range(1, n + 1):
        cur = [i] + [0] * m
        ai = a[i - 1]
        prev = rows[i - 1]
        for j in range(1, m + 1):
            cost = 0 if tokens_equivalent(ai, b[j - 1], token_similarity_min, equivalences) else 1
            best = min(
                prev[j] + 1,  # deletion
                cur[j - 1] + 1,  # insertion
                prev[j - 1] + cost,  # substitution / match
            )
            if cost:  # cost==0 already found the cheapest possible
                # 1 source token = a run of L hyp tokens, cost 0.
                for run_len in range(2, min(_MAX_JOIN_RUN, j) + 1):
                    if _run_equivalent(
                        tuple(b[j - run_len:j]), ai, token_similarity_min, equivalences
                    ):
                        best = min(best, prev[j - run_len])
                # a run of L source tokens = 1 hyp token, cost 0.
                for run_len in range(2, min(_MAX_JOIN_RUN, i) + 1):
                    if _run_equivalent(
                        tuple(a[i - run_len:i]), b[j - 1], token_similarity_min, equivalences
                    ):
                        best = min(best, rows[i - run_len][j - 1])
            cur[j] = best
        rows.append(cur)
    return rows[n][m]


# --------------------------------------------------------------------------- duration model (9.2, defect 1)
#
# THE DEFECT THIS FIXES: real chunk duration is roughly
# fixed_overhead + rate * chars, with 0.2-0.3s+ of leading/trailing silence
# per chunk (measured; CONTRACT.md 9.2). The old test compared
# duration/chars directly against the chapter's median duration/chars ratio
# -- a ratio-only, zero-intercept model. For a short chunk the fixed
# overhead dominates that ratio, inflating it far past anything the content
# length alone explains. A real incident: a 7-character chunk ("Gyko!",
# ch05/0002) rendered CORRECTLY in 1.43s, and whisper transcribed it
# correctly -- but 1.43s / 7 chars was 3.1x the chapter's median ratio, so
# the test flagged perfectly good audio as a "runaway" purely because it had
# no notion of fixed overhead. The very next chunk up (a 12-character
# chapter heading) measured 2.3x on a different chapter -- a near miss that
# would have tripped there too. The bias is systematic, not a one-off: it
# hits every short chunk, worst the shorter the chunk is.
#
# The fix models duration as an affine function of chars,
# duration ~= intercept + slope * chars, fit per chapter (CONTRACT.md 9.2
# already requires >= 5 chunks before the test runs at all) so the fixed
# overhead is estimated and subtracted out before the outlier comparison
# runs, instead of being smeared uniformly across a chars-only ratio.
#
# The fit is Theil-Sen: the slope is the median of every pairwise slope
# between two chunks with different chars, and the intercept is the median
# residual once that slope is fixed. Both steps use the median rather than
# the mean specifically because this is a robust regression, resistant to
# exactly the kind of single extreme outlier the test exists to find -- an
# ordinary least-squares fit would let that same outlier drag the fitted
# line toward itself and blunt the test's own detection power.
#
# Belt and braces: min_chars_for_duration_test (DEFAULT_QC_CONFIG) exempts a
# chunk below a minimum character count from the test outright, the same
# shape of guard min_tokens_for_wer already is for the wer test. This
# protects the smallest chunks even in a chapter whose fit happens to be
# poorly conditioned (e.g. very little length variation among its chunks).

_MIN_EXPECTED_DURATION_S = 0.05


def _fit_duration_model(pairs: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Return a robust (intercept, slope) affine fit of duration_s over
    chars for one chapter's (chars, duration_s) pairs, or None when there
    are no pairs at all.

    Theil-Sen (see this section's module comment): the slope is the median
    of every pairwise slope between two points with different chars values;
    the intercept is the median residual (duration - slope*chars) over
    every point.

    Falls back to a zero-intercept, ratio-only model -- the module's old
    behaviour -- when every pair shares the same chars value: a real
    chapter never does this (chunks are natural-language text of varying
    length), but a slope has no meaning without at least two distinct chars
    values to compare, so this is the one input shape a robust fit still
    has to degrade gracefully on instead of raising.
    """
    if not pairs:
        return None
    xs = [p[0] for p in pairs]
    if len(set(xs)) < 2:
        ratios = [d / c for c, d in pairs if c]
        if not ratios:
            return None
        return 0.0, statistics.median(ratios)

    n = len(pairs)
    slopes = [
        (pairs[j][1] - pairs[i][1]) / (pairs[j][0] - pairs[i][0])
        for i in range(n)
        for j in range(i + 1, n)
        if pairs[j][0] != pairs[i][0]
    ]
    slope = statistics.median(slopes)
    intercept = statistics.median(d - slope * c for c, d in pairs)
    return intercept, slope


def _expected_duration(duration_model: tuple[float, float], chars: int) -> float:
    """Return the fitted model's expected duration for a chunk of this many
    characters, floored well above zero so a degenerate fit (e.g. a
    negative intercept combined with a small chars count) can never produce
    an expected duration at or below zero -- which would make the outlier
    test fire on any positive-duration audio at all, good or bad."""
    intercept, slope = duration_model
    return max(intercept + slope * chars, _MIN_EXPECTED_DURATION_S)


def _score(
    source_text: str,
    transcript: str,
    config: dict,
    duration_model: tuple[float, float] | None,
    duration_s: float,
    chars: int,
    pronunciations: dict[str, str] | None = None,
    rms: float | None = None,
) -> dict:
    """Normalize, score, and flag one (source, transcript) pair.

    `duration_model` is the chapter's (intercept, slope) affine fit of
    duration_s over chars -- see _fit_duration_model() -- or None when the
    chapter is too small (< 5 chunks) for the fit to be meaningful, per
    CONTRACT.md 9.2.

    `pronunciations` is the optional per-book source-side map (book.json
    "pronunciations", empty by default -- see qc_normalize). It is applied
    to the source side only, never the transcript.

    `rms` is the chunk WAV's root-mean-square amplitude (see _wav_rms()),
    or None when the caller has none to give (most direct-call tests).
    Consulted only on the audio_only path below.
    """
    table = resolve_equivalences(config)
    equivalences = build_equivalence_index(table)
    source_norm = qc_normalize(source_text, pronunciations=pronunciations)
    transcript_norm = qc_normalize(transcript)
    max_repeat = config.get("max_token_repeat", DEFAULT_QC_CONFIG["max_token_repeat"])
    # Collapse on both sides, for symmetry with "the stage normalises both
    # sides with the same function" -- in practice only the hyp side ever
    # runs away, since a source chunk is capped at 350-450 chars.
    source_tokens = collapse_degenerate_repeats(_tokens(source_norm), max_repeat)
    hyp_tokens = collapse_degenerate_repeats(_tokens(transcript_norm), max_repeat)

    # Same token_similarity_min and equivalences index feed both metrics,
    # because tokens_equivalent() is the one relation both wer() and
    # coverage() compare against -- this is the fix itself, not just a
    # config threading detail (see wer()'s docstring).
    token_similarity_min = config.get("token_similarity_min", DEFAULT_QC_CONFIG["token_similarity_min"])
    w = wer(source_tokens, hyp_tokens, token_similarity_min, equivalences)
    cov = coverage(source_tokens, hyp_tokens, token_similarity_min, equivalences)

    # Overlord addition (real ch08/0128, `"Aha!"` -> whisper's invented
    # "oh thats not good", coverage 0.000): below this many source tokens, a
    # transcript comparison is not a meaningful test at all -- whisper has
    # no context on a one- or two-word chunk and simply makes things up.
    # `wer`/`coverage` are still computed and stored above (honest,
    # diagnostic numbers), but a chunk this short is graded on AUDIO
    # plausibility instead: not silent, and not wildly off the chapter's
    # expected duration for its length. THIS DOES NOT VERIFY THE WORDS SAID
    # ARE THE RIGHT WORDS -- only that something real, roughly the right
    # length, was rendered. See resolution "audio_only" (RESOLUTIONS above)
    # and this worker's report for what this does and does not check.
    min_tokens_for_coverage = config.get(
        "min_tokens_for_coverage", DEFAULT_QC_CONFIG["min_tokens_for_coverage"]
    )
    audio_only = len(source_tokens) < min_tokens_for_coverage

    flags: list[str] = []
    if audio_only:
        min_rms = config.get("min_rms_for_short_chunk", DEFAULT_QC_CONFIG["min_rms_for_short_chunk"])
        if rms is not None and rms < min_rms:
            flags.append("silent")
        if duration_model is not None:
            expected = _expected_duration(duration_model, chars)
            low_factor = config.get(
                "short_chunk_duration_factor_low", DEFAULT_QC_CONFIG["short_chunk_duration_factor_low"]
            )
            high_factor = config["duration_outlier_factor"]
            if not (low_factor * expected <= duration_s <= high_factor * expected):
                flags.append("duration")
        # A degenerate-repeat runaway is still caught: collapse_degenerate_
        # repeats() already ran on hyp_tokens above, unconditionally, before
        # this branch -- nothing extra to do here to "keep it active".
    else:
        if len(source_tokens) >= config["min_tokens_for_wer"] and w > config["wer_max"]:
            flags.append("wer")
        if cov < config["coverage_min"]:
            flags.append("coverage")
        min_chars = config.get(
            "min_chars_for_duration_test", DEFAULT_QC_CONFIG["min_chars_for_duration_test"]
        )
        if duration_model is not None and chars >= min_chars:
            expected = _expected_duration(duration_model, chars)
            if duration_s > config["duration_outlier_factor"] * expected:
                flags.append("duration")

    return {
        "source_norm": source_norm,
        "transcript_norm": transcript_norm,
        "wer": w,
        "coverage": cov,
        "duration_s": duration_s,
        "flags": flags,
        "audio_only": audio_only,
    }


# --------------------------------------------------------------------------- transcriber


class WhisperTranscriber:
    """The default transcriber. Wraps mlx_whisper.transcribe().

    mlx_whisper caches the loaded model itself, keyed by path_or_hf_repo (see
    mlx_whisper.transcribe.ModelHolder) -- calling transcribe() repeatedly
    with the same model id across 800+ chunks reuses the already-loaded
    weights, so there is nothing extra to cache in this class.

    A test never constructs this class for real transcription; a test passes
    a canned transcriber into qc.run() instead.
    """

    def __init__(
        self,
        model: str = DEFAULT_QC_CONFIG["whisper_model"],
        condition_on_previous_text: bool = DEFAULT_QC_CONFIG["condition_on_previous_text"],
    ) -> None:
        self.model = model
        # CONTRACT.md 9.5: a default transcribe call hallucinates a runaway
        # repeat loop at the end of a real render. condition_on_previous_text
        # =False removed it completely on measurement. The stage always
        # passes it -- never let a caller silently flip this back to True.
        self.condition_on_previous_text = condition_on_previous_text

    def transcribe(self, wav_path: str | Path) -> str:
        import mlx_whisper

        result = mlx_whisper.transcribe(
            str(wav_path),
            path_or_hf_repo=self.model,
            language="en",
            temperature=0.0,  # greedy decode, for determinism
            verbose=None,  # suppress tqdm/console output per chunk
            condition_on_previous_text=self.condition_on_previous_text,
        )
        return result["text"]


class FasterWhisperTranscriber:
    """A second transcriber, behind the same seam as WhisperTranscriber.
    Wraps faster_whisper.WhisperModel (CTranslate2), for a Linux CPU host
    where mlx_whisper cannot run -- mlx_whisper is Apple-silicon only.

    Read from
    .venv/lib/python3.12/site-packages/faster_whisper/transcribe.py
    (faster-whisper 1.2.1) to confirm this class's use of the library:

    - `WhisperModel(model_size_or_path, device=..., compute_type=...,
      cpu_threads=...)` (class at line 620) loads the CTranslate2 model in
      its constructor and holds it on the instance. Unlike mlx_whisper,
      which caches the loaded weights globally by path_or_hf_repo (see
      WhisperTranscriber's docstring above), a WhisperModel instance caches
      nothing beyond itself -- a second WhisperModel(...) call reloads the
      weights from disk. **This class loads the model once, in
      `_ensure_model()`, and keeps it on `self._model`.** Reloading per
      chunk across 800+ chunks would dominate the run; this is the single
      biggest performance trap in this class.
    - `WhisperModel.transcribe(audio, ...)` (line 747) returns a 2-tuple:
      `(segments, info)`. **`segments` is a lazy generator.** It comes from
      `generate_segments()` (line 1103), a function built entirely of
      `yield` statements -- no decoding runs until the caller iterates the
      generator. A caller that returns `segments` unconsumed, or that drops
      it, gets no error: it gets an empty transcript. qc_normalize() then
      compares the source against nothing, coverage reads near 0.0, and a
      good chunk flags for no reason -- or, if some future caller instead
      treats an empty return as "not yet measured" rather than "measured
      empty", a bad chunk could wave through ungated. **This class always
      consumes the generator fully, in `transcribe()`, before it returns.**
    - Each item the generator yields is a `Segment` dataclass (line 48),
      holding `.text` for that one segment. The stage's transcript is the
      join of every segment's `.text`, in order.
    - `condition_on_previous_text` defaults to `True` in the library itself
      (`transcribe()`'s signature, line 770). CONTRACT.md 9.5: a default
      transcribe call hallucinated the word "stir" about two hundred times
      at the end of a real render, with mlx_whisper, under the same failure
      mode this argument controls. `False` removed it completely on
      measurement, and the stage always passes it -- never let a caller
      silently flip this back to `True`, for this class exactly as for
      WhisperTranscriber above.
    - `beam_size` defaults to 5 in the library (`transcribe()`'s signature,
      line 753). A lower beam size decodes faster at a cost to accuracy --
      lower it only after a real measurement on the target CPU, never as a
      first guess.

    Downloading a model this class has not yet cached goes through
    `huggingface_hub.snapshot_download()` (see
    `faster_whisper/utils.py::download_model`), the same library
    `mlx_whisper` uses. `HF_HUB_DISABLE_XET=1` therefore applies to this
    class's downloads too, the same as any other huggingface_hub caller on
    this Mac.

    A test never constructs this class for real transcription, the same
    rule WhisperTranscriber's docstring states above -- a test passes a
    fake `faster_whisper` module into `sys.modules` instead, and never lets
    a real WhisperModel load.
    """

    def __init__(
        self,
        model: str = DEFAULT_QC_CONFIG["faster_whisper_model"],
        condition_on_previous_text: bool = DEFAULT_QC_CONFIG["condition_on_previous_text"],
        device: str = "cpu",
        compute_type: str = DEFAULT_QC_CONFIG["faster_whisper_compute_type"],
        cpu_threads: int = DEFAULT_QC_CONFIG["faster_whisper_cpu_threads"],
        beam_size: int = DEFAULT_QC_CONFIG["faster_whisper_beam_size"],
    ) -> None:
        self.model = model
        # CONTRACT.md 9.5, carried forward from WhisperTranscriber above:
        # the stage always passes this explicitly -- never let a caller
        # silently flip it back to the library's own True default.
        self.condition_on_previous_text = condition_on_previous_text
        self.device = device
        self.compute_type = compute_type
        self.cpu_threads = cpu_threads
        self.beam_size = beam_size
        # Loaded once, lazily, on first transcribe() -- see the class
        # docstring. None until then.
        self._model = None

    def _ensure_model(self):
        """Load the CTranslate2 model on first use and reuse it after.
        WhisperModel caches nothing itself, so a call that skipped this
        cache and constructed a fresh WhisperModel per chunk would reload
        the weights from disk 800+ times over one book."""
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise ImportError(
                    "FasterWhisperTranscriber needs the 'faster-whisper' "
                    "package, which is not installed in this environment. "
                    "Run: uv pip install faster-whisper"
                ) from exc
            self._model = WhisperModel(
                self.model,
                device=self.device,
                compute_type=self.compute_type,
                cpu_threads=self.cpu_threads,
            )
        return self._model

    def transcribe(self, wav_path: str | Path) -> str:
        model = self._ensure_model()
        segments, _info = model.transcribe(
            str(wav_path),
            language="en",
            temperature=0.0,  # greedy decode, for determinism
            beam_size=self.beam_size,
            condition_on_previous_text=self.condition_on_previous_text,
        )
        # segments is a lazy generator -- see the class docstring. Consume
        # it fully, in order, before returning, or the transcript comes
        # back empty and a good chunk flags for no reason.
        return "".join(segment.text for segment in segments)


def _select_transcriber_backend(config: dict) -> str:
    """Resolve CONTRACT.md 9.2's whisper_backend ("auto", "mlx", or
    "faster") to the concrete backend name run() should construct.

    "auto" picks "mlx" when mlx_whisper imports, "faster" otherwise -- the
    Linux CPU container has no mlx_whisper installed, so "auto" always
    resolves to "faster" there, with no config change needed per host. An
    explicit "mlx" or "faster" always wins, whether or not mlx_whisper
    actually imports; run() lets FasterWhisperTranscriber's own ImportError
    surface rather than silently falling back, so a forced "faster" with
    the package missing fails loudly instead of quietly.
    """
    backend = config.get("whisper_backend", DEFAULT_QC_CONFIG["whisper_backend"])
    if backend not in ("auto", "mlx", "faster"):
        raise ValueError(
            f"whisper_backend must be 'auto', 'mlx', or 'faster', got {backend!r}"
        )
    if backend != "auto":
        return backend
    try:
        import mlx_whisper  # noqa: F401
    except ImportError:
        return "faster"
    return "mlx"


# --------------------------------------------------------------------------- render seam
#
# render.py and engines/ are owned by Worker B and are being written at the same
# time as this file. These two wrappers do the actual import lazily, inside the
# function body, so:
#   1. importing abpipe.qc never touches abpipe.render / abpipe.engines, and
#   2. a test can monkeypatch _render_chunk / _get_engine directly on this
#      module and so never triggers the import at all, even while render.py is
#      mid-edit or briefly broken.


def _render_chunk(engine, text: str) -> tuple[np.ndarray, int]:
    from abpipe.render import render_chunk

    return render_chunk(engine, text)


def _get_engine(config: dict):
    from abpipe.engines import get_engine

    return get_engine(config)


def _apply_pronunciations(text: str, pronunciations: dict[str, str] | None) -> str:
    """CONTRACT.md 9.6's map, applied through the one shared implementation
    (abpipe.render.apply_pronunciations) so the source-side substitution
    qc_normalize does and the substitution render.py does before synthesis
    can never drift apart. Imported lazily for the same reason as
    _render_chunk/_get_engine above."""
    from abpipe.render import apply_pronunciations

    return apply_pronunciations(text, pronunciations)


def _is_fatal_disk_error(exc: BaseException) -> bool:
    """True for a disk-full / read-only-filesystem failure -- the same
    detector render.py uses (CONTRACT.md 8.2), reused rather than
    reimplemented so the two stages can never drift on what counts as
    fatal. Matters here because this stage's ladder (_re_render_chunk /
    _split_render_chunk) writes audio through the same soundfile path
    render.py does, and soundfile's own failure (LibsndfileError, not a
    plain OSError, with no .errno) needs the same message-text fallback."""
    from abpipe.render import _is_fatal_disk_error as _real_is_fatal_disk_error

    return _real_is_fatal_disk_error(exc)


def _apply_homographs(text: str, chunk_decisions: list[dict] | None) -> str:
    """CONTRACT.md 18.5's per-occurrence phoneme markup, applied through the
    one shared implementation, and for the same reason _apply_pronunciations
    above exists: the ladder re-renders text, so it must hand the engine
    exactly what stage 4 hands it. Without this a flagged chunk loses its
    forced heteronym reading and says the word wrong again -- silently,
    because the QC matcher drops non-first vowels and cannot hear the
    difference between /wuːnd/ and /waʊnd/. Imported lazily, like the other
    render.py borrowings above."""
    from abpipe.homographs import apply_homographs

    return apply_homographs(text, chunk_decisions)


def _render_input_hash(rec: dict, decisions_doc: dict, chapter_id: str) -> str:
    """The exact per-chunk input_hash formula render.run() uses
    (CONTRACT.md 18.6), asked of render.py rather than reproduced here.

    The ladder writes a render meta for the audio it promotes. That meta
    must carry the hash stage 4 itself would compute, or stage 4 reads the
    ladder's winning WAV as stale and renders over it on the next run,
    undoing the ladder's own work. This is the same drift trap
    _render_config_hash above guards against, on the input half."""
    from abpipe.render import render_input_hash

    return render_input_hash(rec, decisions_doc, chapter_id)


def _render_config_hash(engine_desc: dict, pronunciations: dict | None) -> str:
    """The exact config_hash formula render.run() uses for stage 4
    (engine.describe() + the pronunciation map). The remediation ladder
    (_re_render_chunk / _split_render_chunk below) re-renders a chunk and
    writes its render meta file with this hash, so stage 4 recognises that
    WAV as fresh on its own next run -- not a different formula that reads
    it as stale again and redoes the ladder's work."""
    from abpipe.render import render_config_hash

    return render_config_hash(engine_desc, pronunciations)


def _write_wav(path: Path, audio: np.ndarray, sample_rate: int) -> None:
    """Write a WAV atomically: to a `.tmp` file beside `path`, then
    `os.replace` into place, with the `.tmp` removed on any failure.

    Job 2's render.py review extended to this file's own writer: the
    remediation ladder (9.3, _re_render_chunk / _split_render_chunk) writes
    audio too, on the same overnight run that render.py does, so a failed
    write here should not leave a truncated WAV at the final path (which
    is_fresh() could later trust) or a `.tmp` file behind either.
    """
    import os

    import soundfile as sf

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / (path.name + ".tmp")
    try:
        sf.write(
            str(tmp_path), np.asarray(audio, dtype=np.float32), int(sample_rate),
            subtype="PCM_16", format="WAV",
        )
        os.replace(tmp_path, path)
    except BaseException:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def _wav_duration(path: str | Path) -> float:
    import soundfile as sf

    info = sf.info(str(path))
    return info.frames / float(info.samplerate)


def _wav_rms(path: str | Path) -> float:
    """Return the root-mean-square amplitude of a WAV's samples, for
    _score()'s audio_only "not silent" check (overlord addition, real
    ch08/0128). A digitally silent or near-silent render (RMS near 0) is
    the one thing a transcript comparison at this chunk length cannot rule
    out on its own -- whisper can hallucinate words over dead air just as
    readily as over real speech.
    """
    import soundfile as sf

    data, _ = sf.read(str(path), dtype="float32", always_2d=False)
    if data.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(data), dtype=np.float64)))


# --------------------------------------------------------------------------- the ladder (9.3)

_SENTENCE_END_RE = re.compile(r"[.!?]+[\"'’”)\]]*(\s+)")
_COMMA_RE = re.compile(r",(\s+)")
_SPACE_RE = re.compile(r"\s+")


def _nearest(positions: list[int], target: float) -> int:
    return min(positions, key=lambda p: abs(p - target))


def _has_content(s: str) -> bool:
    """True when a string holds at least one letter or digit -- used to
    reject a "split" whose half is just leftover punctuation/whitespace."""
    return any(ch.isalnum() for ch in s)


def split_chunk_text(text: str) -> tuple[str, str] | None:
    """Split chunk text into two halves for the ladder's rung 2, or return
    None when the text cannot be meaningfully split.

    Defect 2a fix (CONTRACT.md 9.3): the ladder must never cut inside a
    word. A real internal boundary is required -- a sentence end, then a
    comma, then a plain space, checked in that preference order, nearest the
    midpoint -- and both resulting halves must hold real content, not just
    leftover punctuation. A single word (this book's real chunk ch05/0002,
    "Gyko!") has no such boundary at all; splitting it used to fall back to
    a hard midpoint cut that landed inside the word, producing two nonsense
    fragments -- the exact mechanism that turned a false duration flag into
    real, audible damage. The caller must treat a None return as "this
    chunk cannot be split" and skip rung 2 entirely, going straight to
    needs_human instead of ever falling back to a mid-word cut.
    """
    stripped = text.strip()
    n = len(stripped)
    if n < 2:
        return None
    mid = n / 2

    for pattern in (_SENTENCE_END_RE, _COMMA_RE, _SPACE_RE):
        if pattern is _SPACE_RE:
            positions = [m.start() for m in pattern.finditer(stripped) if 0 < m.start() < n]
        else:
            positions = [m.end() for m in pattern.finditer(stripped) if 0 < m.end() < n]
        if not positions:
            continue
        point = _nearest(positions, mid)
        first, second = stripped[:point].strip(), stripped[point:].strip()
        if _has_content(first) and _has_content(second):
            return first, second

    return None


def _re_render_chunk(
    source_text: str,
    out_path: Path,
    engine,
    pronunciations: dict[str, str] | None = None,
    chunk_decisions: list[dict] | None = None,
) -> None:
    """Ladder rung 1: render the chunk again, into out_path.

    Kokoro is deterministic, so a re-render of the exact same text mostly
    catches infrastructure faults (a truncated write, a transient decode
    glitch) rather than content errors -- it will not fix a genuine
    mispronunciation. We still run it: the contract requires it, and it is
    cheap next to a whisper pass.

    Defect 2 fix: this used to write straight to the chunk's real wav_path
    and delete its meta first. It no longer touches either -- out_path is a
    scratch file (see _qc_chunk's _scratch_path()), so the chunk's real WAV
    and meta are never disturbed unless and until this rung's audio is
    chosen as the best-scoring attempt.

    This rung renders text, so it goes through the same source-to-engine
    pronunciation substitution as render.py (CONTRACT.md 9.6) -- otherwise a
    re-rendered chunk would silently lose the correction stage 4 gave it,
    and the WAV this rung produces would say "Gyko" wrong again.
    """
    # CONTRACT.md 18.5: the homograph markup first, then the pronunciation
    # map -- the same order, through the same two functions, that stage 4
    # uses. Reverse it and a pronunciation entry can match inside the
    # bracketed word.
    text = _apply_homographs(source_text, chunk_decisions)
    text = _apply_pronunciations(text, pronunciations)
    audio, sr = _render_chunk(engine, text)
    _write_wav(out_path, audio, sr)


def _split_decisions(
    first: str, second: str, chunk_decisions: list[dict] | None
) -> tuple[list[dict], list[dict]]:
    """Split one chunk's homograph decisions across the two ladder halves.

    A decision names the nth whole-word match of a word **in the whole
    chunk**. Rung 2 renders two halves separately, so each half needs its
    own occurrence numbers. Handing a half the chunk-level number would
    mark the wrong word, or raise, which is why this re-indexing exists.

    The mapping is exact. `split_chunk_text` only ever cuts at a sentence
    end, a comma, or a space, so it never cuts inside a word: every match
    in the chunk survives whole in one half or the other, and the order is
    kept. Match n of the chunk is therefore match n of the first half when
    the first half holds at least n matches, and match (n - that count) of
    the second half otherwise.

    A decision that matches in neither half is dropped, not raised on. The
    ladder is a repair path for a chunk QC already flagged; it must not
    turn a stale decision file into a crash in the middle of a long QC run.
    """
    if not chunk_decisions:
        return [], []
    from abpipe.homographs import count_matches  # lazy, same reason as above

    first_out: list[dict] = []
    second_out: list[dict] = []
    for decision in chunk_decisions:
        word = str(decision.get("word", ""))
        try:
            wanted = int(decision.get("occurrence", 0))
        except (TypeError, ValueError):
            continue
        in_first = count_matches(word, first)
        if 1 <= wanted <= in_first:
            first_out.append({**decision, "occurrence": wanted})
        elif in_first < wanted <= in_first + count_matches(word, second):
            second_out.append({**decision, "occurrence": wanted - in_first})
    return first_out, second_out


def _split_render_chunk(
    first: str,
    second: str,
    out_path: Path,
    engine,
    pronunciations: dict[str, str] | None = None,
    chunk_decisions: list[dict] | None = None,
) -> None:
    """Ladder rung 2: render two already-split halves, join, into out_path.

    The caller (_qc_chunk) is responsible for finding the split point
    (split_chunk_text()) and for refusing to call this at all when no real
    internal boundary exists (defect 2a) -- this function only ever renders
    the two halves it is given. Each half goes through the same
    pronunciation substitution as rung 1 before synthesis.

    Defect 2 fix: writes to a scratch out_path, same as rung 1 -- see that
    function's docstring.
    """
    first_decisions, second_decisions = _split_decisions(first, second, chunk_decisions)
    parts: list[np.ndarray] = []
    sr = None
    for half, half_decisions in ((first, first_decisions), (second, second_decisions)):
        if not half:
            continue
        # CONTRACT.md 18.5: the markup first, then the pronunciation map --
        # the same order stage 4 and rung 1 use.
        text = _apply_homographs(half, half_decisions)
        text = _apply_pronunciations(text, pronunciations)
        audio, half_sr = _render_chunk(engine, text)
        parts.append(np.asarray(audio, dtype=np.float32).reshape(-1))
        sr = half_sr
    if not parts:
        parts = [np.zeros(1, dtype=np.float32)]
    combined = np.concatenate(parts)
    _write_wav(out_path, combined, sr or 24000)


def _scratch_path(wav_path: Path, attempt_no: int) -> Path:
    """Return a scratch path for one ladder attempt's candidate audio, in
    the same directory as wav_path (so a later os.replace() onto wav_path is
    a same-filesystem atomic rename) but never sharing wav_path's name.

    Defect 2 fix: the whole point of rendering into a scratch file is that
    wav_path itself -- the chunk's real, on-disk WAV -- is never touched
    until the best-scoring candidate among every attempt is known. The old
    ladder overwrote wav_path at every rung, so whichever rung ran LAST was
    always what survived, even when it scored worse than an earlier attempt.
    """
    return wav_path.with_name(f"{wav_path.stem}.qc-attempt{attempt_no}{wav_path.suffix}")


def _candidate_rank(score: dict) -> tuple:
    """Sort key for choosing the best-scoring ladder candidate: lower is
    better. An unflagged candidate always outranks a flagged one; among
    candidates with the same clear/flagged status, fewer flags is better,
    and `wer + (1 - coverage)` is a cheap combined quality proxy that breaks
    ties between two candidates carrying the same flag count. `min()` over a
    list of candidates in attempt order is stable, so a true tie (e.g. two
    attempts scoring identically) resolves to the EARLIER attempt -- the
    simplest, cheapest surviving audio, and never a needless replacement of
    something already just as good.
    """
    cleared = 0 if not score["flags"] else 1
    return (cleared, len(score["flags"]), score["wer"] + (1.0 - score["coverage"]))


def _qc_chunk(
    chapter_id: str,
    rec: dict,
    source_text: str,
    wav_path: Path,
    engine,
    transcriber,
    config: dict,
    duration_model: tuple[float, float] | None,
    engine_desc_hash: str,
    pronunciations: dict[str, str] | None = None,
    accept_records: dict[tuple[str, str], dict] | None = None,
    chunk_decisions: list[dict] | None = None,
    render_input_hash_value: str | None = None,
) -> dict:
    """Score one chunk and, if flagged, run the remediation ladder per
    CONTRACT.md 9.3 -- keeping the BEST-scoring attempt, never merely the
    last one (defect 2).

    Every attempt (the original WAV, then each ladder rung that runs) is
    scored before any decision is made about which one survives. wav_path
    is never touched while the ladder is in progress -- each rung renders
    into its own scratch file (_scratch_path()) -- so a crash mid-ladder
    leaves the chunk's real WAV and meta exactly as they were, still valid
    and still matching each other. Only once the winner is known does this
    function move the winning scratch file onto wav_path (or leave wav_path
    untouched, when attempt 1 itself is the winner) and write a render meta
    that describes whichever bytes actually end up there -- CONTRACT.md 3's
    idempotence rule never sees a WAV and a meta that disagree.

    Rung 1 (re-render) is skipped-but-still-scored when it reproduces
    attempt 1's audio byte for byte: Kokoro is deterministic, so identical
    text and identical config can only ever reproduce identical bytes, and a
    second whisper pass on already-transcribed audio can only ever repeat
    the first pass's verdict. Reusing attempt 1's score avoids that wasted
    transcription and stops a provably pointless duplicate from ever being
    treated as independent evidence.

    Rung 2 (split) never runs at all when split_chunk_text() finds no real
    internal boundary in the source text (defect 2a) -- a single word has
    nowhere safe to cut, and the chunk goes straight to needs_human with
    whichever of the first two attempts scored best.

    `accept_records`, keyed `(chapter_id, chunk_id)` (see
    load_accept_records()), is consulted ONLY when the ladder's own verdict
    is `needs_human`: a hash-pinned human acceptance is the answer to
    needs_human, never a shortcut around scoring in the first place, so it
    is checked strictly after the ladder has already tried and failed.
    """
    cid = rec["id"]
    chars = rec.get("chars") or 0
    scratch_files: list[Path] = []

    def _score_wav(path: Path) -> dict:
        duration_s = _wav_duration(path)
        rms = _wav_rms(path)
        transcript = transcriber.transcribe(str(path))
        return _score(
            source_text, transcript, config, duration_model, duration_s, chars, pronunciations, rms
        )

    try:
        attempt1_score = _score_wav(wav_path)
        candidates: list[tuple[int, Path | None, dict]] = [(1, None, attempt1_score)]

        if attempt1_score["flags"]:
            wav_hash_before = hash_file(wav_path)
            scratch2 = _scratch_path(wav_path, 2)
            _re_render_chunk(source_text, scratch2, engine, pronunciations, chunk_decisions)
            scratch_files.append(scratch2)

            if hash_file(scratch2) == wav_hash_before:
                attempt2_score = attempt1_score
            else:
                attempt2_score = _score_wav(scratch2)
            candidates.append((2, scratch2, attempt2_score))

            if attempt2_score["flags"]:
                boundary = split_chunk_text(source_text)
                if boundary is not None:
                    first, second = boundary
                    scratch3 = _scratch_path(wav_path, 3)
                    _split_render_chunk(
                        first, second, scratch3, engine, pronunciations, chunk_decisions
                    )
                    scratch_files.append(scratch3)
                    attempt3_score = _score_wav(scratch3)
                    candidates.append((3, scratch3, attempt3_score))
                # else: no real internal boundary -- a single word, or
                # anything else that cannot be split without cutting inside
                # a word. Rung 2 is skipped entirely; attempts stays at 2.

        attempts = candidates[-1][0]
        winner_attempt, winner_path, winner_state = min(candidates, key=lambda c: _candidate_rank(c[2]))

        if winner_attempt == 1:
            if winner_state["flags"]:
                resolution = "needs_human"
            elif winner_state.get("audio_only"):
                # Distinct from "ok" on purpose: this chunk was too short for
                # a transcript comparison to mean anything and cleared on
                # audio plausibility instead (CONTRACT.md 9.4 addition). "ok"
                # must always mean "the transcript matched".
                resolution = "audio_only"
            else:
                resolution = "ok"
        elif winner_state["flags"]:
            resolution = "needs_human"
        else:
            resolution = "re_rendered" if winner_attempt == 2 else "split"

        if winner_path is not None:
            # The winner is a ladder rung's audio, not attempt 1's -- move
            # it onto wav_path (atomic, same directory) and give it a fresh
            # render meta so a later run's idempotence check always matches
            # the bytes actually on disk.
            clear_meta(wav_path)
            os.replace(winner_path, wav_path)
            # CONTRACT.md 18.6: the input_hash stage 4 itself would compute,
            # which folds this chunk's homograph decisions into its sha256.
            # Writing the bare sha here would make stage 4 read this
            # promoted WAV as stale and render over it, undoing the ladder.
            write_meta(
                wav_path,
                "render",
                render_input_hash_value or rec["sha256"],
                engine_desc_hash,
            )
            if winner_path in scratch_files:
                scratch_files.remove(winner_path)
        # else: attempt 1 won -- wav_path and its existing meta (if any)
        # were never touched, which is correct either way: nothing this
        # function tried was ever better than what was already there.

        # CHANGE 2: a chunk a human has reviewed can be accepted. Checked
        # only now, against whatever bytes actually ended up at wav_path
        # above (possibly a ladder rung's audio, never the pre-ladder
        # original) -- so the pin is always against reality, and a later
        # re-render that changes those bytes silently voids the acceptance
        # (the hash simply stops matching; nothing has to notice on its own).
        if resolution == "needs_human" and accept_records:
            entry = accept_records.get((chapter_id, cid))
            if entry is not None:
                current_hash = hash_file(wav_path)
                if current_hash == entry["wav_sha256"]:
                    resolution = "accepted"
                    print(
                        f"[qc] ACCEPTED {chapter_id}/{cid}: {entry['reason']}",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"[qc] WARNING: acceptance for {chapter_id}/{cid} is VOID "
                        f"-- the WAV changed since it was accepted "
                        f"(pinned {entry['wav_sha256'][:12]}…, now "
                        f"{current_hash[:12]}…); still needs_human",
                        file=sys.stderr,
                    )

        return {
            "schema": 1,
            "chapter": chapter_id,
            "chunk": cid,
            "source_norm": winner_state["source_norm"],
            "transcript_norm": winner_state["transcript_norm"],
            "wer": round(winner_state["wer"], 6),
            "coverage": round(winner_state["coverage"], 6),
            "duration_s": round(winner_state["duration_s"], 6),
            "attempts": attempts,
            "flags": winner_state["flags"],
            "resolution": resolution,
        }
    finally:
        # Any losing candidate's scratch file (the winner, if any, was
        # already moved out of this list above) never survives this
        # function -- including on an exception raised mid-ladder, so a
        # disk-full or a transient whisper failure never leaves a
        # `*.qc-attemptN.wav` file behind for a later run to trip over.
        for scratch in scratch_files:
            if scratch.exists():
                try:
                    scratch.unlink()
                except OSError:
                    pass


# --------------------------------------------------------------------------- progress


def _fmt_eta(elapsed: float, done: int, total: int) -> str:
    if done <= 0 or total <= 0:
        return "?"
    remaining = max(total - done, 0) * (elapsed / done)
    m, s = divmod(int(remaining), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _count_chunks(ctx, chapter_ids: list[str]) -> int:
    total = 0
    for cid in chapter_ids:
        index = read_json(ctx.stage_dir("chunk", make=False) / cid / "index.json")
        if isinstance(index, dict):
            total += len(index.get("chunks") or [])
    return total


# --------------------------------------------------------------------------- per-chapter


def _process_chapter(
    ctx,
    chapter_id: str,
    config: dict,
    engine,
    transcriber,
    force: bool,
    cfg_hash: str,
    engine_desc_hash: str,
    progress_cb,
    pronunciations: dict[str, str] | None = None,
    max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
    accept_records: dict[tuple[str, str], dict] | None = None,
) -> tuple[dict, int, int, bool, str | None]:
    # CONTRACT.md 18: this book's homograph decisions, read once per
    # chapter. The ladder needs them to re-render a chunk the way stage 4
    # rendered it, and the render meta it writes needs the matching hash.
    # An absent file gives an empty document, and then every hash below is
    # the bare chunk sha256, exactly as it was before this rule existed.
    from abpipe.homographs import decisions_for_chunk as homographs_decisions_for_chunk
    from abpipe.homographs import read_decisions as _read_homograph_decisions

    decisions_doc = _read_homograph_decisions(ctx.book_dir)

    chunk_dir = ctx.stage_dir("chunk", make=False) / chapter_id
    audio_dir = ctx.stage_dir("render", make=False) / chapter_id
    qc_chunk_dir = ctx.stage_dir("qc") / chapter_id

    index = read_json(chunk_dir / "index.json")
    if not isinstance(index, dict):
        index = {}
    records = index.get("chunks")
    if not isinstance(records, list):
        records = []

    durations: dict[str, float] = {}
    for rec in records:
        durations[rec["id"]] = _wav_duration(audio_dir / f"{rec['id']}.wav")

    pairs = [
        (float(rec["chars"]), durations[rec["id"]])
        for rec in records
        if rec.get("chars")
    ]
    duration_model = _fit_duration_model(pairs) if len(records) >= 5 and pairs else None

    counts = {
        "chunks": 0,
        "flagged": 0,
        "re_rendered": 0,
        "split": 0,
        "audio_only": 0,
        "needs_human": 0,
        "accepted": 0,
        "duration_s": 0.0,
    }
    done_n = 0
    skipped_n = 0
    consecutive_failures = 0
    aborted = False
    abort_reason: str | None = None

    for rec in records:
        cid = rec["id"]
        txt_path = chunk_dir / rec["file"]
        wav_path = audio_dir / f"{cid}.wav"
        out_path = qc_chunk_dir / f"{cid}.json"

        wav_sha = hash_file(wav_path)
        input_hash = hash_many([rec["sha256"], wav_sha])

        result = None
        if not force and is_fresh(out_path, input_hash, cfg_hash):
            candidate = read_json(out_path)
            # Defect 2: is_fresh() only validates the *meta* file next to
            # out_path -- it says nothing about whether out_path's own JSON
            # content still parses, or still has the shape this stage wrote.
            # A kill or a full disk can truncate/corrupt the persisted
            # per-chunk result while its meta.json survives untouched and
            # matching. read_json() already returns None on a parse failure;
            # this also guards the case where the file parses but is not the
            # dict this stage expects (e.g. an empty object, a list, a bare
            # string -- any of which would previously crash the whole `qc`
            # command with AttributeError on the very next `.get()` call
            # below). Either way, a bad payload here means "redo this chunk",
            # never a crash three ladder rungs deep into an unattended run.
            if isinstance(candidate, dict):
                result = candidate

        if result is not None:
            skipped_n += 1
            consecutive_failures = 0
            # CHANGE 2: a fresh cached needs_human result is still checked
            # against qc-accept.json every run, never just at the moment it
            # was first scored -- otherwise a human's newly recorded
            # acceptance would sit inert until something else happened to
            # invalidate the cache. wav_sha (above) is the CURRENT wav's
            # hash, already computed for the freshness check, so the
            # comparison below is exactly the same hash pin _qc_chunk()
            # applies to a chunk scored for the first time.
            if result.get("resolution") == "needs_human" and accept_records:
                entry = accept_records.get((chapter_id, cid))
                if entry is not None and wav_sha == entry["wav_sha256"]:
                    result = dict(result)
                    result["resolution"] = "accepted"
                    write_json(out_path, result)
                    write_meta(out_path, "qc", input_hash, cfg_hash, extra={"attempts": result["attempts"]})
                    print(f"[qc] ACCEPTED {chapter_id}/{cid}: {entry['reason']}", file=sys.stderr)
                elif entry is not None:
                    print(
                        f"[qc] WARNING: acceptance for {chapter_id}/{cid} is VOID "
                        f"-- the WAV changed since it was accepted "
                        f"(pinned {entry['wav_sha256'][:12]}…, now {wav_sha[:12]}…); "
                        "still needs_human",
                        file=sys.stderr,
                    )
        else:
            source_text = txt_path.read_text(encoding="utf-8")
            try:
                chunk_decisions = homographs_decisions_for_chunk(
                    decisions_doc, chapter_id, rec["id"]
                )
                result = _qc_chunk(
                    chapter_id, rec, source_text, wav_path, engine, transcriber,
                    config, duration_model, engine_desc_hash, pronunciations,
                    accept_records, chunk_decisions,
                    _render_input_hash(rec, decisions_doc, chapter_id),
                )
            except Exception as exc:  # noqa: BLE001 - CONTRACT.md 8.2, matched to render.py:
                # a multi-hour unattended run must survive one bad chunk, but
                # not grind through the rest of the chapter on a persistent
                # fault. Two guards, identical to render.py's: a fatal disk/
                # read-only error aborts at once; anything else trips the
                # circuit breaker after max_consecutive_failures in a row.
                # Neither case raises -- both report through the return
                # value's aborted/abort_reason, exactly like render.py, so a
                # bare OSError never again escapes this stage as a raw
                # traceback (the defect this replaces: this loop used to
                # catch the fatal-disk case and then re-raise it anyway).
                consecutive_failures += 1
                print(f"[qc] FAILED {chapter_id} {cid}: {exc!r}", file=sys.stderr)

                if _is_fatal_disk_error(exc):
                    aborted = True
                    abort_reason = f"fatal disk error at {chapter_id} chunk {cid}: {exc!r}"
                    print(f"[qc] ABORT: {abort_reason}", file=sys.stderr)
                    break

                if consecutive_failures >= max_consecutive_failures:
                    aborted = True
                    abort_reason = (
                        f"{consecutive_failures} consecutive chunk failures, "
                        f"most recently {chapter_id} chunk {cid}: {exc!r}"
                    )
                    print(f"[qc] ABORT: {abort_reason}", file=sys.stderr)
                    break

                continue  # one bad chunk, surrounded by good ones: keep going

            consecutive_failures = 0
            write_json(out_path, result)
            write_meta(out_path, "qc", input_hash, cfg_hash, extra={"attempts": result["attempts"]})
            done_n += 1

        counts["chunks"] += 1
        counts["duration_s"] += result.get("duration_s", 0.0)
        # "flagged" counts a chunk that needed any remediation or exceptional
        # handling, i.e. its final resolution is not "ok" or "audio_only" --
        # matches the contract's example where
        # flagged == re_rendered + split + needs_human + accepted.
        # "audio_only" is NOT flagged: it cleared cleanly on its first
        # attempt, on a legitimate check for its length, same as "ok" -- it
        # is only ever recorded separately so a reader can tell it apart
        # from a transcript match, not because anything went wrong.
        resolution = result.get("resolution")
        if resolution not in ("ok", "audio_only"):
            counts["flagged"] += 1
        if resolution == "re_rendered":
            counts["re_rendered"] += 1
        elif resolution == "split":
            counts["split"] += 1
        elif resolution == "audio_only":
            counts["audio_only"] += 1
        elif resolution == "needs_human":
            counts["needs_human"] += 1
        elif resolution == "accepted":
            counts["accepted"] += 1

        progress_cb(chapter_id, cid, counts["flagged"])

    counts["duration_s"] = round(counts["duration_s"], 6)
    return counts, done_n, skipped_n, aborted, abort_reason


# --------------------------------------------------------------------------- report


def _recompute_totals(report: dict) -> None:
    totals = {
        "chunks": 0,
        "flagged": 0,
        "re_rendered": 0,
        "split": 0,
        "audio_only": 0,
        "needs_human": 0,
        "accepted": 0,
        "duration_s": 0.0,
    }
    for chap in report["chapters"].values():
        for key in ("chunks", "flagged", "re_rendered", "split", "audio_only", "needs_human", "accepted"):
            totals[key] += chap.get(key, 0)
        totals["duration_s"] += chap.get("duration_s", 0.0)
    totals["duration_s"] = round(totals["duration_s"], 6)
    report["totals"] = totals
    # CHANGE 2: an "accepted" chunk does NOT count against green -- a human
    # already looked at it and confirmed the audio is correct. Only
    # "needs_human" (a chunk still awaiting that human look) turns the
    # report red.
    report["status"] = "red" if totals["needs_human"] > 0 else "green"


def report_is_green(report: dict | None) -> bool:
    """Return True when the QC report holds no chunk needing a human.

    Stage 6 (assemble) imports this to refuse to run against a red report.
    """
    if not isinstance(report, dict):
        return False
    return report.get("status") == "green"


# --------------------------------------------------------------------------- human acceptance (qc-accept.json)
#
# CONTRACT.md 9.3's ladder ends at needs_human and stops the pipeline there
# -- correctly, for a chunk nobody has looked at. But across 2000+ chunks a
# few genuine whisper mishearings of otherwise-correct audio are inevitable
# (real chunk ch07/0130: the proper noun "You Connor" heard as "Ucona",
# audio otherwise perfect). Without a way to record "a human looked, and the
# audio is fine", a book with even one such chunk could never ship. This
# section is that record.
#
# work/<slug>/qc-accept.json:
#   {
#     "schema": 1,
#     "accepted": [
#       {"chapter": "ch07", "chunk": "0130", "wav_sha256": "...",
#        "reason": "...", "accepted_at": "20260815T...Z"}
#     ]
#   }
#
# THE STAGE NEVER WRITES THIS FILE ON ITS OWN -- only accept_chunk() below
# does, and only ever on explicit human/overlord instruction. An entry is
# HASH-PINNED to the exact WAV bytes it was accepted for: the whole point is
# that nobody can accept a chunk and then quietly re-render different audio
# underneath it and have the acceptance silently carry over. If the wav
# changes, the pin no longer matches, and the chunk flags again -- see
# load_accept_records()'s callers (_process_chapter(), _qc_chunk()), which
# always hash the CURRENT on-disk WAV and compare, never trust a cached
# verdict. An entry missing a hash or a non-empty reason is invalid and is
# ignored (with a logged warning), not applied partially.


def load_accept_records(ctx) -> dict[tuple[str, str], dict]:
    """Read work/<slug>/qc-accept.json into {(chapter, chunk): entry},
    skipping (and warning about) any entry missing a hash or a reason.

    Returns {} when the file is absent, unparsable, or holds no valid
    entries -- a book that has never accepted anything behaves exactly as
    it did before this feature existed.
    """
    path = ctx.book_dir / "qc-accept.json"
    data = read_json(path)
    if not isinstance(data, dict):
        return {}
    records: dict[tuple[str, str], dict] = {}
    for entry in data.get("accepted") or []:
        if not isinstance(entry, dict):
            print(f"[qc] WARNING: ignoring non-object qc-accept.json entry: {entry!r}", file=sys.stderr)
            continue
        chapter = entry.get("chapter")
        chunk = entry.get("chunk")
        wav_sha256 = entry.get("wav_sha256")
        reason = entry.get("reason")
        if not (
            isinstance(chapter, str) and chapter
            and isinstance(chunk, str) and chunk
            and isinstance(wav_sha256, str) and wav_sha256
            and isinstance(reason, str) and reason.strip()
        ):
            print(
                f"[qc] WARNING: ignoring invalid qc-accept.json entry "
                f"(needs chapter, chunk, wav_sha256, and a non-empty reason): {entry!r}",
                file=sys.stderr,
            )
            continue
        records[(chapter, chunk)] = entry
    return records


def accept_chunk(ctx, chapter: str, chunk: str, reason: str, accepted_at: str | None = None) -> dict:
    """Append one human acceptance to work/<slug>/qc-accept.json, hashing
    the chunk's CURRENT on-disk WAV. THE ONE SANCTIONED WAY to write this
    file -- qc.run() itself never calls this.

    Re-accepting the same (chapter, chunk) replaces its prior entry rather
    than appending a duplicate, so accepting again after a deliberate
    re-render updates the pin instead of leaving a stale entry beside a new
    one.

    Raises ValueError for an empty `reason`, FileNotFoundError when the
    chunk has no rendered WAV yet.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason must not be empty")
    wav_path = ctx.stage_dir("render", make=False) / chapter / f"{chunk}.wav"
    if not wav_path.exists():
        raise FileNotFoundError(f"no WAV to accept at {wav_path}")

    path = ctx.book_dir / "qc-accept.json"
    data = read_json(path)
    if not isinstance(data, dict):
        data = {"schema": 1, "accepted": []}
    existing = data.get("accepted")
    if not isinstance(existing, list):
        existing = []

    entry = {
        "chapter": chapter,
        "chunk": chunk,
        "wav_sha256": hash_file(wav_path),
        "reason": reason.strip(),
        "accepted_at": accepted_at or utc_stamp(),
    }
    data["accepted"] = [
        e for e in existing
        if not (isinstance(e, dict) and e.get("chapter") == chapter and e.get("chunk") == chunk)
    ] + [entry]
    write_json(path, data)
    print(f"[qc] recorded acceptance for {chapter}/{chunk}: {entry['reason']}", file=sys.stderr)
    return entry


# --------------------------------------------------------------------------- entry point


def run(
    ctx,
    chapters: list[str] | None = None,
    force: bool = False,
    engine=None,
    transcriber=None,
    max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
    book_config: dict | None = None,
    **kw,
) -> dict:
    """Run stage 5 -- QC. Return the summary dict (CONTRACT.md 13).

    `status` is "red" the moment any chunk anywhere ends `needs_human`. That
    is signalled loudly in the return value: `summary["failed"]` holds the
    count of needs_human chunks (non-zero forces a non-zero count the CLI can
    turn into exit code 1, per section 14), and `summary["status"]` mirrors
    the whole-book report status.

    CONTRACT.md 8.2's two abort guards apply here too (mirrored from
    render.py): `summary["aborted"]` and `summary["abort_reason"]` report a
    fatal, non-recoverable fault (a full disk, or `max_consecutive_failures`
    chunk failures in a row) without ever raising -- see
    `_process_chapter`'s except block. A caller (cli.py) reads those two
    keys generically across every stage, this one included.

    `book_config` is the book config dict (CONTRACT.md 4.1,
    source/<slug>.config.json), as returned by extract.load_book_config() --
    optional, and taken as a plain dict rather than a path so this module
    never has to import extract.py (Worker A's file) to get it. The CLI
    (cli.py, Worker E) is responsible for loading the book config and
    passing it down. It is consulted only to seed a brand-new
    qc-config.json's "equivalences" key (CONTRACT.md 9.2) from the book
    config's `qc.equivalences`, and only when qc-config.json does not exist
    yet -- see `_load_or_write_qc_config()`. Omitting it (the default) is
    exactly the old behaviour: a fresh qc-config.json gets no equivalences
    seed, same as before this argument existed.
    """
    equivalences_seed = None
    if isinstance(book_config, dict):
        equivalences_seed = (book_config.get("qc") or {}).get("equivalences")
    config = _load_or_write_qc_config(ctx, equivalences_seed)

    if engine is None:
        engine = _get_engine(dict(ctx.engine_config))
    if transcriber is None:
        # CONTRACT.md 9.2's whisper_backend: mlx_whisper only runs on Apple
        # silicon, so a Linux CPU host needs a second transcriber. See
        # _select_transcriber_backend()'s docstring for the "auto"
        # resolution rule.
        condition_on_previous_text = config.get(
            "condition_on_previous_text", DEFAULT_QC_CONFIG["condition_on_previous_text"]
        )
        backend = _select_transcriber_backend(config)
        if backend == "faster":
            transcriber = FasterWhisperTranscriber(
                config.get("faster_whisper_model", DEFAULT_QC_CONFIG["faster_whisper_model"]),
                condition_on_previous_text=condition_on_previous_text,
                compute_type=config.get(
                    "faster_whisper_compute_type", DEFAULT_QC_CONFIG["faster_whisper_compute_type"]
                ),
                cpu_threads=config.get(
                    "faster_whisper_cpu_threads", DEFAULT_QC_CONFIG["faster_whisper_cpu_threads"]
                ),
                beam_size=config.get(
                    "faster_whisper_beam_size", DEFAULT_QC_CONFIG["faster_whisper_beam_size"]
                ),
            )
        else:
            transcriber = WhisperTranscriber(
                config.get("whisper_model", DEFAULT_QC_CONFIG["whisper_model"]),
                condition_on_previous_text=condition_on_previous_text,
            )

    # The per-book pronunciation map (CONTRACT.md 9.6): a source-side-only
    # override read from book.json's optional "pronunciations" key, empty by
    # default.
    pronunciations = dict(ctx.book.get("pronunciations") or {})

    # CHANGE 2: human acceptances (work/<slug>/qc-accept.json), read fresh
    # every run -- see this module's "human acceptance" section. Never part
    # of cfg_hash: an acceptance does not change how a chunk is SCORED, only
    # whether a needs_human verdict is overridden afterward, and
    # _process_chapter() re-checks it even against an already-cached
    # needs_human result (see there) so a newly recorded acceptance takes
    # effect on the very next run without needing --force.
    accept_records = load_accept_records(ctx)

    engine_desc = engine.describe()
    # This must be the exact formula render.run() uses for stage 4's
    # config_hash (engine.describe() + the pronunciation map), not a plain
    # hash_obj(engine_desc): the remediation ladder below writes a "render"
    # meta file using this hash, and it has to be a hash stage 4 itself
    # would recognise as fresh -- otherwise every chunk the ladder touches
    # would read as stale again the moment render.run() runs next, and get
    # rendered a second time for no reason.
    engine_desc_hash = _render_config_hash(engine_desc, pronunciations)
    # CONTRACT.md 9.2's config_hash covers qc-config.json and
    # engine.describe(); 9.6 adds the pronunciation map on top -- a change to
    # it must invalidate every cached qc-report entry, or a chunk that needed
    # the new mapping to pass would stay marked "needs_human" (or stay
    # falsely "ok") forever. `equivalences` needs no separate entry here: a
    # qc-config.json override of it is already part of `config`, which is
    # already hashed.
    #
    # `_config_for_hash(config)`, not `config` -- see the comment above
    # NEW_QC_CONFIG_KEYS_SINCE_LINUX_ENGINES. Hashing `config` directly
    # would stale every already-delivered book the moment
    # DEFAULT_QC_CONFIG gained the whisper_backend/faster_whisper_* keys,
    # for zero behaviour change to those books.
    cfg_hash = qc_config_hash(config, engine_desc, pronunciations)

    chapter_ids = ctx.chapter_ids(chapters)

    qc_dir = ctx.stage_dir("qc")
    report_path = qc_dir / "qc-report.json"
    report = read_json(report_path)
    if not isinstance(report, dict):
        report = {
            "schema": 1,
            "generated_at": "",
            "thresholds": {},
            "chapters": {},
            "totals": {},
            "status": "green",
        }
    report.setdefault("chapters", {})

    summary = {
        "stage": "qc",
        "done": 0,
        "skipped": 0,
        "failed": 0,
        "flagged": 0,
        "re_rendered": 0,
        "split": 0,
        "audio_only": 0,
        "needs_human": 0,
        "accepted": 0,
        "aborted": False,
        "abort_reason": None,
    }

    total_chunks_all = _count_chunks(ctx, chapter_ids)
    t_start = time.monotonic()
    processed = [0]

    def _progress_cb(chapter_id: str, chunk_id: str, flagged_so_far: int) -> None:
        processed[0] += 1
        if processed[0] % PROGRESS_EVERY != 0 and processed[0] != total_chunks_all:
            return
        elapsed = time.monotonic() - t_start
        eta = _fmt_eta(elapsed, processed[0], total_chunks_all)
        print(
            f"[qc] {chapter_id} {chunk_id} ({processed[0]}/{total_chunks_all}) "
            f"flagged={flagged_so_far} eta={eta}",
            file=sys.stderr,
        )

    for chapter_id in chapter_ids:
        chap_counts, done_n, skipped_n, chap_aborted, chap_abort_reason = _process_chapter(
            ctx, chapter_id, config, engine, transcriber, force,
            cfg_hash, engine_desc_hash, _progress_cb, pronunciations,
            max_consecutive_failures, accept_records,
        )
        report["chapters"][chapter_id] = chap_counts
        summary["done"] += done_n
        summary["skipped"] += skipped_n
        summary["flagged"] += chap_counts["flagged"]
        summary["re_rendered"] += chap_counts["re_rendered"]
        summary["split"] += chap_counts["split"]
        summary["audio_only"] += chap_counts["audio_only"]
        summary["needs_human"] += chap_counts["needs_human"]
        summary["accepted"] += chap_counts["accepted"]

        if chap_aborted:
            # Fatal, non-recoverable: stop the whole qc run right here,
            # exactly like render.py. The chapters already processed keep
            # their results (written above); the chapters after this one are
            # simply never attempted this run -- a later resumed run picks
            # them up normally.
            summary["aborted"] = True
            summary["abort_reason"] = chap_abort_reason
            break

    report["thresholds"] = {
        "wer_max": config["wer_max"],
        "coverage_min": config["coverage_min"],
        "duration_outlier_factor": config["duration_outlier_factor"],
        "min_chars_for_duration_test": config.get(
            "min_chars_for_duration_test", DEFAULT_QC_CONFIG["min_chars_for_duration_test"]
        ),
    }
    report["generated_at"] = utc_stamp()
    _recompute_totals(report)
    write_json(report_path, report)

    summary["failed"] = summary["needs_human"]
    summary["status"] = report["status"]
    return summary
