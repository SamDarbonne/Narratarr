"""The tier disambiguator for `abpipe.homographs`.

Answers two independent questions about one heteronym occurrence in a
chunk of book text:

1. **What will the engine say?** `baseline_phonemes` / `baseline_details`
   phonemize the chunk with the exact misaki configuration the real render
   builds. That configuration is not just `misaki.en.G2P(british=...)`:
   `mlx_audio/tts/models/kokoro/pipeline.py` lines 144-154 (this venv,
   `mlx_audio` package) also wire in a `misaki.espeak.EspeakFallback` for
   the same dialect and set `unk=""`, so a word outside misaki's own
   lexicon is phonemized by espeak instead of silently dropped. `_get_g2p`
   below builds the identical four-part construction --
   `trf=False, british=..., fallback=EspeakFallback(british=...), unk=""`
   -- for this exact reason: omitting the fallback or the `unk` value
   would make this module's baseline diverge from the real render on
   every out-of-lexicon word, which is precisely the case this baseline
   exists to catch. **This construction must track `pipeline.py`'s
   `KokoroPipeline.__init__` (lines 144-154) if mlx-audio ever changes
   it** -- re-read that file before trusting this comment. The
   pronunciation map is applied first, ahead of all of this.

   **Kokoro's acoustic model is NOT deterministic.** Two renders of one
   text give different bytes, measured on this project (CONTRACT.md 9.3).
   That does not weaken this function, and the reason is worth stating
   exactly, because the earlier version of this comment got it wrong.

   The acoustic model consumes ONLY the phoneme string, and the front end
   that makes that string IS deterministic. So this function reproduces
   **which word the engine was told to say**. That is the only thing a
   heteronym decision controls. The waveform varies; the reading does
   not. This is ground truth for the reading in the delivered audio, and
   it says nothing about the bytes.
2. **What is correct?** `disambiguate` runs three tiers of independent
   judgement -- a stronger tagger, context keyword cues, then a batched
   LLM call with a human-review-file fallback that is always wired in.

Read `abpipe/homographs.py`'s module docstring first: it owns the
markup mechanism, the decisions document, and the freshness rule this
module's output feeds. This module owns only the disambiguation itself.

--------------------------------------------------------------------------
Phase 0 measurements (this machine, this run, 2026-08-15)
--------------------------------------------------------------------------

**Model load and throughput.** `en_core_web_sm` (tok2vec+tagger only)
loads in ~0.9-1.3s. `en_core_web_trf` (transformer+tagger only) loads
cold in **1.75s** -- faster than the plan's 2-3s estimate, on this
machine. `misaki.en.G2P(british=True)` builds in ~0.36s (the lexicon
load, not a model). Tagging throughput, measured with `nlp.pipe()` over
all 165 real chunks of chapter 1 of Book A: **165 chunks in 3.08s
= 53.6 chunks/s**. A book the size of Book A (~2,000 chunks) tags
in well under a minute; this is not the audit's bottleneck.

**The seven wound-family passages (plan section 1.1), re-measured here**
with both taggers plus misaki's actual phonemes (`G2P(british=True)`):

| Chunk        | Passage (word)                          | sm tag | trf tag | misaki tag | phonemes    | note |
|---           |---                                       |---     |---      |---         |---          |---|
| ch01/0059    | "muffler wound round and round"         | NN     | **VBN** | NN         | wˈuːnd      | **the crux bug**: sm/trf DISAGREE, trf right (verb) |
| ch04/0037    | "rosary beads wound round and round"    | VBP    | VBN     | VBP        | wˈWnd       | sm/trf agree at the *reading* level (VBP,VBN -> verb); correct |
| ch07/0140    | "he unwound her arms"                   | VBP    | VBD     | VBP        | ʌnwˈWnd     | "unwound" is its own single-reading lexicon entry, not ambiguous |
| ch11/0165    | "like bayonet wounds"                   | NNS    | NNS     | NNS        | wˈuːndz     | agree (noun); correct |
| ch14/0013    | "It wound round and round him"          | VBD    | VBD     | VBD        | wˈWnd       | agree (verb); correct |
| ch16/0064    | "wounded, crying out for shelter"       | VBD    | VBN     | VBD        | wˈuːndɪd    | "wounded" is its own single-reading entry (no "wind" sense exists for it); tags differ but it does not matter |
| ch18/0022    | "the wound in his thigh"                | NN     | NN      | NN         | wˈuːnd      | agree (noun); correct |

Of the 5 occurrences where "wound"/"wounds" is genuinely ambiguous (the
two "unwound"/"wounded" rows are single-reading words, out of the
pos_map's scope entirely), sm and trf **agree on the reading in 4/5**
and disagree in exactly the one case the plan predicted -- ch01/0059,
where trf is right. This confirms the plan's Tier 1 design at the crux
case: agreement auto-passes, disagreement escalates, and it is not
common enough to need a faster path.

**The 46-sentence probe set** (hand-labeled, 8 class-A words with real
two-reading pos_map entries pulled directly from `misaki`'s own
`gb_gold.json`: wound, wind, minute, read, live, tear, bow, dove --
`lead`, `row`, `bass` are excluded because misaki's own lexicon gives
them only one reading total, so no tagger can help; see "read" below for
why they are not the only unfixable case):

    sm accuracy:  39/46 = 84.8%
    trf accuracy: 42/46 = 91.3%

trf beats sm on every mismatch it does not also share, confirming the
plan's expectation (trf > sm, not the reverse) -- **the design does
NOT need to demote Tier 1 or promote Tier 2 to primary.**

**A genuine, unpredicted limitation, found by this measurement, that the
plan does not mention:** misaki's own `gb_gold.json` entry for "read" is
`{"ADJ": "ɹˈɛd", "DEFAULT": "ɹˈiːd", "VBD": "ɹˈɛd", "VBN": "ɹˈɛd", "VBP":
"ɹˈɛd"}` -- it maps **VBP (present tense) to the PAST-tense sound**. All
three present-tense probe sentences ("I read the newspaper every single
morning", "I always read before falling asleep", "Children read faster
when they enjoy the story") get tagged VBP by *both* sm and trf --
correctly, by the grammar -- and both then map through this dict to the
wrong sound. **No tagger, however good, can fix this: the fault is in
misaki's own reading table, not in tagging.** Tier 1 will see sm and trf
*agree* here (both give "past" through misaki's own table) and auto-pass
at high confidence -- silently wrong. This is not a case Tier 2's cue
rules can catch either, since there is no reliable context keyword that
marks present-tense "read" (unlike "wound" + "round"). Reported to the
overlord for H2: "read" may need either a hand-authored VBP override
(distinguishing today's VBP from a stale-tagged past "read") in the
inventory's `cues`, or acceptance that present-tense "read" is
permanently unresolvable by this pipeline and belongs in the review file
by policy, not through the normal tier flow. This module implements the
tiers exactly as specified regardless; the false-positive risk for
"read" specifically is a heteronyms.json content question, not a tier
mechanism question.

**Also measured, matching the plan's expectation:** trf beat sm at
"minute" attributive-adjective use too ("the minute details" -- trf: JJ,
correct; sm: NN, wrong) but both missed a subtler case ("a minute crack
in the porcelain vase" -- both NN). "live" tagged as an adverb (RB) by
both taggers, in "broadcast live" and "airs live" -- outside a plain
two-way pos_map keyed on VERB/ADJ.

**Correction, made after a real audit run found a live production bug
here (see the "used" note in DEFECT 2 below):** the standalone
measurement script used to produce the 91.3% number above gave its own
`reading_for()` helper a DEFAULT-reading fallback, matching misaki's own
`Lexicon.lookup` convention (`ps.get(tag, ps['DEFAULT'])`), so an
untagged case like "live"/RB still resolved to the non-verb reading in
that measurement. **`_reading_for_tag` below, the function actually
shipped in this module, does NOT do this** -- it tries the raw tag, then
the parent tag, and returns `None` (escalate to Tier 2) when neither is
present in `pos_map`, on purpose, unconditionally, for every word this
module ever tiers. A tag absent from `pos_map` must never resolve to a
default reading: proven necessary in production, not just in theory --
see DEFECT 2. Inventing a reading from a default is the one behaviour
that can make this tool ADD a mispronunciation to a book where misaki
was already right, which is the worst possible failure mode for an
audit tool. The 91.3% figure is honest as a tagger-quality measurement
(trf really does tag better than sm), but it is *not* a measurement of
this module's shipped accuracy, which is more conservative by design:
a "live"-shaped case escalates to Tier 2 in production rather than
silently resolving, exactly as `_reading_for_tag`'s docstring says.

--------------------------------------------------------------------------
The misaki-token <-> Occurrence offset mapping
--------------------------------------------------------------------------

`misaki.token.MToken` carries no character-offset field. Verified two
methods on 4 real chunks (ch01/0059, ch04/0037, ch14/0013, ch18/0022, no
pronunciation map) plus one synthetic test with a length-changing
substitution:

- **Offset reconstruction** (`_token_spans`): walk `G2P.preprocess`'s
  `text.lstrip()`-adjusted start, then accumulate `len(tk.text) +
  len(tk.whitespace)` per token -- the same formula
  `misaki.en.G2P.resolve_tokens` itself uses internally
  (`text = ''.join(tk.text + tk.whitespace for tk in tokens[:-1]) +
  tokens[-1].text`). **Verified exact** against `Occurrence.start/end` on
  all 4 real chunks when the pronunciation map is empty (the common
  case: most books carry none, and `homographs.validate()` forbids a
  pronunciation-map word from colliding with a heteronym word).
- **Sequence-position matching**: apply_pronunciations only respells
  whole *different* words (never a heteronym word itself, by the same
  validation rule) and never adds or removes a word, so the Nth
  occurrence of a heteronym word in the pronunciation-mapped text is
  still the Nth occurrence of that word in the on-disk text, even after
  an earlier substitution has shifted every following character offset.
  **Verified with a synthetic length-changing substitution** placed
  before a real "wound" occurrence: the offset method correctly fails to
  find a span match (`found=False`), and the sequence method still
  resolves the exact token misaki actually used.

**Chosen mechanism: try the offset match first (exact, and immune to any
tokenization quirk since it is a literal span comparison); fall back to
sequence-position matching per word when the offset match misses** (this
only differs from the offset match after an earlier pronunciation
substitution in the same chunk has already shifted the text -- the rare
case). Both are implemented in `baseline_details` below.

--------------------------------------------------------------------------
The Tier 2 cue window
--------------------------------------------------------------------------

**Chosen window: `Occurrence.context`** -- the sentence
`abpipe.homographs._sentence_context` already computed and attached to
every occurrence, not a fresh N-token window. Reasons: (1) it is a single
source of truth -- a human reading the audit report or the review file
sees the exact same span the cue matcher searched, so there is nothing
to explain twice; (2) the plan's own measurement ("wound" + round/around
-> verb, precision 1.0) was made within-sentence, and every cue example
in the plan's schema ("the wound", "his wound", "bullet", "knife" for
the noun reading) reads as sentence-scoped English, not a token-count
window; (3) a fixed N-token window risks crossing a sentence boundary
and picking up an unrelated cue from the next sentence, which a
sentence boundary cannot do by construction.

--------------------------------------------------------------------------
Two defects found by a real audit run over Book A, chapter 1, and fixed here
--------------------------------------------------------------------------

**DEFECT 1 -- a heteronym inside a hyphenated compound token reported as
"unresolved".** `ch01/0004 "wind"` in "a wide wind-swept asphalt lane"
and `ch01/0060 "close"` in "a close-cropped bullet-shaped head": misaki
tokenizes each compound as ONE token (`G2P(british=True)` on the real
chunk text: `'wind-swept'` -> phonemes `'wˌɪndswˈɛpt'`; `'close-cropped'`
-> phonemes `'klQskɹˈɒpt'`) -- there is no standalone "wind" or "close"
token to read a baseline from, and no word-level `[wind](/…/)` markup
could force a reading either, since the markup would split a token
misaki has already fused and change what the engine says to something
nobody chose. These occurrences are not forceable by this mechanism at
all, so reporting them as `"unresolved"` was wrong twice over: it
inflated the Tier 3 review queue with something no human decision could
act on, and it failed the audit's gate on a case no verdict could ever
fix. **Fix:** `baseline_details` now detects this case structurally --
the occurrence's span falls strictly inside a single token's span that
is longer than the word itself -- not by looking for a hyphen character
(a real hyphen-joined token *pair*, where the word still gets its own
token, is not this case, and a hyphen test would wrongly catch it; the
structural span test only fires when misaki genuinely fused the tokens).
`disambiguate` checks this before Tier 1 and, when it fires, returns a
`Verdict` with `tier="not_applicable"`, `reading=None`, `phonemes=None`,
`confidence="low"`, skipping Tier 2, Tier 3's LLM call, and the review
file entirely -- Worker H1's `run()` excludes `"not_applicable"` from
the failure count and reports it separately.

**DEFECT 2 -- `ch01/0050 "used"` in "another room that was used by the
occupants" verdicted `jˈuːst` (wrong) against a correct baseline of
`jˈuːzd`.** Traced by re-running `baseline_details` and `_tier1_tag`
directly against the real chunk and the real (H2-authored) inventory
entry for "used": **both sm and trf tag it VBN**, correctly -- this is
an ordinary passive past participle, "was used by". The inventory
entry's `pos_map` (`{"NN": "noun", "NNS": "noun", "VB": "verb", "VBD":
"verb", "VBG": "verb", "VBN": "verb", "VBP": "verb", "VBZ": "verb"}`)
maps VBN to the reading named `"verb"`, whose phonemes are `jˈuːst` --
so Tier 1 agreed with itself (sm VBN -> "verb", trf VBN -> "verb") and
confidently produced the wrong phonemes. **This is not a
`_reading_for_tag` bug: `_reading_for_tag` never referenced
`default_reading` or any other fallback anywhere in this file (grepped
to confirm) -- VBN was an explicit, present key in `pos_map`, resolved
exactly as written, at high confidence, exactly as designed.** The real
cause is upstream, in the inventory's `pos_map` for "used" itself:
misaki's own gold lexicon special-cases "used" outside its normal
tag-keyed dict (`misaki/en.py` `Lexicon.lookup`, the `elif word in
('used', 'Used', 'USED')` branch) -- it returns the `jˈuːst` reading
only when the tag is VBD/JJ **and the following word is "to"** (the
"used to" idiom), and returns the ordinary `jˈuːzd` reading for every
other tag, including a plain VBN with no "to" following, exactly this
sentence. A flat `pos_map` cannot express "look at the next token" at
all, so mapping every VB*-family tag unconditionally to `jˈuːst` is
simply wrong for the ordinary (non-idiom) case, which is the overwhelming
majority of "used" occurrences in real prose. **Reported to H2/the
overlord, not fixed here**: this module has no authority to special-case
one inventory word's `pos_map`, and doing so would be the wrong
architecture even if it fixed this one word -- the fix belongs in
`heteronyms.json` (either swap which reading name the ordinary VB* tags
map to, or drop them from `pos_map` entirely and add a Tier 2 cue keyed
on a following "to"). Nothing in `homograph_tiers.py` changed for this
defect beyond the confirmation that no default-fallback path exists:
see `_reading_for_tag`'s docstring, and the correction two sections
above, both of which state the same "never invent a reading" rule this
trace confirmed is honoured everywhere in this file.

--------------------------------------------------------------------------
Tier 3: the `claude` CLI, following the precedent of another project in
this codebase
--------------------------------------------------------------------------

Read directly from `score.py` of another project in this codebase (`claude`
2.1.232, this machine): `call_claude` runs
`claude -p --output-format json --model sonnet --tools "" --system-prompt
"<...>"` with the prompt piped on stdin (`subprocess.run(cmd,
input=prompt, capture_output=True, text=True, timeout=...)`); the JSON
envelope's `"result"` key holds the model's raw text; a non-zero exit or
an `is_error` envelope raises/logs rather than trusting the output.
`extract_json_array` pulls the outermost balanced `[...]` out of a
possibly chatty response (stripping markdown fences first), and
`normalize` drops any item with a bad or unknown id/field rather than
raising. `score_batch` retries once on an empty or partial result, then
gives up and reports the batch as unscored -- it never fabricates a
score. This module's `_call_claude` / `_extract_json_array` /
`_normalise_tier3` follow that exact shape: one subprocess call per
batch (here, one call for the whole book's unresolved occurrences, since
volume is tens per book, not thousands of jobs), a `{"index":
<int>, "reading": "<name>"}` JSON array, one retry on a partial/empty
parse, and a hard degrade to the review file -- never a raise, never a
crash, never a guess -- for anything the retry does not fix.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import spacy
from misaki import en as misaki_en
from misaki import espeak as misaki_espeak

from abpipe.homographs import Occurrence, strip_stress
from abpipe.meta import write_json

# --------------------------------------------------------------------------- module-level caches
#
# Both the misaki G2P object and the trf spacy pipeline are expensive to
# build (a lexicon load, a transformer model load, and now an espeak
# backend load for the fallback) and the audit calls into this module
# once per chunk (baseline) and once for the whole book (disambiguate) --
# hundreds of calls on a real book. Caching at module level, keyed only
# on the one axis that actually varies (dialect for G2P; nothing for trf,
# which is dialect-independent), means the cost is paid once per process,
# not once per call. The fallback is built as part of the cached G2P, not
# separately -- it is not an independent axis, it is fixed by the same
# `dialect` key everything else here is cached on.

_G2P_CACHE: dict[str, misaki_en.G2P] = {}
_TRF_NLP: Any = None


def _build_espeak_fallback(british: bool) -> "misaki_espeak.EspeakFallback | None":
    """Build the `misaki.espeak.EspeakFallback` the real render wires into
    its G2P, or `None` on any failure -- mirroring, exactly,
    `mlx_audio/tts/models/kokoro/pipeline.py` lines 146-151 (this venv's
    `mlx_audio` package): a bare `try`/`except Exception` around the
    constructor call, degrading to no fallback and logging a warning
    rather than raising. This is deliberate, not merely convenient: if
    the real render's own fallback construction fails on this machine (a
    missing espeak-ng library, a phonemizer backend error), the real
    render degrades the exact same way, silently skipping
    out-of-lexicon words instead of crashing. This audit's baseline must
    be wrong in that same direction, not a different one -- raising here
    instead would make the baseline claim a fidelity guarantee the actual
    render cannot keep on this machine."""
    try:
        return misaki_espeak.EspeakFallback(british=british)
    except Exception as exc:  # noqa: BLE001 -- mirrors pipeline.py's bare except
        print(
            f"[homograph_tiers] EspeakFallback not enabled: out-of-lexicon words "
            f"will be skipped, same as the real render on this machine ({exc!r})",
            flush=True,
        )
        return None


def _get_g2p(dialect: str) -> misaki_en.G2P:
    """Return the cached `misaki.en.G2P` for this dialect, building it once.

    `dialect` is "gb" or "us" (see `abpipe.homographs.dialect_for_lang_code`).
    This construction -- `trf=False`, `british=(dialect == "gb")`,
    `fallback=EspeakFallback(british=...)`, `unk=""` -- is copied from
    `mlx_audio/tts/models/kokoro/pipeline.py` lines 144-154 (this venv,
    `mlx_audio` package), which maps its own `lang_code` "b"/"a" to
    `british` the same way: `dialect == "gb"` here is exactly
    `lang_code == "b"` there. All four parts matter: the fallback makes an
    out-of-lexicon word render as espeak's phonemes instead of nothing,
    and `unk=""` (not misaki's own default `'❓'`) matches what the real
    render passes for whatever the fallback still cannot resolve. Drop
    either one and the baseline stops being ground truth for out-of-
    lexicon words specifically -- see the module docstring's worked
    example. **Must stay in sync with `pipeline.py`'s
    `KokoroPipeline.__init__`; re-read it before changing this function.**
    """
    g2p = _G2P_CACHE.get(dialect)
    if g2p is None:
        british = dialect == "gb"
        fallback = _build_espeak_fallback(british)
        g2p = misaki_en.G2P(trf=False, british=british, fallback=fallback, unk="")
        _G2P_CACHE[dialect] = g2p
    return g2p


def _get_trf_nlp():
    """Return the cached `en_core_web_trf` pipeline, loading it once.

    Only `transformer` and `tagger` are enabled -- the same two-component
    shape mlx-audio hardcodes for the `sm` pipeline misaki itself builds
    (`components = ['transformer' if trf else 'tok2vec', 'tagger']`,
    misaki/en.py `G2P.__init__`), so Tier 1's trf run is doing exactly the
    same amount of work misaki's own sm run does, just with the better
    tagger swapped in. Measured cold load: 1.75s.
    """
    global _TRF_NLP
    if _TRF_NLP is None:
        _TRF_NLP = spacy.load("en_core_web_trf", enable=["transformer", "tagger"])
    return _TRF_NLP


def release_transformer() -> bool:
    """Drop the cached `en_core_web_trf` pipeline. Return True when one went.

    **Warning: a long-lived caller must call this when the audit is done.**
    `_get_trf_nlp()` caches the transformer in a module global, and nothing
    ever frees it. That is correct for the command line, where the process
    ends a moment later. It is wrong for a service.

    Measured on a server with a 5 GB container limit: the audit runs once,
    before the render, and then the transformer sits in memory for the whole
    book. The render stage held 4.2 to 4.6 GB with it resident, against 2.5 GB
    without it. The QC stage then loads a whisper model of about 2.1 GB on
    top, so the process runs out of memory part of the way through a job that
    takes hours.

    The audit needs the tagger once. The render and the QC stage never need
    it. So the caller frees it, and the next audit loads it again.
    """
    global _TRF_NLP
    if _TRF_NLP is None:
        return False
    _TRF_NLP = None
    import gc

    gc.collect()
    return True


# --------------------------------------------------------------------------- baseline (what the engine will say)


def _token_spans(text: str, tokens: list) -> list[tuple[int, int, Any]]:
    """Reconstruct each token's [start, end) character offset in `text`.

    `misaki.token.MToken` carries no offset field. `G2P.preprocess` calls
    `text.lstrip()` before tokenizing, so this walk starts at the length of
    the stripped leading whitespace; after that, each token's start is the
    previous token's end plus its own whitespace length. This is the exact
    formula `misaki.en.G2P.resolve_tokens` itself uses internally to
    reconstruct a joined string (`''.join(tk.text + tk.whitespace for tk in
    tokens[:-1]) + tokens[-1].text`) -- not a guess at the mechanism, a
    read of it. Verified exact against real chunk offsets; see this
    module's docstring.
    """
    base = len(text) - len(text.lstrip())
    pos = base
    spans: list[tuple[int, int, Any]] = []
    for tk in tokens:
        start = pos
        end = start + len(tk.text)
        spans.append((start, end, tk))
        pos = end + len(tk.whitespace)
    return spans


def _token_rating(tk: Any) -> int | None:
    """Return misaki's confidence rating for a token's phonemes, if any.

    misaki stores this inconsistently depending on how the token was
    resolved: a token folded from multiple subtokens gets it inside
    `tk._.rating` (`merge_tokens`, misaki/en.py); a token resolved as a
    single subtoken gets a plain dynamic `tk.rating` attribute (`MToken`
    declares no such dataclass field, but `G2P.__call__` assigns it anyway
    at `w.phonemes, w.rating = self.lexicon(...)`). Check both. This value
    is informational only, for the audit report -- nothing here treats it
    as authoritative.
    """
    rating = getattr(tk, "rating", None)
    if rating is not None:
        return rating
    underscore = getattr(tk, "_", None)
    return getattr(underscore, "rating", None) if underscore is not None else None


def baseline_details(
    text: str,
    occurrences: list[Occurrence],
    dialect: str,
    pronunciations: dict | None,
) -> dict[tuple, dict]:
    """Return `{occurrence.key: {"phonemes": ..., "tag": ..., "rating": ...}}`
    -- what misaki actually assigned that token, phonemized in the exact
    render-identical configuration.

    Applies `render.apply_pronunciations` first, exactly as `render.run()`
    does before it calls the engine -- this is what makes the result
    ground truth for the delivered audio, not an estimate of it. Imported
    lazily inside this function (the `qc.py` pattern): importing this
    module at the top of `abpipe.homographs` (which happens lazily too,
    see that module's docstring) must never risk importing a
    still-being-edited `render.py` at import time.

    Matching a token to an occurrence: try an exact offset match first
    (`_token_spans` against the pronunciation-mapped text); an occurrence
    whose (start, end) span survives unchanged through
    `apply_pronunciations` -- true for every occurrence before the first
    substitution in this chunk, and every occurrence at all when
    `pronunciations` is empty, the common case -- lands here exactly.
    Fall back to sequence-position matching per word (the Nth token whose
    text matches the word, for the Nth occurrence of that word) when the
    offset match misses: `apply_pronunciations` only ever respells whole
    *different* words (never a heteronym word itself --
    `homographs.validate()` rejects a book where they collide) and never
    adds or removes a word, so word *order* survives a length-changing
    substitution even though character offsets do not. See this module's
    docstring for the experiment that verified both halves of this.

    **A third case, checked between those two:** the occurrence's span can
    fall STRICTLY INSIDE a single misaki token that is longer than the
    word itself -- misaki tokenizes a hyphenated compound like
    "wind-swept" or "close-cropped" as ONE token and phonemizes it as a
    unit (verified: `G2P(british=True)("...wind-swept...")` yields a
    single token `'wind-swept'` with phonemes `'wˌɪndswˈɛpt'`; there is no
    standalone "wind" token to read a baseline from at all). This is
    detected structurally -- a token whose span contains the occurrence's
    span but is not equal to it, not by looking for a hyphen character --
    because the same fusion happens for any compound misaki tokenizes as
    a unit, and a hyphen character can just as easily separate two
    tokens misaki keeps distinct (a real hyphen-joined token pair is NOT
    this case and must not be reported as one). Such an occurrence gets
    `"compound"` set to the containing token's text instead of a
    phoneme/tag pair -- `disambiguate` below reads this and routes the
    occurrence straight to a `"not_applicable"` verdict, never through
    the normal tiers, because word-level `[word](/phonemes/)` markup
    cannot force a reading inside a token misaki has already fused: the
    markup would split the compound and change what the engine says to
    something no one asked for, not fix the reading.

    A value of `None` for "phonemes" means the occurrence could not be
    located in the tokenized text at all, or misaki assigned it no
    phonemes -- not the compound case above, which is reported separately.
    """
    if not occurrences:
        return {}

    from abpipe.render import apply_pronunciations

    engine_text = apply_pronunciations(text, pronunciations)
    g2p = _get_g2p(dialect)
    _, tokens = g2p(engine_text)
    spans = _token_spans(engine_text, tokens)
    span_by_offset = {(start, end): tk for start, end, tk in spans}

    by_word: dict[str, list[Occurrence]] = {}
    for occ in occurrences:
        by_word.setdefault(occ.word.lower(), []).append(occ)
    for occs in by_word.values():
        occs.sort(key=lambda o: o.occurrence)

    def _entry(tk: Any) -> dict:
        return {"phonemes": tk.phonemes, "tag": tk.tag, "rating": _token_rating(tk), "compound": None}

    def _containing_compound(occ: Occurrence) -> str | None:
        """Return the text of a misaki token that strictly contains this
        occurrence's span but is longer than the occurrence's own word --
        see the docstring above. Checked in the same (start, end)
        coordinate space `_token_spans` reconstructs from `engine_text`,
        so like the exact-offset match above, this is reliable up to the
        first pronunciation substitution earlier in the chunk (the common
        case: most chunks, and every occurrence before that point, carry
        none)."""
        span_len = occ.end - occ.start
        for start, end, tk in spans:
            if start <= occ.start and occ.end <= end and len(tk.text) > span_len:
                return tk.text
        return None

    result: dict[tuple, dict] = {}
    unresolved_by_word: dict[str, list[Occurrence]] = {}

    for word, occs in by_word.items():
        for occ in occs:
            tk = span_by_offset.get((occ.start, occ.end))
            if tk is not None and tk.text.lower() == word:
                result[occ.key] = _entry(tk)
                continue
            compound_token = _containing_compound(occ)
            if compound_token is not None:
                result[occ.key] = {"phonemes": None, "tag": None, "rating": None, "compound": compound_token}
                continue
            unresolved_by_word.setdefault(word, []).append(occ)

    for word, occs in unresolved_by_word.items():
        count = 0
        idx = 0
        for tk in tokens:
            if idx >= len(occs):
                break
            if tk.text.lower() == word:
                count += 1
                if count == occs[idx].occurrence:
                    result[occs[idx].key] = _entry(tk)
                    idx += 1
        for occ in occs[idx:]:
            result[occ.key] = {"phonemes": None, "tag": None, "rating": None, "compound": None}

    return result


def baseline_phonemes(
    text: str,
    occurrences: list[Occurrence],
    dialect: str,
    pronunciations: dict | None,
) -> dict[tuple, str | None]:
    """Return `{occurrence.key: the phoneme string misaki assigns that
    token}`. `None` when the token cannot be located or misaki gives it no
    phonemes (this includes the "inside a fused compound token" case --
    see `baseline_details`, which callers that need to distinguish it
    should call directly). This is a thin projection of `baseline_details`."""
    return {key: detail.get("phonemes") for key, detail in baseline_details(text, occurrences, dialect, pronunciations).items()}


# --------------------------------------------------------------------------- out-of-lexicon words


def _lexicon_miss_words(text: str, dialect: str) -> list[tuple[str, str | None, int | None]]:
    """Return one `(word, espeak_phonemes, rating)` triple per misaki
    token in `text` that misaki's own lexicon could not resolve -- one
    entry per occurrence, not deduplicated; the caller (`unknown_words`)
    aggregates counts across the whole book.

    **Detection method:** a token whose final `_token_rating(tk) == 2`.
    Read straight off the single with-fallback G2P `_get_g2p` already
    builds for the baseline -- no second G2P object, no second pass over
    the text.

    **Why rating alone is dependable here, checked rather than assumed:**
    grepped both `misaki/en.py` and `misaki/espeak.py` (this venv) for
    every literal rating a token can receive. Exactly one place ever
    assigns 2: `EspeakFallback.__call__` (misaki/espeak.py) hardcodes
    `return ..., 2` on every call, unconditionally -- the fallback is
    only ever invoked (`misaki.en.G2P.__call__`) when the lexicon has
    already returned `None` for that token, so a rating of 2 can only
    mean "the lexicon missed, and the fallback answered instead."
    Every in-lexicon path found rates 3, 4, or 5 (silvers/NNP 3, golds
    and the special-case table 4, inline `/phonemes/` markup 5) and
    never 2. A multi-sub-token merge (`merge_tokens`, misaki/en.py) takes
    `min(rating)` over its sub-tokens' ratings (or `None` if any
    sub-token has no rating at all) -- `min` can lower a merged rating
    toward 2 but never produce a spurious 2 out of ratings that are all
    3+, so a merged word inherits 2 if and only if fallback resolved (part
    of) it. Confirmed empirically too, not just by reading the source: on
    this book's ch01/0129 chunk, "divil" and "Gyko" (single-token misses)
    and "McAllister" (misaki folds "Mc"+"Allister" into one merged token,
    resolved as a whole by the fallback once "Mc" alone failed the
    lexicon) all carry `_token_rating == 2`, and no in-lexicon token in
    that chunk does. Cross-checked against the more expensive
    with-fallback/without-fallback double-G2P-build comparison (build a
    second, fallback-less G2P, and flag a token whose lexicon-only
    phonemes come back `None`) across 15 further real chunks of chapter 1
    -- zero mismatches between the two methods. Given that agreement, the
    single-pass rating check is what ships: it is simpler, and half the
    G2P work, for the same answer.

    A rating of `None` is NOT a miss signal by itself (an ordinary
    multi-sub-token lexicon resolution can also merge to `None` -- one of
    its sub-tokens simply never had its `_.rating` field touched, see
    `misaki.en.G2P.__call__`'s left/right binary search loop, which sets
    a plain `.rating` attribute on ancillary sub-tokens rather than
    `_.rating` -- so `None` is ambiguous where 2 is not)."""
    g2p = _get_g2p(dialect)
    _, tokens = g2p(text)

    misses: list[tuple[str, str | None, int | None]] = []
    for tk in tokens:
        if _token_rating(tk) != 2:
            continue
        word = tk.text
        if not any(c.isalpha() for c in word):
            continue  # a punctuation-only token can never be an out-of-lexicon word
        misses.append((word, tk.phonemes, _token_rating(tk)))
    return misses


def unknown_words(
    texts: dict[tuple[str, str], str],
    dialect: str,
    pronunciations: dict | None = None,
) -> list[dict]:
    """Return one entry per distinct word (case-insensitively) that
    misaki's own lexicon has no entry for, anywhere in `texts`, aggregated
    across the whole book:

        {"word": ..., "phonemes": ..., "rating": ..., "count": ...,
         "first_chapter": ..., "first_chunk": ...}

    `texts` maps `(chapter, chunk)` to that chunk's text -- the same
    shape `disambiguate` already takes, so a caller that already built
    that dict for a disambiguate run can pass it straight through.

    `phonemes` is what the real espeak fallback actually produces for the
    word (see `_get_g2p`) -- `None` only in the degraded case where
    `_build_espeak_fallback` itself could not build a fallback on this
    machine, matching what the real render would also produce there.
    `rating` is misaki's own confidence rating on the fallback-resolved
    token; for every entry this function returns it is 2 (see
    `_lexicon_miss_words`), reported anyway rather than hardcoded, so a
    caller never has to trust an assumption this docstring states instead
    of the data itself.

    **Out-of-lexicon detection is deliberately NOT "looks foreign" and
    does not filter by character set.** A real English-looking word that
    simply has no lexicon entry -- a publisher typo like "hisshadow"
    (which espeak happily mangles into `hˈɪsʃədˌQ` rather than refusing),
    or a dialect word like "divil" -- is exactly the case this function
    exists to surface, and it is the highest-value case: nobody already
    knows a typo is there. Filtering by shape would hide it. The signal
    used is purely "misaki's lexicon has no entry" -- see
    `_lexicon_miss_words`.

    **Case:** aggregated case-insensitively (so "Gyko" and a hypothetical
    lower-cased "gyko" elsewhere in the book count as one word and one
    `count`), but the reported `"word"` is the first spelling this
    function encounters, in `(chapter, chunk)` order -- a proper noun
    must come back capitalized, not silently lower-cased just because
    aggregation is case-insensitive. `first_chapter`/`first_chunk` name
    that same first-seen occurrence, for a quick look at context.

    **Sorted by `count` descending, then by word** -- the frequent misses
    (worth hand-seeding a pronunciation for) sort to the top; this
    ordering, not just the aggregation itself, is why this function
    operates over the whole book rather than returning per-chunk results
    a caller would have to re-aggregate itself.

    Reads the same cached G2P object `baseline_phonemes` uses, but
    changes nothing about `baseline_phonemes`, `baseline_details`, or any
    tier in `disambiguate` -- this is a separate, read-only report."""
    from abpipe.render import apply_pronunciations

    by_lower: dict[str, dict] = {}

    for key in sorted(texts):
        chapter, chunk = key
        text = texts[key]
        if not text:
            continue
        engine_text = apply_pronunciations(text, pronunciations)
        for word, phonemes, rating in _lexicon_miss_words(engine_text, dialect):
            lower = word.lower()
            entry = by_lower.get(lower)
            if entry is None:
                by_lower[lower] = {
                    "word": word,
                    "phonemes": phonemes,
                    "rating": rating,
                    "count": 1,
                    "first_chapter": chapter,
                    "first_chunk": chunk,
                }
            else:
                entry["count"] += 1

    return sorted(by_lower.values(), key=lambda entry: (-entry["count"], entry["word"]))


# --------------------------------------------------------------------------- verdicts


@dataclass
class Verdict:
    occurrence: Occurrence
    reading: str | None  # a key into the inventory entry's readings[dialect]
    phonemes: str | None
    tier: str  # "tier1", "tier2:cue:round", "tier3:llm", "tier3:review", "unresolved"
    confidence: str  # "high", "medium" or "low"
    detail: str = ""


# --------------------------------------------------------------------------- Tier 1: the transformer tagger


def _get_parent_tag(tag: str | None) -> str | None:
    """Mirror `misaki.en.Lexicon.get_parent_tag` exactly, so a `pos_map`
    entry keyed on a parent tag (VERB, NOUN, ADJ, ADV -- the shape
    misaki's own "wind" and "live" entries use) resolves the same way
    misaki's own lexicon resolves the identical key. Copied, not imported:
    `Lexicon.get_parent_tag` is a `@staticmethod` on a private class this
    module has no other reason to import."""
    if tag is None:
        return tag
    if tag.startswith("VB"):
        return "VERB"
    if tag.startswith("NN"):
        return "NOUN"
    if tag.startswith("ADV") or tag.startswith("RB"):
        return "ADV"
    if tag.startswith("ADJ") or tag.startswith("JJ"):
        return "ADJ"
    return tag


def _reading_for_tag(tag: str | None, pos_map: dict) -> str | None:
    """Resolve a POS tag to a reading name via the inventory entry's
    `pos_map`. Tried as the raw Penn tag first (the plan's "wound" example
    keys on "VBD", "NN", ...), then the parent tag (misaki's own "wind" and
    "live" gold entries key on "VERB"/"DEFAULT" -- an inventory entry can
    plausibly use the same shorthand, and the probe-set measurement above
    shows this matters: "live" broadcast sense tags RB in both taggers,
    which only resolves through this parent-tag fallback landing on the
    non-verb default).

    Absent from both -> `None`. Per the plan: "The tag is absent from
    pos_map -> fall to Tier 2." Unlike misaki's own `Lexicon.lookup`, this
    does *not* fall back to `pos_map`'s own idea of a default reading --
    an inventory entry does not have to declare one, and "cannot resolve"
    is exactly the signal that should send an occurrence to Tier 2's cues
    rather than silently guessing the majority reading.
    """
    if tag is None:
        return None
    if tag in pos_map:
        return pos_map[tag]
    parent = _get_parent_tag(tag)
    if parent is not None and parent in pos_map:
        return pos_map[parent]
    return None


def _find_spacy_token(doc, start: int, end: int):
    """Return the spacy token covering `text[start:end]` of the doc's own
    text (the ON-DISK chunk text -- `doc` here is always built by tagging
    `texts[key]` directly, never a pronunciation-mapped copy, so `tok.idx`
    lines up with `Occurrence.start` with no offset math needed at all)."""
    for tok in doc:
        if tok.idx == start:
            return tok
    for tok in doc:  # a boundary mismatch (rare) -- widen to any overlap
        if tok.idx < end and tok.idx + len(tok.text) > start:
            return tok
    return None


def _tier1_tag(
    occurrences: list[Occurrence], texts: dict[tuple[str, str], str]
) -> tuple[dict[tuple, dict], float, int]:
    """Tag every distinct chunk that holds an occurrence, once each, via
    `nlp.pipe()` -- not once per occurrence -- for throughput (measured:
    53.6 chunks/s on real chunks). Returns (per-occurrence trf tag dict,
    elapsed seconds, chunk count) so the caller can log the measured rate.

    Matches spacy's token to the occurrence by `token.idx` against the
    ON-DISK chunk text (`texts[key]`, never the pronunciation-mapped
    text), per the plan: the correct grammatical tag for a word does not
    depend on a respelled proper noun three sentences away, and using the
    on-disk text means `Occurrence.start`/`.end` need no adjustment.
    """
    keys_needed = sorted({(occ.chapter, occ.chunk) for occ in occurrences})
    chunk_texts = [texts.get(key, "") for key in keys_needed]

    nlp = _get_trf_nlp()
    t0 = time.monotonic()
    docs = list(nlp.pipe(chunk_texts))
    elapsed = time.monotonic() - t0

    doc_by_key = dict(zip(keys_needed, docs))
    occs_by_chunk: dict[tuple, list[Occurrence]] = {}
    for occ in occurrences:
        occs_by_chunk.setdefault((occ.chapter, occ.chunk), []).append(occ)

    result: dict[tuple, dict] = {}
    for key, occs in occs_by_chunk.items():
        doc = doc_by_key.get(key)
        for occ in occs:
            tok = _find_spacy_token(doc, occ.start, occ.end) if doc is not None else None
            result[occ.key] = {"trf_tag": tok.tag_ if tok is not None else None}
    return result, elapsed, len(chunk_texts)


# --------------------------------------------------------------------------- Tier 2: context cue rules

_CUE_WORD_CACHE: dict[str, re.Pattern[str]] = {}


def _cue_pattern(keyword: str) -> re.Pattern[str]:
    pattern = _CUE_WORD_CACHE.get(keyword)
    if pattern is None:
        # \b-bounded and case-insensitive on purpose, same rule
        # `homographs._word_pattern` uses for the word itself: a keyword
        # like "round" must not match inside "surround", and "the wound"
        # (a multi-word cue phrase, per the plan's schema example) works
        # unchanged since \b only cares about the two outer edges.
        pattern = re.compile(r"\b" + re.escape(keyword) + r"\b", re.IGNORECASE)
        _CUE_WORD_CACHE[keyword] = pattern
    return pattern


def _cue_hits(window: str, cues: dict[str, list[str]]) -> dict[str, list[str]]:
    """Return `{reading: [keywords that fired]}` for every reading whose
    cue list has at least one whole-word (or whole-phrase) match in
    `window`, case-insensitively."""
    hits: dict[str, list[str]] = {}
    for reading, keywords in cues.items():
        fired = [kw for kw in keywords if _cue_pattern(kw).search(window)]
        if fired:
            hits[reading] = fired
    return hits


# --------------------------------------------------------------------------- Tier 3: batched claude CLI + review file

CLAUDE_BIN = "claude"
CLAUDE_MODEL = "sonnet"
CLAUDE_TIMEOUT = 90  # seconds; the plan asks for 60-120s. Another project's own
# batches (up to 15 job postings, much more prompt text per item) use 600s
# for a much bigger unit of work; homograph batches are single sentences,
# tens of them at most, so 90s leaves comfortable headroom without letting
# one hung call block an otherwise-finished audit for ten minutes.

CLAUDE_SYSTEM_PROMPT = (
    "You are a precise linguist choosing which English pronunciation reading "
    "fits a word in a sentence. You reply with raw JSON only: no prose, no "
    "markdown code fences, no commentary. You never invent facts about a "
    "sentence that are not in the text you were given."
)

TIER3_PROMPT_TEMPLATE = textwrap.dedent(
    """\
    Decide which pronunciation reading is correct for the marked word in
    each item below, from context in its own sentence.

    Each item names a word, the sentence it appears in, and two or more
    candidate readings. Each reading is described by its part of speech
    and a rough pronunciation ("sounds roughly like ..."), not exact
    phonetic notation. Pick the reading a fluent English reader would use
    for that word in that sentence.

    {items}
    ## OUTPUT

    Return a raw JSON array with EXACTLY {n} objects, one per item above,
    each shaped:

    {{"index": <int>, "reading": "<the chosen reading name, verbatim>"}}

    index values must be drawn from this list: {ids}
    Output the JSON array and nothing else. No markdown fences. No preamble.
    """
)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

# Part-of-speech tags/parent-tags -> a short, human-readable label, used
# only to build the Tier 3 prompt's plain-English gloss (never to decide a
# reading -- that is Tier 1's job).
_POS_LABELS = {
    "NN": "noun", "NNS": "noun", "NNP": "noun", "NNPS": "noun", "NOUN": "noun",
    "VB": "verb", "VBD": "verb (past tense)", "VBG": "verb (-ing form)",
    "VBN": "verb (past participle)", "VBP": "verb", "VBZ": "verb", "VERB": "verb",
    "JJ": "adjective", "JJR": "adjective", "JJS": "adjective", "ADJ": "adjective",
    "RB": "adverb", "RBR": "adverb", "RBS": "adverb", "ADV": "adverb",
}

# A rough IPA-ish -> plain-letter respelling, built from misaki's own
# symbol table (misaki/en.py: GB_VOCAB, US_VOCAB, DIPHTHONGS) -- not a
# general IPA converter, just enough to turn "wˈWnd" into "wownd" (rhymes
# with "found") and "wˈuːnd" into "woond" (rhymes with "tuned"), matching
# the plan's own worked example of what a plain-English gloss should read
# like. Sorted longest-key-first so two-character sequences (the long
# vowels, "iː", "uː", ...) match before their single-character prefix
# would. Any symbol absent here (the plain consonants b,d,f,h,k,l,m,n,p,
# s,t,v,w,z) passes through unchanged -- English already spells those the
# same way it sounds them.
_PHONEME_RESPELL = {
    "A": "ay", "I": "eye", "O": "oh", "Q": "oh", "W": "ow", "Y": "oy",
    "iː": "ee", "uː": "oo", "ɔː": "aw", "ɜː": "er", "ɑː": "ah",
    "æ": "a", "ɑ": "ah", "ɒ": "o", "ɔ": "aw", "ə": "uh", "ɛ": "eh",
    "ɜ": "er", "ɪ": "ih", "ʊ": "uu", "ʌ": "uh", "ᵊ": "uh", "ᵻ": "ih",
    "ɡ": "g", "ɹ": "r", "ʃ": "sh", "ʒ": "zh", "ʤ": "j", "ʧ": "ch",
    "θ": "th", "ð": "th", "ŋ": "ng", "ɾ": "d", "ʔ": "", "j": "y",
    "ː": "", "ˈ": "", "ˌ": "",
}
_RESPELL_KEYS = sorted(_PHONEME_RESPELL, key=len, reverse=True)


def _approx_pronunciation(phonemes: str | None) -> str:
    """Turn a misaki phoneme string into a rough respelling for a human or
    an LLM prompt -- "sounds roughly like woond", not raw IPA. See the
    module docstring's worked table for verified examples (wound, read,
    minute, live, tear, bow, dove)."""
    if not phonemes:
        return "?"
    out: list[str] = []
    i, n = 0, len(phonemes)
    while i < n:
        for key in _RESPELL_KEYS:
            if phonemes.startswith(key, i):
                out.append(_PHONEME_RESPELL[key])
                i += len(key)
                break
        else:
            out.append(phonemes[i])
            i += 1
    return "".join(out) or "?"


def _pos_categories_for(reading: str, pos_map: dict) -> list[str]:
    labels: list[str] = []
    for tag, mapped in pos_map.items():
        if mapped == reading:
            label = _POS_LABELS.get(tag, tag)
            if label not in labels:
                labels.append(label)
    return labels


def _describe_reading(reading: str, phonemes: str, pos_map: dict, cue_words: list[str] | None) -> str:
    """Build the plain-English gloss the plan asks for: "a reading name
    plus a short gloss ... described in plain English, not in phonemes."
    Combines the part(s) of speech `pos_map` assigns this reading, a rough
    respelling (`_approx_pronunciation`), and, when the inventory entry
    carries them, the cue words this reading tends to appear near."""
    categories = _pos_categories_for(reading, pos_map)
    pos_text = "/".join(categories) if categories else "a reading"
    gloss = f'{pos_text}, sounds roughly like "{_approx_pronunciation(phonemes)}"'
    if cue_words:
        gloss += f" (often appears near words like: {', '.join(cue_words[:5])})"
    return gloss


def _resolve_claude_bin() -> str | None:
    """Return the path to the `claude` CLI, or `None` when it cannot be
    found -- the caller treats `None` as "degrade to the review file",
    never as an error to raise.

    Prefers PATH (another project in this codebase runs bare "claude" and
    relies on it). Falls back to `~/.local/bin/claude` -- the install location
    verified on this machine, `claude` 2.1.232 -- for a subprocess
    environment where PATH has been stripped (a cron job, a bare venv
    invocation): exactly the kind of place `abpipe homographs` runs.
    """
    found = shutil.which(CLAUDE_BIN)
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "claude"
    return str(fallback) if fallback.exists() else None


def _call_claude(prompt: str) -> str | None:
    """Run the `claude` CLI once with `prompt` on stdin. Returns the
    result text, or `None` on ANY failure -- missing binary, non-zero
    exit, a timeout, or an unparseable JSON envelope. Never raises: this
    function's whole job is to hand the caller a clean "did not work"
    signal it can degrade from, the same shape another project's `call_claude`
    uses (verified by reading its `score.py`, which invokes the `claude`
    CLI the same way), except that one raises and lets its own caller
    catch it -- this one swallows the failure itself, because the module
    contract here (Verdict / review file, never a crash) is stricter than
    that project's.
    """
    claude_bin = _resolve_claude_bin()
    if claude_bin is None:
        print("[homograph_tiers] tier3: `claude` CLI not found on PATH or ~/.local/bin", flush=True)
        return None

    cmd = [
        claude_bin, "-p",
        "--output-format", "json",
        "--model", CLAUDE_MODEL,
        "--tools", "",
        "--system-prompt", CLAUDE_SYSTEM_PROMPT,
    ]
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=CLAUDE_TIMEOUT)
    except subprocess.TimeoutExpired:
        print(f"[homograph_tiers] tier3: claude timed out after {CLAUDE_TIMEOUT}s", flush=True)
        return None
    except OSError as exc:
        print(f"[homograph_tiers] tier3: could not run claude: {exc!r}", flush=True)
        return None

    if proc.returncode != 0:
        print(
            f"[homograph_tiers] tier3: claude exited {proc.returncode}: "
            f"{(proc.stderr or proc.stdout)[:300]}",
            flush=True,
        )
        return None
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return proc.stdout or None  # older/other output shape: treat stdout as the text
    if envelope.get("is_error"):
        print(f"[homograph_tiers] tier3: claude reported an error: {str(envelope)[:300]}", flush=True)
        return None
    return envelope.get("result") or None


def _extract_json_array(text: str | None) -> list | None:
    """Pull the outermost JSON array out of a possibly chatty response.
    Copied from another project's `score.py:extract_json_array` (same
    bracket-balancing scan, same markdown-fence stripping) -- the exact
    defensive-parsing shape the plan asks this module to follow."""
    if not text:
        return None
    candidates = [text.strip()]
    for m in _FENCE_RE.finditer(text):
        candidates.append(m.group(1).strip())

    start = text.find("[")
    if start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : i + 1])
                    break

    for cand in candidates:
        try:
            data = json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, list):
            return data
    return None


def _normalise_tier3(data: Any, glosses_by_index: dict[int, dict[str, str]]) -> dict[int, str]:
    """Validate raw parsed model output into `{index: reading}`, dropping
    anything malformed: a missing/non-int index, an index this batch never
    asked about, or a reading name not among that item's own candidates.
    Mirrors another project's `normalize()` -- coerce what is trustworthy, silently
    drop the rest, never raise."""
    out: dict[int, str] = {}
    if not isinstance(data, list):
        return out
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        if index not in glosses_by_index:
            continue
        reading = item.get("reading")
        if reading not in glosses_by_index[index]:
            continue
        out[index] = reading
    return out


def _tier3_llm(
    occurrences: list[Occurrence], inventory: dict, dialect: str
) -> dict[tuple, str]:
    """One batched `claude` CLI call for every occurrence in `occurrences`
    (the whole book's unresolved set, not one call per occurrence, per the
    plan). Returns `{occurrence.key: reading name}` for whatever the model
    resolved; occurrences it did not resolve are simply absent from the
    result, and the caller sends those to the review file."""
    indexed = list(enumerate(occurrences, start=1))
    glosses_by_index: dict[int, dict[str, str]] = {}
    index_to_occ: dict[int, Occurrence] = {}
    item_blocks: list[str] = []

    for index, occ in indexed:
        index_to_occ[index] = occ
        entry = inventory.get(occ.word) or {}
        readings = (entry.get("readings") or {}).get(dialect) or {}
        pos_map = entry.get("pos_map") or {}
        cues = entry.get("cues") or {}
        glosses = {r: _describe_reading(r, phon, pos_map, cues.get(r)) for r, phon in readings.items()}
        glosses_by_index[index] = glosses

        block = [f"### ITEM {index}", f"word: {occ.word}", f"sentence: {occ.context}", "candidate readings:"]
        for reading, gloss in glosses.items():
            block.append(f"  - {reading!r}: {gloss}")
        item_blocks.append("\n".join(block))

    prompt = TIER3_PROMPT_TEMPLATE.format(
        items="\n\n".join(item_blocks), n=len(indexed), ids=[i for i, _ in indexed]
    )

    results: dict[int, str] = {}
    for attempt in (1, 2):
        raw = _call_claude(prompt)
        results = _normalise_tier3(_extract_json_array(raw), glosses_by_index)
        missing = set(glosses_by_index) - set(results)
        if not missing:
            break
        if attempt == 1:
            print(f"[homograph_tiers] tier3: {len(missing)} item(s) missing from claude's response, retrying", flush=True)
            time.sleep(1)
    if missing:
        print(
            f"[homograph_tiers] tier3: {len(missing)} item(s) unresolved after retry; "
            "falling to the review file",
            flush=True,
        )

    return {index_to_occ[index].key: reading for index, reading in results.items()}


def _write_review_file(
    occurrences: list[Occurrence], inventory: dict, dialect: str, review_path: str | os.PathLike | None
) -> None:
    """Write the still-unresolved occurrences to `review_path` as a list a
    person can edit by hand: the word, the context sentence, the candidate
    readings with their plain-English glosses and their real phonemes, and
    an empty `"reading": null` field to fill in. The maintainer's recorded
    decision (plan section 5, Q1): this fallback is always wired in, whether or not
    the LLM call itself ran or succeeded -- the audit never silently
    guesses. A `None` `review_path` is a no-op (a caller that does not want
    a review file, e.g. a unit test)."""
    if review_path is None or not occurrences:
        return

    entries = []
    for occ in occurrences:
        entry = inventory.get(occ.word) or {}
        readings = (entry.get("readings") or {}).get(dialect) or {}
        pos_map = entry.get("pos_map") or {}
        cues = entry.get("cues") or {}
        candidates = [
            {
                "reading": reading,
                "phonemes": phonemes,
                "gloss": _describe_reading(reading, phonemes, pos_map, cues.get(reading)),
            }
            for reading, phonemes in readings.items()
        ]
        entries.append(
            {
                "chapter": occ.chapter,
                "chunk": occ.chunk,
                "word": occ.word,
                "occurrence": occ.occurrence,
                "context": occ.context,
                "class": occ.severity,
                "candidates": candidates,
                "reading": None,
            }
        )

    write_json(review_path, {"schema": 1, "occurrences": entries})
    print(f"[homograph_tiers] tier3: wrote {len(entries)} occurrence(s) to review file {review_path}", flush=True)


# --------------------------------------------------------------------------- the tiers, run in order


def disambiguate(
    occurrences: list[Occurrence],
    inventory: dict,
    texts: dict[tuple[str, str], str],
    dialect: str,
    use_llm: bool = True,
    review_path: str | os.PathLike | None = None,
) -> list[Verdict]:
    """Return one Verdict for each occurrence, in the input order.

    Tier 1 (transformer tagger): tag every distinct chunk once via
    `nlp.pipe()`, compare the trf-mapped reading against misaki's own
    sm-mapped reading (from `baseline_details`, called here with
    `pronunciations=None` -- this function's signature carries no
    pronunciation map, and the tag misaki assigns is unaffected by a
    substitution that never touches a heteronym word, so this is safe;
    see the module docstring for why `homographs.run()` still redoes its
    own baseline call with the real map, for the ground-truth phoneme
    comparison that call needs and this one does not). Agreement at high
    confidence auto-passes; disagreement or an unmapped tag escalates.

    Tier 2 (context cues): for Tier 1 escalations whose inventory entry
    carries a `cues` object, search `Occurrence.context` (the containing
    sentence -- see the module docstring for why). Exactly one reading's
    cues firing resolves it (confidence high when it agrees with Tier 1's
    trf reading, medium when it overrides it); zero or two-or-more
    readings firing, or no `cues` object at all, escalates further.

    Tier 3 (LLM + review file): one batched `claude` CLI call for every
    remaining occurrence in the whole book. `use_llm=False` skips the call
    entirely. Whatever the call does not resolve (missing binary, bad
    JSON, an unknown index/reading, a timeout, `use_llm=False`, or simply
    every occurrence when the call fails outright) is written to
    `review_path` and returned as a `"unresolved"` Verdict with
    `reading=None` -- never a guess.
    """
    if not occurrences:
        return []

    occs_by_chunk: dict[tuple[str, str], list[Occurrence]] = {}
    for occ in occurrences:
        occs_by_chunk.setdefault((occ.chapter, occ.chunk), []).append(occ)

    sm_by_key: dict[tuple, str | None] = {}
    compound_by_key: dict[tuple, str | None] = {}
    for key, occs in occs_by_chunk.items():
        details = baseline_details(texts.get(key, ""), occs, dialect, None)
        for occ_key, detail in details.items():
            sm_by_key[occ_key] = detail.get("tag")
            compound_by_key[occ_key] = detail.get("compound")

    trf_by_key, trf_elapsed, n_chunks = _tier1_tag(occurrences, texts)
    rate = n_chunks / trf_elapsed if trf_elapsed > 0 else float("inf")
    print(
        f"[homograph_tiers] tier1: tagged {n_chunks} chunk(s) with en_core_web_trf "
        f"in {trf_elapsed:.2f}s ({rate:.1f} chunks/s)",
        flush=True,
    )

    verdicts: list[Verdict] = []
    tier2_pending: list[Occurrence] = []

    for occ in occurrences:
        compound_token = compound_by_key.get(occ.key)
        if compound_token:
            # Not forceable: misaki has already fused this word into a
            # larger compound token ("wind-swept", "close-cropped") and
            # phonemized it as a unit -- see baseline_details. Word-level
            # `[word](/phonemes/)` markup would split that token and
            # change what the engine says to something nobody chose,
            # not correct the reading. This occurrence gets its own
            # verdict state and skips every tier -- Tier 2's cues, Tier
            # 3's LLM call, and the review file -- entirely; sending it
            # to any of those would either waste a call on something no
            # decision can act on, or, worse, write a decision that
            # `apply_homographs` would apply wrongly. Worker H1's run()
            # excludes "not_applicable" from the failure count and
            # reports it in its own bucket.
            verdicts.append(
                Verdict(
                    occurrence=occ, reading=None, phonemes=None,
                    tier="not_applicable", confidence="low",
                    detail=f"inside the compound {compound_token!r}; not forceable by word-level markup",
                )
            )
            continue

        entry = inventory.get(occ.word) or {}
        pos_map = entry.get("pos_map") or {}
        readings = (entry.get("readings") or {}).get(dialect) or {}

        sm_tag = sm_by_key.get(occ.key)
        trf_tag = (trf_by_key.get(occ.key) or {}).get("trf_tag")
        sm_reading = _reading_for_tag(sm_tag, pos_map)
        trf_reading = _reading_for_tag(trf_tag, pos_map)

        if sm_reading is not None and trf_reading is not None and sm_reading == trf_reading:
            phonemes = readings.get(sm_reading)
            if phonemes is not None:
                verdicts.append(
                    Verdict(
                        occurrence=occ, reading=sm_reading, phonemes=phonemes,
                        tier="tier1", confidence="high",
                        detail=f"sm tag {sm_tag!r} and trf tag {trf_tag!r} both -> {sm_reading!r}",
                    )
                )
                continue

        tier2_pending.append(occ)

    tier3_pending: list[Occurrence] = []

    for occ in tier2_pending:
        entry = inventory.get(occ.word) or {}
        cues = entry.get("cues") or {}
        readings = (entry.get("readings") or {}).get(dialect) or {}
        pos_map = entry.get("pos_map") or {}
        trf_tag = (trf_by_key.get(occ.key) or {}).get("trf_tag")
        trf_reading = _reading_for_tag(trf_tag, pos_map)

        if not cues:
            tier3_pending.append(occ)
            continue

        hits = _cue_hits(occ.context, cues)
        if len(hits) != 1:
            tier3_pending.append(occ)  # zero or ambiguous cue matches -- never guess
            continue

        reading, keywords = next(iter(hits.items()))
        phonemes = readings.get(reading)
        if phonemes is None:
            tier3_pending.append(occ)
            continue

        confidence = "high" if trf_reading == reading else "medium"
        verdicts.append(
            Verdict(
                occurrence=occ, reading=reading, phonemes=phonemes,
                tier=f"tier2:cue:{keywords[0]}", confidence=confidence,
                detail=f"cue window matched {keywords!r} for reading {reading!r} "
                f"(trf reading was {trf_reading!r})",
            )
        )

    if tier3_pending:
        llm_results: dict[tuple, str] = {}
        if use_llm:
            llm_results = _tier3_llm(tier3_pending, inventory, dialect)

        still_unresolved: list[Occurrence] = []
        for occ in tier3_pending:
            entry = inventory.get(occ.word) or {}
            readings = (entry.get("readings") or {}).get(dialect) or {}
            reading = llm_results.get(occ.key)
            if reading is not None and reading in readings:
                verdicts.append(
                    Verdict(
                        occurrence=occ, reading=reading, phonemes=readings[reading],
                        tier="tier3:llm", confidence="medium",
                        detail="resolved by a batched `claude` CLI call",
                    )
                )
            else:
                still_unresolved.append(occ)

        if still_unresolved:
            _write_review_file(still_unresolved, inventory, dialect, review_path)
            for occ in still_unresolved:
                verdicts.append(
                    Verdict(
                        occurrence=occ, reading=None, phonemes=None,
                        tier="unresolved", confidence="low",
                        detail="no tier resolved this occurrence; written to the review file",
                    )
                )

    return verdicts
