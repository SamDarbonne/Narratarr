"""Homograph audit: find, decide, force, and check English heteronym readings.

A heteronym is one spelling with two pronunciations chosen by grammar or
sense: "wound" (an injury, or the past tense of "wind"), "read", "live",
"minute", "wind", "bow", "lead", and hundreds more. Kokoro's misaki front
end picks a reading from a part-of-speech tag made by spacy's small
tagger, and the tagger sometimes picks wrong. In the delivered audiobook
of Book A, chunk ch01/0059 holds "a white muffler wound round and
round his neck". The small tagger reads "muffler wound" as a noun
compound, tags "wound" NN, and misaki says the injury /wuːnd/ where the
text needs the past tense of wind /waʊnd/. This module finds every such
error from text alone, decides the right reading, and writes the markup
that forces it. CONTRACT.md does not yet document this module; the
design decisions below are recorded here until a CONTRACT.md section
exists.

Design decisions, with the reason for each:

1. **The forcing mechanism is inline phoneme markup, not an engine
   change.** misaki natively reads `[word](/phonemes/)` and sets that
   token's phonemes directly (`LINK_REGEX` in `misaki/en.py`). A real
   render confirmed the markup is consumed, not spoken, and that no
   bracket or slash character leaks into the phoneme output. See
   `apply_homographs` below.

2. **Decisions live in `work/<slug>/homographs.json`, not in `book.json`
   or the source config.** This keeps the mechanism out of every other
   worker's file. `render.py` reads this file once per run and applies
   `apply_homographs` to the in-memory chunk text, the same way it
   already applies the pronunciation map — the on-disk chunk text never
   changes, so QC (which compares against the on-disk text) stays blind
   to the markup by construction.

3. **Freshness folds in per chunk, not globally.** `chunk_input_hash`
   returns the bare chunk sha256 when a chunk has no decisions -- so
   every WAV rendered before this module existed stays fresh. Only a
   chunk that gains a decision goes stale. Folding decisions into the
   stage's global `config_hash` instead (the way the pronunciation map
   works) would stale every chunk of the book on any single decision
   change; see `chunk_input_hash` for the full argument.

4. **A machine decision is regenerated every run; a human decision never
   is.** `run()` recomputes every non-human decision from scratch each
   time it is called with `write=True`, so a decision file always
   reflects the current inventory and tier logic. A decision a person
   wrote or edited (`"human": true`) is never touched, so a human
   correction survives forever.

   This is also why `conflict` status (an existing decision whose
   phonemes differ from the current tier verdict) applies to a HUMAN
   decision only. A human decision is authoritative and never rewritten,
   so a difference from the verdict is a genuine contradiction that needs
   a person to look at -- it blocks the gate, and `write=True` leaves it
   on disk exactly as found. A machine decision is disposable by design:
   a difference from the verdict is not a conflict at all, just an
   out-of-date value about to be replaced on the next `--write`, so it is
   never escalated and never blocks -- the occurrence is classified as if
   that stale machine value were not on file (`agree`/`disagree`/`fixed`,
   whichever the baseline comparison gives), and `force_this` in `run()`'s
   loop regenerates it normally.

5. **This module stays cheap to import.** Detection, markup, hashing, and
   the decision file are pure-Python and carry no dependency on spacy or
   misaki. Only the audit itself (`run()`) needs those, and it imports
   `abpipe.homograph_tiers` lazily, inside the function body -- the same
   pattern `qc.py` uses for `render.py`. That keeps `import abpipe.homographs`
   free of spacy and misaki, so `render.py` (which must call
   `apply_homographs` on every rendered chunk) can import this module
   without paying for a tagger or a G2P model it never uses, and the pure
   parts of this module's test suite stay fast.

6. **The baseline never applies an existing decision, on purpose --
   "already fixed" is a presentational status layered on top, not a
   change to what gets compared.** The baseline answers one question only:
   what would misaki say completely unaided. If a decision were folded
   into the baseline instead, a correct decision would erase the very
   disagreement that justified writing it -- the next machine regeneration
   (`_merge_decisions`) would then see no disagreement, drop the decision,
   and the fix would vanish on the next `--write`. That self-erasing loop
   is why an occurrence with a matching decision on file is reported as
   `fixed` (see `run()`'s per-occurrence loop below) rather than folded
   into `agree`: the underlying engine is still wrong without the
   decision, and the decision has to keep existing to keep it right.

7. **A human decision can record that a person already rejected an
   alternative reading, so the conflict gate does not ask the same
   question on every future audit.** `adjudicated_against` is an optional
   list of phoneme strings on a `human: true` decision. `run()` compares
   the current verdict's phonemes against that list, normalised through
   `engine_phonemes()` then `strip_stress()` on both sides (see `run()`'s
   per-occurrence loop), BEFORE it ever reports `conflict`. A verdict
   already named in the list reports `fixed`, not `conflict`, and never
   blocks the gate: a person has
   already looked at exactly that reading and rejected it, and a gate
   that stays red for a resolved reason is a gate people learn to ignore.
   A verdict NOT in the list is still a genuine, un-reviewed contradiction
   and still reports `conflict` -- this field narrows what counts as a
   conflict, it does not turn the check off. `adjudicate()` below is the
   one sanctioned way to grow the list; it raises rather than let a
   caller adjudicate a machine decision, which has no standing to be
   adjudicated at all (point 4 above -- a machine decision is disposable
   and regenerated every run, so there is nothing durable to adjudicate).
   `adjudicated_against` never changes what gets synthesized, so
   `chunk_input_hash` excludes it from `_AUDIO_AFFECTING_FIELDS`, exactly
   like `decided_by` and `context`.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from abpipe.context import Context
from abpipe.meta import hash_many, hash_obj, read_json, write_json

# --------------------------------------------------------------------------- constants

# Resolved from __file__, not the current working directory, so this module
# works the same way whether it is run as `python -m abpipe.homographs` from
# the repo root or imported from a test that runs from anywhere.
INVENTORY_PATH = Path(__file__).resolve().parent / "data" / "heteronyms.json"

# Class A: a gross vowel difference (wound, wind, minute, read, live, ...) --
# a wrong choice is an audible howler. Class B: a voicing / final-consonant
# difference (house, use, close, ...) -- audible but subtle. Class C:
# stress-only (record, present, ...) -- least audible. The maintainer's
# decision (plan section 5, Q2): force class A only by default; report B and C so a
# human can opt in.
SEVERITY_CLASSES = ("A", "B", "C")
DEFAULT_FORCE_CLASSES = ("A",)

# The markup this module writes is `[surface](/phonemes/)`. Any of these
# characters inside a phoneme string would break that markup -- a stray `]`
# or `)` could close the token early, and a stray `/` could start a second
# one. apply_homographs and validate() both reject a phoneme string that
# holds any of them, before either one is written or applied.
_MARKUP_CHARS = frozenset("[]()/")

# Misaki marks stress with these two characters (misaki/en.py `STRESSES`):
# primary ˈ (U+02C8) and secondary ˌ (U+02CC). strip_stress() removes both.
_STRESS_MARKS = "ˌˈ"

# The fields of a decision dict that can change what the engine says.
# chunk_input_hash hashes only these -- see that function's docstring for
# why the rest (reading, decided_by, misaki_baseline, class, context,
# human, adjudicated_against, and any future bookkeeping field) are
# excluded on purpose.
_AUDIO_AFFECTING_FIELDS = ("word", "occurrence", "phonemes")


class HomographError(Exception):
    """A homograph decision, or a chunk's occurrences, cannot be trusted.

    Raised for a decision that names an occurrence larger than the count
    of matches actually found in the text (the chunk changed under a
    stale decision file), for two decisions naming the same word and
    occurrence, or for phonemes that hold a markup character. Every one of
    these is a case where a silent miss would be worse than a loud
    failure -- the audit exists to catch a wrong reading nobody notices,
    so this module never guesses past a fault it can name.
    """


# --------------------------------------------------------------------------- inventory


def load_inventory(path: str | os.PathLike | None = None) -> dict[str, dict]:
    """Return the `entries` object of heteronyms.json, keyed by the
    lower-case word.

    `path` defaults to INVENTORY_PATH (abpipe/data/heteronyms.json,
    resolved from __file__). A test, or a caller auditing a book before
    Worker H2's generator has run, passes an explicit path to a scratch
    inventory of the same shape instead.
    """
    inventory_path = Path(path) if path is not None else INVENTORY_PATH
    data = read_json(inventory_path)
    if not isinstance(data, dict):
        raise HomographError(f"heteronym inventory missing or unreadable: {inventory_path}")
    entries = data.get("entries")
    if not isinstance(entries, dict):
        raise HomographError(f"heteronym inventory at {inventory_path} holds no 'entries' object")
    return {str(word).lower(): entry for word, entry in entries.items()}


def dialect_for_lang_code(lang_code: str) -> str:
    """Return "gb" for lang_code "b", "us" for "a". Any other value returns "us".

    book.json's engine.lang_code names the misaki dialect Kokoro uses
    (CONTRACT.md 4, engine block). "gb" and "us" heteronym entries hold
    different phonemes for the same word (the plan's worked example:
    wound's noun reading is wˈuːnd in gb, wˈund in us), so the audit and
    the render seam both need this mapping to pick the right side of the
    inventory's `readings` object.
    """
    return "gb" if lang_code == "b" else "us"


# --------------------------------------------------------------------------- occurrences


@dataclass(frozen=True)
class Occurrence:
    """One inventory word found once in one chunk's text."""

    chapter: str  # "ch01"
    chunk: str  # "0059" (the chunk id, a string, zero padded)
    word: str  # the lower-case inventory key, "wound"
    surface: str  # the text as it is written, "Wound" or "wound"
    occurrence: int  # 1-based count of this word inside this chunk text
    start: int  # the character offset in the chunk text
    end: int
    context: str  # the sentence that holds the occurrence
    severity: str  # "A", "B" or "C", copied from the inventory entry

    @property
    def key(self) -> tuple:
        """The identity of this occurrence, stable across a re-scan of the
        same chunk text. Used as a dict key everywhere a caller needs to
        join an occurrence to a baseline phoneme string or a tier verdict."""
        return (self.chapter, self.chunk, self.word, self.occurrence)


@lru_cache(maxsize=None)
def _word_pattern(word: str) -> re.Pattern[str]:
    """Compile the one pattern both find_occurrences and apply_homographs
    use to locate a word: \\b-bounded (so "wound" never matches inside
    "wounded", "wounds", or "rewound") and case-insensitive (so "Wound" at
    a sentence start still matches the lower-case inventory key). \\b
    treats an apostrophe as a boundary, so "wound's" matches "wound" on
    purpose -- the possessive is the same reading as the plain word.

    Cached without a size limit: re's own internal compile cache holds only
    512 entries, and the inventory already holds several hundred words, so
    a book with more than one chapter would otherwise recompile the same
    pattern on every chunk once the inventory outgrows that cache. There
    are at most a few thousand inventory words ever, so an unbounded cache
    here costs nothing that matters.
    """
    return re.compile(r"\b" + re.escape(word) + r"\b", re.IGNORECASE)


def _iter_matches(word: str, text: str):
    """Yield every match of `word` in `text`, left to right.

    The single shared source of match iteration: find_occurrences numbers
    occurrences by enumerating this, and apply_homographs indexes into
    `list(_iter_matches(...))` by the same 1-based rule, so an occurrence
    number found by one function always names the same match to the
    other. Do not write this regex a second time anywhere in this module.
    """
    return _word_pattern(word).finditer(text)


def count_matches(word: str, text: str) -> int:
    """Return how many times `word` occurs in `text`, by the same rule
    apply_homographs and find_occurrences use.

    Public because qc.py's remediation ladder (CONTRACT.md 9.3) splits a
    chunk in two and must re-index each half's occurrence numbers against
    the whole chunk's numbering. That code needs this module's exact match
    rule, and a second copy of the regex over there would be free to drift
    from this one.
    """
    return sum(1 for _ in _iter_matches(word, text))


_SENTENCE_END = re.compile(r"[.!?]")


def _sentence_context(text: str, start: int, end: int) -> str:
    """Return the sentence that holds text[start:end].

    A cheap heuristic -- the nearest sentence-ending punctuation on each
    side -- not a parser. This is good enough for a human reading the
    audit report or a decision file's `context` field; a mis-drawn
    boundary (an abbreviation's period, for example) never affects any
    decision, only the printed and recorded snippet.
    """
    left = 0
    for m in _SENTENCE_END.finditer(text, 0, start):
        left = m.end()
    right_match = _SENTENCE_END.search(text, end)
    right = right_match.end() if right_match else len(text)
    return text[left:right].strip()


def find_occurrences(
    text: str, inventory: dict, chapter: str = "", chunk: str = ""
) -> list[Occurrence]:
    """Find every inventory word in the text. Ordered by `start`.

    Occurrence numbers are 1-based per word, counted by the same
    left-to-right scan apply_homographs uses (_iter_matches), so an
    occurrence number found here round-trips into a decision that
    apply_homographs can act on.
    """
    found: list[Occurrence] = []
    for word, entry in inventory.items():
        entry = entry if isinstance(entry, dict) else {}
        severity = str(entry.get("class") or "C")
        count = 0
        for m in _iter_matches(word, text):
            count += 1
            found.append(
                Occurrence(
                    chapter=chapter,
                    chunk=chunk,
                    word=word,
                    surface=m.group(0),
                    occurrence=count,
                    start=m.start(),
                    end=m.end(),
                    context=_sentence_context(text, m.start(), m.end()),
                    severity=severity,
                )
            )
    found.sort(key=lambda o: o.start)
    return found


# --------------------------------------------------------------------------- markup


def _check_phonemes(phonemes: str, word: str) -> None:
    bad = _MARKUP_CHARS.intersection(phonemes)
    if bad:
        raise HomographError(
            f"phonemes for {word!r} hold markup character(s) {''.join(sorted(bad))!r}: "
            f"{phonemes!r}"
        )


# Two of the books being audited carry editorial square brackets in their
# chunk text -- translator glosses ("arepas [thick maize tortillas]") and
# "[sic]" markers. misaki's own LINK_REGEX (misaki/en.py:
# `r'\[([^\]]+)\]\(([^\)]*)\)'`) opens on `\[([^\]]+)\]` -- a `[`, one or
# more non-`]` characters, then the FIRST `]`. Writing our own
# `[word](/phonemes/)` markup for a word already sitting inside such a span
# nests a second `[` inside it: LINK_REGEX still matches from the OUTER
# `[`, so everything between the original `[` and our own word is absorbed
# into its first group and silently dropped from the phoneme stream, and
# the surrounding `)` and `/` characters leak in as if they were letters.
# Measured end to end: "They brought arepas [thick maize tortillas], roast
# plantains." with a decision on "tortillas" phonemizes as "...ɑɹˈApəz
# tɔɹtˈijəz), ɹˈOst..." -- "thick maize" is gone and a stray `)` leaked in.
# _BRACKET_SPAN mirrors LINK_REGEX's own bracket half exactly, so this
# module and the regex it defends against can never disagree about what
# counts as "inside a bracket". **This must track misaki/en.py's
# LINK_REGEX if misaki ever changes it** -- re-read that file before
# trusting this comment.
_BRACKET_SPAN = re.compile(r"\[([^\]]+)\]")

# The shared explanation string for every place this fault surfaces: the
# not_applicable row's note (run()), the HomographError apply_homographs
# raises, and the error validate() reports. One string, so a person sees
# the identical reason wherever the fault is caught.
_BRACKET_REASON = (
    "inside an editorial bracket; word-level markup would nest and delete "
    "the surrounding text"
)


def _in_bracket_span(text: str, start: int, end: int) -> bool:
    """Return True when text[start:end] sits strictly inside one of
    text's `[...]` spans -- after that span's opening `[` and before its
    closing `]`, never overlapping either bracket character itself.

    Called with an occurrence's own (start, end) offsets into the exact
    chunk text it was found in. apply_homographs, validate(), and run()
    all call this with the same notion of "inside a bracket" (see
    `_BRACKET_SPAN`'s comment) -- there is no second parser here that
    could disagree with misaki's LINK_REGEX about what a bracket span is.
    """
    for m in _BRACKET_SPAN.finditer(text):
        if m.start() < start and end < m.end():
            return True
    return False


def apply_homographs(text: str, decisions: list[dict] | None) -> str:
    """Return the text with the phoneme markup put in. THIS IS THE FUNCTION
    RENDER CALLS.

    `decisions` is a list of dicts, each with at least `word` (lower
    case), `occurrence` (1-based int), and `phonemes` (str).

    An empty or `None` decision list returns `text` unchanged -- the same
    object, not a copy -- since this is the default for every book and
    every chunk and must cost nothing.

    For a decision naming word W and occurrence N: find the nth
    \\b-bounded, case-insensitive match of W in `text` (the same scan
    find_occurrences uses), and replace it with
    `[<original surface text>](/<phonemes>/)`, keeping the original
    capitalisation of the matched text. misaki reads the phonemes from
    the parentheses; the bracketed text only has to keep the on-disk
    spelling honest for a human reading the marked-up string.

    **Every replacement span is computed against the ORIGINAL text
    offsets, all up front, before any splicing happens; the splices are
    then applied highest offset to lowest, in one pass.** Applying them
    left to right instead would let an earlier insertion's extra
    characters (the added "[](//)" markup is longer than the bare word)
    shift every later match's offset, so a decision naming occurrence 2
    could land on text that used to be occurrence 3 -- the wrong word
    gets marked, silently. Because every span here is located in a single
    read-only pass over the untouched `text` before the first character
    is ever spliced in, that class of bug cannot happen: there is no step
    at which a later lookup could see a mutated string.

    Raises HomographError for: two decisions naming the same
    (word, occurrence); an occurrence number larger than the count of
    matches found (the chunk's text changed under a stale decision file);
    phonemes holding a markup character ([ ] ( ) /); or a decision whose
    target sits inside an existing `[...]` span of `text` (`run()` never
    writes one of these -- see `_BRACKET_SPAN`'s comment -- but a
    hand-edited decisions file must not be able to cause the silent
    deletion this would otherwise produce).
    """
    if not decisions:
        return text

    seen: set[tuple[str, int]] = set()
    by_word: dict[str, list[dict]] = {}
    for decision in decisions:
        word = str(decision["word"]).lower()
        occurrence = int(decision["occurrence"])
        key = (word, occurrence)
        if key in seen:
            raise HomographError(f"duplicate decision for {word!r} occurrence {occurrence}")
        seen.add(key)
        _check_phonemes(str(decision["phonemes"]), word)
        by_word.setdefault(word, []).append(decision)

    replacements: list[tuple[int, int, str]] = []
    for word, word_decisions in by_word.items():
        matches = list(_iter_matches(word, text))
        for decision in word_decisions:
            occurrence = int(decision["occurrence"])
            if occurrence < 1 or occurrence > len(matches):
                raise HomographError(
                    f"decision names {word!r} occurrence {occurrence}, but the text "
                    f"holds only {len(matches)} occurrence(s) of it"
                )
            match = matches[occurrence - 1]
            if _in_bracket_span(text, match.start(), match.end()):
                raise HomographError(
                    f"decision for {word!r} occurrence {occurrence} sits {_BRACKET_REASON}"
                )
            surface = match.group(0)  # preserve the on-disk capitalisation
            phonemes = str(decision["phonemes"])
            replacements.append((match.start(), match.end(), f"[{surface}](/{phonemes}/)"))

    # Highest offset first: each splice below only ever touches text to the
    # right of every splice still to come, so no offset computed above is
    # ever invalidated by an earlier splice in this loop.
    replacements.sort(key=lambda r: r[0], reverse=True)
    result = text
    for start, end, replacement in replacements:
        result = result[:start] + replacement + result[end:]
    return result


def strip_stress(phonemes: str) -> str:
    """Return `phonemes` with primary (ˈ) and secondary (ˌ) stress marks
    removed.

    run() uses this to compare a tier verdict's phonemes against misaki's
    baseline on vowel content only, so a stress-only difference (class C:
    record, present, perfect, ...) never reads as a disagreement for the
    words this audit cares most about. Public because Worker H3's tier
    module needs the identical normalisation -- comparing with two
    slightly different stress strippers would let the audit and the tier
    disambiguator disagree about what "the same reading" means.
    """
    if not phonemes:
        return phonemes
    return "".join(ch for ch in phonemes if ch not in _STRESS_MARKS)


def engine_phonemes(phonemes: str) -> str:
    """Return `phonemes` as the engine emits them, not as the lexicon
    stores them.

    `misaki/en.py`'s `G2P.__call__` (this venv, `misaki` package, near
    the end of the method) rewrites every token's phonemes on the way
    out, unconditionally for any `self.version != '2.0'`:
    `tk.phonemes = tk.phonemes.replace('ɾ', 'T').replace('ʔ', 't')`. The
    us lexicon (`us_gold.json`) stores the PRE-transform spelling (`ɾ`),
    but a real render, and misaki's own in-context baseline, always goes
    through `G2P.__call__` and so only ever emits the POST-transform
    spelling (`T`). Comparing a lexicon-derived string against a baseline
    or verdict string without this transform mis-compares two spellings
    of the identical sound -- measured on a real book, ~9,384 entries of
    the us lexicon contain `ɾ` and zero contain `T`, so this is not an
    edge case.

    **This function must track `misaki/en.py`'s `G2P.__call__` if misaki
    ever changes it** -- re-read that file before trusting this comment,
    the same tracking note `homograph_tiers.py`'s G2P construction
    carries for `pipeline.py`.

    The transform is not dialect-guarded in misaki itself, so this
    function is not either -- it is applied unconditionally, mirroring
    misaki exactly, regardless of British or American lang_code.
    """
    if not phonemes:
        return phonemes
    return phonemes.replace("ɾ", "T").replace("ʔ", "t")


def _comparable(phonemes: str) -> str:
    """Return `phonemes` normalised for an equality comparison: engine
    representation first (`engine_phonemes`), then stress stripped
    (`strip_stress`).

    Every phoneme comparison in run() goes through this one helper, on
    both sides, so there is exactly one normalisation rule -- a call
    site that used `strip_stress()` alone would still mis-compare a
    lexicon-spelled `ɾ` against an engine-spelled `T` (see
    `engine_phonemes`'s docstring), so `strip_stress()` alone is never
    enough for a baseline, a verdict, or a decision's phonemes.
    """
    return strip_stress(engine_phonemes(phonemes))


# --------------------------------------------------------------------------- decisions document


def _decisions_path(book_dir: str | os.PathLike) -> Path:
    return Path(book_dir) / "homographs.json"


def read_decisions(book_dir: str | os.PathLike) -> dict:
    """Return the parsed work/<slug>/homographs.json, or an empty document
    when it is absent.

    An absent file is the default state of every book that has never had
    the audit run with --write: this returns a well-formed empty document
    rather than None, so a caller never has to special-case "the file
    does not exist yet" before reading `doc["decisions"]`.
    """
    data = read_json(_decisions_path(book_dir))
    if not isinstance(data, dict) or not isinstance(data.get("decisions"), list):
        return {"schema": 1, "decisions": []}
    return data


def write_decisions(book_dir: str | os.PathLike, doc: dict) -> None:
    """Write work/<slug>/homographs.json atomically, through meta.write_json.

    `meta.write_json` writes a temp file beside the target and moves it
    into place with `os.replace` (CONTRACT.md 3.3), so a reader (render.py,
    on every run) never sees a half-written decision file.
    """
    write_json(_decisions_path(book_dir), doc)


def decisions_for_chunk(doc: dict, chapter: str, chunk: str) -> list[dict]:
    """Return this chunk's decisions, in a deterministic order.

    Sorted by (word, occurrence) so two callers reading the same document
    always see the same list, regardless of the order json.load happened
    to hand back -- render.py's per-chunk apply_homographs call and this
    module's own chunk_input_hash both need that determinism, the first
    for a stable markup pass and the second because a hash is only useful
    when equal inputs always produce it.
    """
    decisions = doc.get("decisions") or []
    matching = [
        d for d in decisions if d.get("chapter") == chapter and d.get("chunk") == chunk
    ]
    matching.sort(key=lambda d: (str(d.get("word", "")), int(d.get("occurrence", 0) or 0)))
    return matching


def _normalise_decision(decision: dict) -> dict:
    return {field: decision.get(field) for field in _AUDIO_AFFECTING_FIELDS}


def chunk_input_hash(chunk_sha256: str, chunk_decisions: list[dict] | None) -> str:
    """Return the render input_hash for one chunk. THE EMPTY CASE RETURNS
    chunk_sha256.

    The empty case returns the bare `chunk_sha256` -- not a hash of it --
    on purpose. render.py's current input_hash IS `record["sha256"]`, so
    every WAV of every book rendered before this module existed has a
    meta file whose input_hash already equals its chunk's bare sha256.
    Returning anything else here (even hash_many([chunk_sha256])) would
    make every one of those metas mismatch on the very next render run,
    staling all 2,042 chunks of a delivered audiobook for no reason. Only
    a chunk that gains an actual decision goes stale, which is exactly
    the invalidation this function exists to control.

    The non-empty case hashes only `word`, `occurrence`, `phonemes` from
    each decision (via _normalise_decision) -- the three fields that can
    change what the engine says. `reading`, `decided_by`,
    `misaki_baseline`, `class`, `context`, `human`, `adjudicated_against`,
    and any future bookkeeping field are excluded on purpose: re-running
    the tier disambiguator with a new `decided_by` label, a human editing
    a `context` note, or recording an adjudicated reading through
    `adjudicate()`, must not stale a WAV that would render byte-identical
    audio. The decisions are sorted before hashing so two equal decision
    sets in a different order (e.g. after a human edits the file by hand)
    still hash the same.
    """
    if not chunk_decisions:
        return chunk_sha256
    normalised = sorted(
        (_normalise_decision(d) for d in chunk_decisions),
        key=lambda d: (str(d.get("word")), int(d.get("occurrence") or 0)),
    )
    return hash_many([chunk_sha256, hash_obj(normalised)])


def validate(
    doc: dict,
    pronunciations: dict | None,
    inventory: dict,
    texts: dict[tuple[str, str], str] | None = None,
) -> list[str]:
    """Return a list of human-readable error strings. An empty list means
    the document is good.

    Checks, one error string per fault found:
      - a decision names a word absent from the inventory;
      - a decision's phonemes hold a markup character ([ ] ( ) /);
      - two decisions share the same (chapter, chunk, word, occurrence);
      - a decision's word also appears in the book's pronunciation map.
        The pronunciation substitution (render.py's apply_pronunciations)
        runs on the SAME in-memory text apply_homographs already marked
        up, matching \\b-bounded whole words -- a word that is both a
        pronunciation-map key and a homograph decision would have its
        markup's bracketed surface text rewritten mid-bracket, corrupting
        it. Compared case-insensitively: the pronunciation map is
        case-sensitive (it names a proper noun spelled one way), but a
        heteronym key is always lower case, so the comparison lower-cases
        both sides to catch the collision either way.
      - a decision's target sits inside an existing `[...]` span of its
        chunk's text (see `_BRACKET_SPAN`'s comment and
        `apply_homographs`'s matching raise) -- checked only when `texts`
        is given, since this check needs the actual chunk text a
        (chapter, chunk) key names, keyed the same way `run()` builds
        its own `texts` dict. `texts=None` (the default) skips this
        check silently, not an error: most callers validate a decisions
        document on its own, with no chunk text in hand, and this is the
        one check with a real dependency on it.
    """
    errors: list[str] = []
    decisions = doc.get("decisions") or []
    pronunciation_words = {str(k).lower() for k in (pronunciations or {})}
    seen: dict[tuple, int] = {}

    for i, decision in enumerate(decisions):
        word = str(decision.get("word", "")).lower()
        chapter = decision.get("chapter")
        chunk = decision.get("chunk")
        occurrence = decision.get("occurrence")

        if word not in inventory:
            errors.append(f"decision {i}: word {word!r} is not in the heteronym inventory")

        phonemes = str(decision.get("phonemes", ""))
        bad = _MARKUP_CHARS.intersection(phonemes)
        if bad:
            errors.append(
                f"decision {i}: phonemes for {word!r} hold markup character(s) "
                f"{''.join(sorted(bad))!r}"
            )

        if texts is not None and occurrence is not None:
            chunk_text = texts.get((chapter, chunk))
            if chunk_text is not None:
                matches = list(_iter_matches(word, chunk_text))
                occ_num = int(occurrence)
                if 1 <= occ_num <= len(matches):
                    m = matches[occ_num - 1]
                    if _in_bracket_span(chunk_text, m.start(), m.end()):
                        errors.append(
                            f"decision {i}: {word!r} occurrence {occ_num} in "
                            f"chapter={chapter!r} chunk={chunk!r} sits {_BRACKET_REASON}"
                        )

        key = (chapter, chunk, word, occurrence)
        if key in seen:
            errors.append(
                f"decision {i}: duplicate decision for chapter={chapter!r} chunk={chunk!r} "
                f"word={word!r} occurrence={occurrence!r} (first seen at decision {seen[key]})"
            )
        else:
            seen[key] = i

        if word in pronunciation_words:
            errors.append(
                f"decision {i}: word {word!r} is also a key in the book's pronunciation map -- "
                f"the pronunciation substitution would rewrite it inside the homograph markup"
            )

    return errors


def adjudicate(
    book_dir: str | os.PathLike,
    chapter: str,
    chunk: str,
    word: str,
    occurrence: int,
    rejected_phonemes: str,
) -> dict:
    """Record that a person considered `rejected_phonemes` and kept the
    decision. Returns the updated decision.

    This is the one sanctioned way to grow a human decision's
    `adjudicated_against` list (module docstring point 7): loads
    work/<slug>/homographs.json, finds the decision named by (chapter,
    chunk, word, occurrence), appends `rejected_phonemes` to its
    `adjudicated_against` list (creating the list if it is absent), and
    writes the document back atomically through `write_decisions`. A
    reading already in the list, compared with `strip_stress()` so a
    stress-only difference still counts as the same rejected reading, is
    never added twice -- calling this a second time with the identical
    reading is a no-op past the first call, not a growing list of
    duplicates.

    `run()` checks this exact list before ever reporting `conflict` (see
    its per-occurrence loop): once a verdict's phonemes are on file here,
    that verdict reports `fixed`, not `conflict`, and stops blocking the
    gate. This is how a person clears a conflict they have already looked
    at -- without it, an audit re-asks the identical question on every
    future run of this book, and a gate that stays red for a resolved
    reason is a gate people learn to ignore.

    Raises HomographError when no decision matches the four-part key, or
    when the matching decision is not `human: true` -- a machine decision
    is regenerated from scratch every run (module docstring point 4) and
    has no standing to be adjudicated; recording a rejection against it
    would be forgotten on the very next `--write`.
    """
    doc = read_decisions(book_dir)
    decisions = doc.get("decisions") or []
    word_lower = str(word).lower()

    match = None
    for d in decisions:
        if (
            d.get("chapter") == chapter
            and d.get("chunk") == chunk
            and str(d.get("word", "")).lower() == word_lower
            and d.get("occurrence") == occurrence
        ):
            match = d
            break

    if match is None:
        raise HomographError(
            f"no decision on file for {word!r} {chapter}/{chunk}#{occurrence} -- "
            f"nothing to adjudicate"
        )
    if not match.get("human"):
        raise HomographError(
            f"decision for {word!r} {chapter}/{chunk}#{occurrence} is not a human "
            f"decision (human={match.get('human')!r}) -- a machine decision is "
            f"regenerated every run, never adjudicated"
        )

    existing = match.get("adjudicated_against") or []
    already_present = any(
        strip_stress(str(p)) == strip_stress(str(rejected_phonemes)) for p in existing
    )
    if not already_present:
        match["adjudicated_against"] = [*existing, rejected_phonemes]

    write_decisions(book_dir, doc)
    return match


# --------------------------------------------------------------------------- tier seam (lazy)
#
# abpipe/homograph_tiers.py is owned by Worker H3 and is being written at the
# same time as this file. These two wrappers do the actual import lazily,
# inside the function body -- the same pattern qc.py uses for render.py --
# so importing abpipe.homographs never touches spacy or misaki, and a test
# can monkeypatch these two names directly without ever triggering the
# import, even while homograph_tiers.py is mid-edit or briefly broken.


def _baseline_phonemes(text: str, occurrences: list, dialect: str, pronunciations: dict | None):
    from abpipe import homograph_tiers

    return homograph_tiers.baseline_phonemes(text, occurrences, dialect, pronunciations)


def _disambiguate(
    occurrences: list,
    inventory: dict,
    texts: dict,
    dialect: str,
    use_llm: bool,
    review_path,
):
    from abpipe import homograph_tiers

    return homograph_tiers.disambiguate(
        occurrences, inventory, texts, dialect, use_llm=use_llm, review_path=review_path
    )


# --------------------------------------------------------------------------- the audit


def _decision_from_verdict(occ: Occurrence, verdict, baseline_phonemes: str | None) -> dict:
    """Build one machine decision dict, per the schema in this module's
    docstring and the plan's worked example (word "wound", chunk
    ch01/0059)."""
    return {
        "chapter": occ.chapter,
        "chunk": occ.chunk,
        "word": occ.word,
        "occurrence": occ.occurrence,
        "phonemes": verdict.phonemes,
        "reading": verdict.reading,
        "decided_by": verdict.tier,
        "misaki_baseline": baseline_phonemes,
        "class": occ.severity,
        "context": occ.context,
        "human": False,
    }


def _decision_key(decision: dict) -> tuple:
    return (
        decision.get("chapter"),
        decision.get("chunk"),
        str(decision.get("word", "")).lower(),
        decision.get("occurrence"),
    )


def _merge_decisions(existing_doc: dict, machine_decisions: list[dict]) -> dict:
    """Return a new decisions document: every human decision from
    `existing_doc`, plus every freshly computed machine decision whose
    (chapter, chunk, word, occurrence) key a human decision does not
    already claim.

    This is the whole idempotence-without-clobbering rule (module
    docstring, point 4): a machine decision is recomputed from scratch
    every call, so it always reflects the current inventory and tier
    logic, including dropping a machine decision that this run no longer
    finds a disagreement for. A human decision is never recomputed and
    never dropped -- it is carried forward untouched, and it always wins
    a key collision against a fresh machine decision.
    """
    existing = existing_doc.get("decisions") or []
    human = [d for d in existing if d.get("human")]
    human_keys = {_decision_key(d) for d in human}

    merged = list(human)
    for decision in machine_decisions:
        if _decision_key(decision) not in human_keys:
            merged.append(decision)

    merged.sort(key=lambda d: (str(d.get("chapter")), str(d.get("chunk")), str(d.get("word")), d.get("occurrence")))
    return {"schema": 1, "decisions": merged}


_STATUS_DISPLAY = {
    "agree": "agree",
    "disagree": "DISAGREE",
    "fixed": "FIXED",
    "conflict": "CONFLICT",
    "decided": "decided",
    "stale": "stale?",
    "unresolved": "unresolved",
    "not_applicable": "n/a",
}

# THE GATE PREDICATE, stated once and reused everywhere it is explained
# (run()'s docstring, this constant, and the printed report):
#
#   blocking = (
#       (status == "unresolved" and severity in force_classes)
#       or status == "conflict"
#       or status == "stale"
#   )
#
# Two different scopes, both deliberate:
#   - "unresolved" blocks only in a class the caller would ever act on
#     (severity in force_classes, default ("A",) -- the maintainer's "force
#     class A, report B/C"). An unresolved class-C word is real, but blocking on
#     one would make the gate unpassable for no benefit: this book alone
#     holds 303 class-B and 33 class-C occurrences, and a gate nobody can
#     pass gets switched off, protecting nothing. A caller who wants the
#     stricter, every-class gate passes force_classes=("A", "B", "C").
#   - "conflict" and "stale" block at EVERY class, with no force_classes
#     scoping: both mean an existing DECISION is untrustworthy (the
#     current verdict contradicts it, or the tiers can no longer
#     reproduce it), and a decision only exists because a person chose to
#     act on that exact occurrence. The tool owes that person the same
#     care whatever the class -- so, unlike "unresolved", these two are
#     not gated by force_classes at all.
#
# "conflict" and "stale" are also each scoped to one side of the
# human/machine line, and that asymmetry is deliberate (module docstring
# point 4): a HUMAN decision is authoritative and never rewritten, so it
# is the only kind that can be in "conflict" (contradicted by the current
# verdict) or "decided" (the tiers found no verdict at all) -- both are
# about a person's answer, never silently overridden. A MACHINE decision
# is disposable and regenerated every run, so it is the only kind that
# can go "stale" (no verdict this run means nothing can regenerate it,
# which is genuinely suspect); when a machine decision merely disagrees
# with a verdict that DOES exist, that is not a conflict at all, just an
# ordinary update -- see run()'s per-occurrence loop.
_GATE_EXPLANATION = (
    "Gate: green when failed == 0, where an occurrence blocks if it is "
    "`unresolved` in a class you'd force (default: A), OR `conflict` -- a "
    "HUMAN decision the current verdict contradicts (any class), OR "
    "`stale?` -- a MACHINE decision the tiers can no longer reproduce (any "
    "class). `decided`, `fixed`, `agree`, and `not_applicable` never block."
)


def _print_action_block(rows: list[dict]) -> None:
    """Print the short, actionable summary a person reads first: exactly
    what must be done, before the full per-occurrence table (roughly 450
    lines for the whole book). This is the list handed to the maintainer.

    Status "disagree" means genuinely uncovered -- no decision on file
    names this occurrence at all. An occurrence whose existing decision
    already produces the tier verdict's own phonemes is "fixed", not
    "disagree" (module docstring point 6): it is deliberately excluded
    from item 1 below, because item 1 must hold only work still to do.

    Every row already carries `blocking` (computed once in run(), per
    _GATE_EXPLANATION's predicate), so this function never re-derives the
    gate logic -- it only groups the rows already marked blocking by WHY
    they block, because the three reasons need three different human
    actions:
      - unresolved (in a forced class) -> decide it: re-run with the LLM
        tier, or answer the review file.
      - conflict -> read both readings and pick one, then freeze it.
      - stale? -> confirm the machine decision still applies, then
        freeze it.

    Items, in the order a person acts on them:
      1. Every class-A DISAGREE that WILL be forced -- the book's fix list.
         Fixed occurrences are not in this list; when every class-A
         disagreement is already covered, this item says so instead.
      BLOCKING, when any exist: every occurrence blocking the gate,
         grouped by the three reasons above.
      3. A one-line count of class B/C disagreements, reported not forced.
      4. A one-line count of not_applicable occurrences (no markup can
         fix these; the detail column in the table below says why).
      5. A one-line count of unresolved occurrences OUTSIDE a forced class
         (e.g. class C by default) -- real, reported, but does not block.
    """
    forced_a = [r for r in rows if r["status"] == "disagree" and r["forced"] and r["class"] == "A"]
    forced_other = [r for r in rows if r["status"] == "disagree" and r["forced"] and r["class"] != "A"]
    fixed_a = [r for r in rows if r["status"] == "fixed" and r["class"] == "A"]
    blocking_unresolved = [r for r in rows if r["blocking"] and r["status"] == "unresolved"]
    blocking_conflict = [r for r in rows if r["blocking"] and r["status"] == "conflict"]
    blocking_stale = [r for r in rows if r["blocking"] and r["status"] == "stale"]
    blocking_total = blocking_unresolved + blocking_conflict + blocking_stale
    unresolved_not_blocking = [r for r in rows if r["status"] == "unresolved" and not r["blocking"]]
    bc_disagree = [r for r in rows if r["status"] == "disagree" and not r["forced"] and r["class"] in ("B", "C")]
    not_applicable = [r for r in rows if r["status"] == "not_applicable"]

    print("=" * 78)
    print("ACTION")
    print("=" * 78)
    print(f"  {_GATE_EXPLANATION}")

    if not forced_a and not blocking_total:
        if fixed_a:
            print(
                f"\n  green -- all {len(fixed_a)} class-A disagreement(s) are covered by "
                f"existing decisions; nothing new to force."
            )
        else:
            print("\n  green -- nothing to force, and nothing is blocking the gate.")

    if forced_a:
        print(f"\n  1. Class-A fixes to force ({len(forced_a)}) -- this is the book's fix list:")
        for r in forced_a:
            print(
                f"     {r['word']} {r['chapter']}/{r['chunk']}#{r['occurrence']}: "
                f"{r['baseline'] or '-'} -> {r['verdict'] or '-'}  [{r['tier']}]"
            )
            print(f"       {r['context']}")
    elif fixed_a:
        print(f"\n  1. All {len(fixed_a)} class-A disagreement(s) are covered by decisions; nothing new to force.")
    if forced_other:
        # A trap word (missing_from_misaki) forces at any class, per the
        # run() docstring point 6 -- named here, not folded silently into
        # item 1, so item 1 stays exactly "the class-A fix list".
        print(
            f"\n     (also {len(forced_other)} forced trap-word fix(es) outside class A "
            f"-- see the full table)"
        )

    if blocking_total:
        print(f"\n  BLOCKING -- {len(blocking_total)} occurrence(s) hold the gate at red, grouped by why:")
        if blocking_unresolved:
            print(
                f"\n    a. Unresolved, in a forced class ({len(blocking_unresolved)}) -- "
                f"decide it: re-run with the LLM tier, or answer the review file."
            )
            for r in blocking_unresolved:
                print(
                    f"       {r['word']} (class {r['class']}) {r['chapter']}/{r['chunk']}#{r['occurrence']}: "
                    f"{r['context']}"
                )
        if blocking_conflict:
            print(
                f"\n    b. Conflict ({len(blocking_conflict)}) -- read both readings and pick "
                f"one, then freeze it. Already reviewed and reject the verdict's reading? "
                f"Call homographs.adjudicate(book_dir, chapter, chunk, word, occurrence, "
                f"rejected_phonemes) instead of re-freezing -- it records the rejection "
                f"and clears this gate on the next audit:"
            )
            for r in blocking_conflict:
                print(
                    f"       {r['word']} (class {r['class']}) {r['chapter']}/{r['chunk']}#{r['occurrence']}: "
                    f"{r['note']}"
                )
                print(f"         {r['context']}")
        if blocking_stale:
            print(
                f"\n    c. Stale machine decision ({len(blocking_stale)}) -- confirm it still "
                f"applies, then freeze it:"
            )
            for r in blocking_stale:
                print(
                    f"       {r['word']} (class {r['class']}) {r['chapter']}/{r['chunk']}#{r['occurrence']}: "
                    f"{r['note']}"
                )
                print(f"         {r['context']}")

    print(f"\n  3. Class B/C disagreements, reported but not forced: {len(bc_disagree)}")
    print(f"  4. not_applicable (no markup can fix; see the table for why): {len(not_applicable)}")
    print(
        f"  5. Unresolved outside a forced class, reported but not blocking: "
        f"{len(unresolved_not_blocking)}"
    )
    print("=" * 78)
    print()


def _print_report(rows: list[dict], by_class: dict, by_tier: dict) -> None:
    """Print the ACTION block, then a readable full table, a per-class
    summary, and a per-tier coverage count -- kept plain text (no external
    table library) so the report is useful piped to a file or read
    straight off a terminal."""
    _print_action_block(rows)

    header = (
        f"{'chapter':<7} {'chunk':<6} {'word':<14} {'#':>3} {'cls':<4} "
        f"{'tier':<18} {'status':<12} {'baseline':<16} {'verdict':<16} context"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        status_s = _STATUS_DISPLAY.get(row["status"], row["status"])
        context = row["context"][:60]
        # A not_applicable row shows the tier's own explanation (`detail`);
        # a fixed/conflict/decided/stale row shows which decision covers
        # it, whether that decision is human, or how the two sides differ
        # (`note`).
        extra = row.get("note") or (row.get("detail") if row["status"] == "not_applicable" else "")
        if extra:
            context = f"{context}  ({extra[:60]})"
        print(
            f"{row['chapter']:<7} {row['chunk']:<6} {row['word']:<14} {row['occurrence']:>3} "
            f"{row['class']:<4} {row['tier']:<18} {status_s:<12} "
            f"{str(row['baseline'] or '-'): <16} {str(row['verdict'] or '-'): <16} "
            f"{context}"
        )
    print()
    print("-- by class --")
    for cls in SEVERITY_CLASSES:
        c = by_class.get(cls, {})
        print(
            f"  class {cls}: occurrences={c.get('occurrences', 0)} agree={c.get('agree', 0)} "
            f"disagree={c.get('disagree', 0)} fixed={c.get('fixed', 0)} "
            f"conflict={c.get('conflict', 0)} decided={c.get('decided', 0)} "
            f"stale={c.get('stale', 0)} unresolved={c.get('unresolved', 0)} "
            f"not_applicable={c.get('not_applicable', 0)} forced={c.get('forced', 0)}"
        )
    print("-- by tier --")
    for tier in sorted(by_tier):
        print(f"  {tier}: {by_tier[tier]}")


def run(
    ctx: Context,
    chapters: list[str] | None = None,
    force: bool = False,
    write: bool = False,
    force_classes: tuple[str, ...] = DEFAULT_FORCE_CLASSES,
    use_llm: bool = True,
    book_config: dict | None = None,
    **kw: Any,
) -> dict:
    """The audit. CONTRACT.md section 13 signature. Return the summary dict.

    1. Load the inventory. Find every occurrence in every chunk of the
       selected chapters.
    2. Get misaki's in-context baseline for each occurrence -- what the
       engine will say, or said -- from Worker H3's
       abpipe.homograph_tiers.baseline_phonemes, imported lazily (see the
       "tier seam" section above).
    3. Get the tiers' verdict for each occurrence, from
       abpipe.homograph_tiers.disambiguate.
    4. Compare. An occurrence is a disagreement when the verdict's
       phonemes differ from the baseline phonemes, compared with
       engine_phonemes() then strip_stress() applied to both sides (see
       engine_phonemes()'s docstring: a lexicon-derived phoneme string
       and the live engine's baseline use different spellings of the
       same flap/glottal-stop sounds, and engine_phonemes() puts both
       sides into the engine's own spelling before they are compared).
       A verdict whose tier is
       `"not_applicable"` (Worker H3: a heteronym sitting inside a
       compound misaki tokenizes as one unit, e.g. "wind" in
       "wind-swept" -- no markup can address one word inside a token
       misaki never splits) is its own status, never an agreement, a
       disagreement, or an "unresolved" needing a decision: nothing this
       module can do would change it, so it is reported and excluded
       from both `forced` and `failed`. An occurrence sitting inside an
       existing `[...]` span of the chunk text (an editorial gloss, a
       "[sic]" marker) joins the identical `"not_applicable"` status, for
       the identical reason: writing `[word](/phonemes/)` markup inside
       an existing bracket would nest inside it and delete the
       surrounding text (see `_BRACKET_SPAN`'s comment).

       Every resolved occurrence is further split by what
       work/<slug>/homographs.json already holds for that exact occurrence
       (module docstring point 4): a HUMAN decision whose phonemes differ
       from the CURRENT VERDICT is a "conflict" -- checked BEFORE the
       agree/disagree split, so it is surfaced even when that verdict
       happens to already agree with misaki's baseline, because a human's
       contradicted choice does not stop being contradicted just because
       the baseline also disagrees with it. UNLESS the verdict's phonemes
       are already in that human decision's `adjudicated_against` list
       (module docstring point 7) -- then it is "fixed", not "conflict":
       a person has already looked at exactly this reading and rejected
       it, so the gate must not ask the same question on every future
       audit. Call `adjudicate()` to grow that list. A MACHINE decision that
       differs from the verdict is NOT a conflict at all -- point 4's
       "regenerated every run" rule means a stale machine value is
       ordinary, about to be corrected on the next `--write`, so it is
       classified as if it were not on file: "agree" when the verdict
       matches the baseline, otherwise "disagree" (about to be forced,
       below). Once no human conflict applies: no existing decision (or
       an unmatched machine one) with a disagreeing verdict is a genuine,
       uncovered "disagree" (the thing item 1 of the ACTION block lists);
       ANY existing decision (human or machine) whose phonemes already
       equal a disagreeing verdict's is "fixed" (reported, never re-listed
       as work to do). This only affects how the finding is REPORTED -- it
       never changes the baseline or the verdict comparison itself; see
       point 6.

       An occurrence the tiers could NOT resolve at all this run (no
       verdict, or tier == "unresolved") is split the same way, by what is
       already on file: a HUMAN decision has already established the
       reading -- that is the entire purpose of the review-file escalation
       and the "never guess in silence" rule, and re-deriving it with a
       language model is a cross-check, not the authority -- so it is
       "decided", not "unresolved". A MACHINE decision the tiers can no
       longer reproduce is genuinely suspicious (the inventory changed,
       the text changed, or tier 3 wandered), so it is "stale", and unlike
       "decided" it DOES block the gate. No existing decision at all is
       "unresolved", and also blocks.
    5. Print a report: an ACTION block naming exactly what a person must
       do, then the full per-occurrence table, a per-class summary, and a
       per-tier coverage count.
    6. write=True persists decisions for the *forced* occurrences: every
       occurrence that DISAGREES with misaki's baseline, and whose class
       is in `force_classes` (default ("A",)) OR whose word is flagged
       `missing_from_misaki` in the inventory. A missing_from_misaki word
       (misaki's lexicon holds only one reading for it, e.g. "lead"'s
       metal reading /lɛd/ does not exist there) is forced at any class,
       because when it disagrees misaki cannot be right by luck -- the
       reading it wants simply is not in its own lexicon. It is NOT
       forced when it happens to agree: an occurrence can land on the one
       sense misaki's single reading already covers, by accident of what
       the sentence needs, and forcing that writes a decision that
       changes no audio and stales the chunk for nothing. Class B and C
       disagreements of an ordinary (non-trap) word are reported, never
       forced by default. A "conflict" occurrence is NEVER forced, at any
       class: forcing it would mean writing a machine-authored
       replacement for the very human decision the conflict is about, and
       `_merge_decisions` keeps the human original over any colliding
       machine one regardless -- so `write=True` leaves a conflicting
       human decision on disk exactly as it found it, byte for byte, and
       `forced` / `by_class[cls]["forced"]` never count it.

    `force` is accepted for signature conformity with every other stage
    (CONTRACT.md 13), but this stage holds no per-chunk meta of its own to
    skip -- it re-scans every chunk of the selected chapters on every
    call, whether or not `force` is set. It is reserved for a future
    caching layer, not currently a no-op-vs-behaviour switch.

    `book_config` is accepted because the CLI seam (S2, not part of this
    module) passes it to every stage per CONTRACT.md 14's forward design;
    this stage does not yet define any config key of its own, so the
    argument is presently unused. `**kw` swallows anything else the CLI
    passes that this stage does not need.

    THE GATE, IN ONE SENTENCE (see also `_GATE_EXPLANATION`): the audit is
    green when `failed == 0`, where an occurrence BLOCKS exactly when

        (status == "unresolved" and severity in force_classes)
        or status == "conflict"
        or status == "stale"

    Two different scopes, on purpose:

    - `unresolved` blocks ONLY in a class this run would ever act on
      (`severity in force_classes`, default `("A",)` -- the maintainer's
      "force class A, report B/C"). Blocking on every unresolved class-C word would
      make the gate unpassable for no benefit: Book A alone holds
      303 class-B and 33 class-C occurrences, and a gate nobody can pass
      gets switched off, protecting nothing. A caller that wants the
      stricter, every-class gate passes `force_classes=("A", "B", "C")`
      -- an explicit choice, not an accident.
    - `conflict` and `stale` block at EVERY class, with NO force_classes
      scoping. Both mean an existing DECISION is untrustworthy -- the
      current tier verdict contradicts it (`conflict`), or the tiers can
      no longer reproduce it at all (`stale`) -- and a decision only
      exists because a person chose to act on that exact occurrence. The
      tool owes that person the same care whatever the class, so these
      two are never softened by force_classes the way `unresolved` is.

      `conflict` and `stale` also sit on opposite sides of the human/
      machine line, on purpose (module docstring point 4): only a HUMAN
      decision can be `conflict` (a machine decision that disagrees with
      the verdict is ordinary regeneration, not a contradiction -- see
      point 4 of this docstring), and only a MACHINE decision can be
      `stale` (a human decision with no verdict this run is `decided`,
      never `stale`, because the tiers not re-deriving it does not make
      the human's answer any less settled).

    `agree`, `fixed`, `decided`, and `not_applicable` never block, at any
    class: each means somebody -- a person, or an unaided baseline check
    -- has already established the truth for that occurrence, or (for
    `not_applicable`) no decision could ever resolve it in the first
    place, so blocking on it would stop a book forever on a finding no
    one can act on.

    This is the one shape of failure the audit cannot paper over, so it
    is the one that makes `abpipe homographs` a gate: CONTRACT.md's
    "exit 1 when a stage reports a failure" rule then reads it as a hard
    stop, the same shape as QC's gate.

    This also makes the gate's answer independent of which CLI flags a
    run used, for the part that matters: a book whose open questions are
    all frozen into human decisions gives the SAME gate result with or
    without `--no-llm`, because a human decision reads as `decided`
    (never `unresolved`) whether or not this run's tiers could re-derive
    it. A gate that only agreed with itself when someone remembered to
    pass `--no-llm` correctly would be a trap for whoever runs this as a
    later triage step.
    """
    inventory = load_inventory()

    chapter_ids = ctx.chapter_ids(chapters)

    done = 0
    skipped = 0
    all_occurrences: list[Occurrence] = []
    texts: dict[tuple[str, str], str] = {}
    occurrences_by_chunk: dict[tuple[str, str], list[Occurrence]] = {}

    for chapter_id in chapter_ids:
        index_path = ctx.stage_dir("chunk", make=False) / chapter_id / "index.json"
        index = read_json(index_path)
        if not isinstance(index, dict) or "chunks" not in index:
            # A missing or unreadable chunk index means this chapter has
            # not been through stage 3 yet. The audit reports this as a
            # skip, not a raise: an audit run against a book still mid
            # pipeline should still cover every chapter that IS ready,
            # the same tolerance render.py shows a missing index (module
            # docstring point 5's cost/benefit does not apply to a stage
            # that never writes durable output for a chapter it skips).
            print(f"[homographs] skip {chapter_id}: missing or invalid chunk index", flush=True)
            skipped += 1
            continue

        for record in index["chunks"]:
            chunk_id = record["id"]
            chunk_path = ctx.stage_dir("chunk", make=False) / chapter_id / record["file"]
            try:
                text = chunk_path.read_text(encoding="utf-8")
            except OSError as exc:
                print(f"[homographs] skip {chapter_id}/{chunk_id}: {exc}", flush=True)
                skipped += 1
                continue

            key = (chapter_id, chunk_id)
            texts[key] = text
            occs = find_occurrences(text, inventory, chapter=chapter_id, chunk=chunk_id)
            occurrences_by_chunk[key] = occs
            all_occurrences.extend(occs)
            done += 1

    dialect = dialect_for_lang_code(ctx.engine_config.get("lang_code", "b"))
    pronunciations = dict(ctx.book.get("pronunciations") or {})

    # Per-chunk, not one call for the whole book: baseline_phonemes takes
    # one chunk's text and the occurrences found in it (Worker H3's
    # contract), because misaki's baseline is a real in-context
    # phonemization of that chunk, not a lookup against arbitrary text.
    baseline: dict[tuple, str | None] = {}
    for key, occs in occurrences_by_chunk.items():
        if not occs:
            continue
        baseline.update(_baseline_phonemes(texts[key], occs, dialect, pronunciations))

    review_path = ctx.book_dir / "homograph-review.json"
    verdicts = _disambiguate(all_occurrences, inventory, texts, dialect, use_llm, review_path)
    verdict_by_key = {v.occurrence.key: v for v in verdicts}

    def _empty_class_counters() -> dict[str, int]:
        return {
            "occurrences": 0,
            "agree": 0,
            "disagree": 0,
            "fixed": 0,
            "conflict": 0,
            "decided": 0,
            "stale": 0,
            "unresolved": 0,
            "not_applicable": 0,
            "forced": 0,
        }

    # Existing decisions, read once, up front -- needed by every occurrence
    # in the loop below (not only when write=True) so a read-only audit run
    # already shows "fixed" for an occurrence a prior --write already
    # covered. See module docstring point 6: this lookup is used ONLY to
    # label a status for a person to read; it never feeds the baseline or
    # the verdict comparison itself.
    existing_doc = read_decisions(ctx.book_dir)
    existing_by_key: dict[tuple, dict] = {
        _decision_key(d): d for d in (existing_doc.get("decisions") or [])
    }

    by_class: dict[str, dict[str, int]] = {cls: _empty_class_counters() for cls in SEVERITY_CLASSES}
    by_tier: dict[str, int] = {}
    rows: list[dict] = []
    forced_decisions: list[dict] = []
    disagreements = 0  # genuinely UNCOVERED disagreements only -- see below
    fixed_total = 0
    conflict_total = 0
    decided_total = 0
    stale_total = 0
    unresolved = 0
    not_applicable_total = 0
    failed = 0  # occurrences that block the gate -- see the docstring's one-sentence rule

    for occ in all_occurrences:
        cls = occ.severity if occ.severity in SEVERITY_CLASSES else "C"
        by_class.setdefault(cls, _empty_class_counters())
        by_class[cls]["occurrences"] += 1

        verdict = verdict_by_key.get(occ.key)
        tier = verdict.tier if verdict is not None else "unresolved"
        by_tier[tier] = by_tier.get(tier, 0) + 1

        baseline_ph = baseline.get(occ.key)
        entry = inventory.get(occ.word) or {}
        missing_from_misaki = bool(entry.get("missing_from_misaki"))

        # An occurrence whose own span sits inside a PRE-EXISTING `[...]`
        # span of the chunk text (an editorial gloss, a "[sic]") is also
        # not_applicable, for the same reason as the compound case below:
        # no per-word markup can safely reach it (see `_BRACKET_SPAN`'s
        # comment for why writing markup here would delete text instead).
        # Checked here, against the real chunk text, not left to
        # apply_homographs's raise alone -- so a book with editorial
        # brackets never even queues a forced decision that
        # apply_homographs would refuse at render time anyway.
        in_bracket = _in_bracket_span(texts[(occ.chapter, occ.chunk)], occ.start, occ.end)

        # A `not_applicable` verdict (Worker H3: the word sits inside a
        # compound misaki tokenizes as one unit, so no per-word markup can
        # reach it) is its own status -- checked before "resolved" so it
        # never falls into "unresolved" (which would wrongly grow the
        # tier-3 queue) or into "failed" (which would wrongly block the
        # gate on a finding nothing can act on). An in-bracket occurrence
        # joins the identical bucket, for the identical reason.
        is_not_applicable = in_bracket or (verdict is not None and tier == "not_applicable")
        resolved = (
            verdict is not None
            and verdict.phonemes is not None
            and tier not in ("unresolved", "not_applicable")
            and not in_bracket
        )

        status: str
        agree: bool | None
        note = ""
        is_adjudicated_fixed = False  # see force_this below -- this is also a
        # human decision that must never be silently force-regenerated
        if is_not_applicable:
            status = "not_applicable"
            agree = None
            not_applicable_total += 1
            by_class[cls]["not_applicable"] += 1
            if in_bracket:
                note = _BRACKET_REASON
        elif not resolved:
            # The tiers produced no usable verdict this run. Before calling
            # that "unresolved", check whether a decision on file already
            # settled it (module docstring point 6a, the gate rule):
            #   - a HUMAN decision has established the reading -- that is
            #     the entire purpose of the review-file escalation and the
            #     "never guess in silence" rule. Re-deriving it with a
            #     language model is a cross-check, not the authority, so a
            #     run that cannot re-derive it (e.g. --no-llm) must not
            #     read a settled human answer as an open question. Status
            #     "decided": visible in the table, never unresolved, never
            #     blocks the gate.
            #   - a MACHINE decision the tiers can no longer reproduce is
            #     genuinely suspicious -- the inventory changed, the text
            #     changed, or tier 3 wandered. Nothing independently backs
            #     it any more. Status "stale": visible, and it DOES block,
            #     because an unverifiable machine decision is exactly the
            #     thing that should stop a render.
            existing_decision = existing_by_key.get(occ.key)
            if existing_decision is not None and existing_decision.get("human"):
                status = "decided"
                agree = None
                decided_total += 1
                by_class[cls]["decided"] += 1
                note = "decided by a human; the tiers could not re-derive it"
            elif existing_decision is not None:
                status = "stale"
                agree = None
                stale_total += 1
                by_class[cls]["stale"] += 1
                note = (
                    f"a machine decision (phonemes={existing_decision.get('phonemes')!r}) the "
                    f"tiers can no longer reproduce -- re-run with the LLM tier, or review and "
                    f"freeze it"
                )
            else:
                status = "unresolved"
                agree = None
                unresolved += 1
                by_class[cls]["unresolved"] += 1
        else:
            # baseline_ph is None only when misaki produced no phonemes at
            # all for this token -- treated as a disagreement, since
            # "unknown" can never count as "the same reading". Both sides
            # go through _comparable() (engine_phonemes() then
            # strip_stress()), not strip_stress() alone -- baseline_ph
            # comes from the live engine (post-transform, e.g. `T`), while
            # a lexicon-derived phoneme string is pre-transform (`ɾ`); see
            # engine_phonemes()'s docstring for the mis-compare this
            # avoids.
            baseline_agree = baseline_ph is not None and _comparable(verdict.phonemes) == _comparable(
                baseline_ph
            )
            existing_decision = existing_by_key.get(occ.key)
            existing_is_human = existing_decision is not None and bool(existing_decision.get("human"))
            existing_matches_verdict = existing_decision is not None and _comparable(
                str(existing_decision.get("phonemes", ""))
            ) == _comparable(verdict.phonemes)

            # CONFLICT applies to a HUMAN decision only, and is checked
            # BEFORE the agree/disagree split below -- deliberately, so a
            # human decision that contradicts the current verdict is
            # surfaced even when that verdict happens to already agree
            # with misaki's unaided baseline (that agreement does not make
            # the human's contradicting choice any less contradicted).
            #
            # A MACHINE decision that disagrees with the verdict is NOT a
            # conflict, on purpose -- this is the asymmetry module
            # docstring point 4 already states: "a machine decision is
            # regenerated every run; a human decision never is." A human
            # decision is authoritative, so a difference is a genuine
            # contradiction that needs a person. A machine decision is
            # disposable, so a difference is just an update -- it falls
            # through to the ordinary agree/disagree/fixed classification
            # below AS IF it were not on file at all, because after the
            # next --write it will not be: force_this (below) regenerates
            # it to the verdict's own phonemes.

            # A human decision may carry `adjudicated_against`: phonemes a
            # person has already considered and rejected for this exact
            # occurrence (module docstring point 7). Checked ONLY when the
            # decision is human and differs from the verdict -- the same
            # gate the conflict check below applies -- so a verdict phoneme
            # string already on that list means a person has already
            # answered this exact question. That turns what would be a
            # conflict into "fixed" instead: reported, named, never
            # blocking, and never re-asked. A verdict NOT on the list is
            # still a genuine, un-reviewed contradiction and still reports
            # "conflict" -- this narrows the check, it does not disable it.
            already_adjudicated = False
            if existing_is_human and not existing_matches_verdict:
                rejected = existing_decision.get("adjudicated_against") or []
                already_adjudicated = any(
                    _comparable(str(p)) == _comparable(verdict.phonemes) for p in rejected
                )

            if existing_is_human and not existing_matches_verdict and already_adjudicated:
                status = "fixed"
                agree = False
                is_adjudicated_fixed = True
                fixed_total += 1
                by_class[cls]["fixed"] += 1
                note = (
                    f"a person already considered and rejected phonemes="
                    f"{verdict.phonemes!r} for this occurrence (adjudicated_against); "
                    f"the human decision phonemes={existing_decision.get('phonemes')!r} stands"
                )
            elif existing_is_human and not existing_matches_verdict:
                status = "conflict"
                agree = False
                conflict_total += 1
                by_class[cls]["conflict"] += 1
                note = (
                    f"existing HUMAN decision phonemes={existing_decision.get('phonemes')!r} "
                    f"vs current verdict phonemes={verdict.phonemes!r} [{tier}]"
                    + (" (verdict agrees with misaki's baseline)" if baseline_agree else "")
                    + f" -- already reviewed and reject this reading? call "
                      f"homographs.adjudicate(book_dir, {occ.chapter!r}, {occ.chunk!r}, "
                      f"{occ.word!r}, {occ.occurrence}, {verdict.phonemes!r}) to record "
                      f"it and clear this gate"
                )
            elif baseline_agree:
                status = "agree"
                agree = True
                by_class[cls]["agree"] += 1
            elif existing_decision is not None and existing_matches_verdict:
                # Baseline disagrees, and SOME existing decision (human or
                # machine) already carries the verdict's own phonemes --
                # already covered, nothing new to do.
                status = "fixed"
                agree = False
                fixed_total += 1
                by_class[cls]["fixed"] += 1
                note = f"covered by existing decision (human={existing_is_human})"
            else:
                # Baseline disagrees, and either no existing decision at
                # all, or a MACHINE decision that does not yet match the
                # verdict (ordinary staleness, about to be corrected by
                # force_this below, not a conflict -- see above).
                status = "disagree"
                agree = False
                disagreements += 1
                by_class[cls]["disagree"] += 1

        # THE GATE PREDICATE -- the single place that decides whether an
        # occurrence blocks. Three reasons, two different scopes, on
        # purpose (see run()'s docstring for the full argument):
        #   - "unresolved" blocks only in a class we would ever act on
        #     (severity in force_classes, default ("A",) i.e. the
        #     maintainer's "force class A, report B/C"). Blocking on every unresolved class-C
        #     word would make the gate unpassable for no benefit -- this
        #     book alone holds 303 class-B and 33 class-C occurrences --
        #     and a gate nobody can pass gets switched off, protecting
        #     nothing. A caller who wants the stricter, every-class gate
        #     passes force_classes=("A", "B", "C") -- an explicit choice,
        #     not an accident.
        #   - "conflict" and "stale" block at EVERY class, regardless of
        #     force_classes: both mean an existing DECISION is
        #     untrustworthy (contradicted by the current verdict, or no
        #     longer reproducible), and a decision only exists because a
        #     person chose to act on that occurrence. The tool owes that
        #     person the same care whatever the class.
        blocking = (
            (status == "unresolved" and cls in force_classes)
            or status == "conflict"
            or status == "stale"
        )
        if blocking:
            failed += 1

        # A trap word (missing_from_misaki) is forced at any class, but
        # only when it actually disagrees -- an occurrence can land on the
        # one sense misaki's single reading already covers, and forcing
        # that would write a decision that changes no audio and stales
        # the chunk for nothing (see the docstring above, point 6).
        #
        # `status != "conflict"` and `not is_adjudicated_fixed` are both
        # load-bearing: a conflict, and a "fixed" that is fixed only
        # because a person already adjudicated it (module docstring point
        # 7), are each an existing HUMAN decision the tool must never
        # overwrite or silently resolve either way (see the branches
        # above). Without these guards, force_this would still fire (agree
        # is False for both) and add a machine-authored replacement
        # decision to forced_decisions -- harmless in effect, since
        # _merge_decisions always keeps the human original over any
        # colliding machine decision, but it would still inflate
        # `forced`/by_class[...]["forced"] and misreport that this
        # occurrence was acted on, when nothing was written for it at all.
        force_this = (
            resolved
            and not agree
            and status != "conflict"
            and not is_adjudicated_fixed
            and (cls in force_classes or missing_from_misaki)
        )
        if force_this:
            forced_decisions.append(_decision_from_verdict(occ, verdict, baseline_ph))
            by_class[cls]["forced"] += 1

        rows.append(
            {
                "chapter": occ.chapter,
                "chunk": occ.chunk,
                "word": occ.word,
                "occurrence": occ.occurrence,
                "class": cls,
                "tier": tier,
                "status": status,
                "blocking": blocking,
                "forced": force_this,
                "baseline": baseline_ph,
                "verdict": verdict.phonemes if verdict is not None else None,
                "context": occ.context,
                "detail": verdict.detail if verdict is not None else "",
                "note": note,
            }
        )

    _print_report(rows, by_class, by_tier)

    if write:
        # Reuse the same read taken before the loop (not a fresh
        # read_decisions() call) -- one read for the whole run means the
        # "fixed"/"conflict" labels above and the merge below always agree
        # about what was on disk, even if something else touched the file
        # mid-run.
        merged = _merge_decisions(existing_doc, forced_decisions)
        problems = validate(merged, pronunciations, inventory, texts=texts)
        if problems:
            print("[homographs] validation warnings before write:", flush=True)
            for problem in problems:
                print(f"  - {problem}", flush=True)
        write_decisions(ctx.book_dir, merged)
        print(
            f"[homographs] wrote {len(merged['decisions'])} decision(s) to "
            f"{_decisions_path(ctx.book_dir)}",
            flush=True,
        )

    return {
        "stage": "homographs",
        "done": done,
        "skipped": skipped,
        "failed": failed,
        "occurrences": len(all_occurrences),
        "disagreements": disagreements,
        "forced": len(forced_decisions),
        "unresolved": unresolved,
        "not_applicable": not_applicable_total,
        "fixed": fixed_total,
        "conflicts": conflict_total,
        "decided": decided_total,
        "stale": stale_total,
        "by_class": by_class,
        "by_tier": by_tier,
    }


# --------------------------------------------------------------------------- __main__


def _context_from_args(args: argparse.Namespace) -> Context:
    """Build a Context the same way cli.py's _build_context does: an unset
    flag keeps Context's own default rather than overriding it with None.
    Kept as a private copy, not an import from abpipe.cli, so this module
    never has to import cli.py -- cli.py is a shared seam under active
    edit by another worker, and `python -m abpipe.homographs` must work
    with no change to it at all.
    """
    kwargs: dict[str, Any] = {}
    if args.book:
        kwargs["epub"] = args.book
    if args.slug:
        kwargs["slug"] = args.slug
    return Context(**kwargs)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m abpipe.homographs",
        description="Audit a book's heteronym readings against misaki's in-context baseline.",
    )
    parser.add_argument("--book", dest="book", default=None, help="the source EPUB path")
    parser.add_argument("--slug", dest="slug", default=None, help="the book slug (work/<slug>)")
    parser.add_argument(
        "--chapter",
        dest="chapter",
        action="append",
        default=None,
        help="restrict the audit to one chapter id; repeat for more than one",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="persist forced decisions to work/<slug>/homographs.json",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="skip the tier-3 LLM call; unresolved occurrences go to the human review file",
    )
    return parser.parse_args(argv)


def _main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    ctx = _context_from_args(args)
    summary = run(ctx, chapters=args.chapter, write=args.write, use_llm=not args.no_llm)
    print(
        f"[homographs] done={summary['done']} skipped={summary['skipped']} "
        f"failed={summary['failed']} occurrences={summary['occurrences']} "
        f"disagreements={summary['disagreements']} forced={summary['forced']} "
        f"fixed={summary['fixed']} conflicts={summary['conflicts']} "
        f"decided={summary['decided']} stale={summary['stale']} "
        f"unresolved={summary['unresolved']} not_applicable={summary['not_applicable']}",
        flush=True,
    )
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(_main())
