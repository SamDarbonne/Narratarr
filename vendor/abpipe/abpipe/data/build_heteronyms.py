"""Build abpipe/data/heteronyms.json from misaki's own gold lexicons.

Run this module as `python -m abpipe.data.build_heteronyms` from the project
root. The command is one-shot and re-runnable: it always writes the whole
file again, from the lexicons plus the CURATION table below. **A re-run must
never lose curated data.** CURATION lives in this file, not in the JSON, so
a re-run always re-applies it. Never hand-edit heteronyms.json; edit
CURATION and run this module again.

Why the misaki gold lexicon is the source, not a general heteronym list:
the gold lexicon IS the decision table `misaki.en.Lexicon.lookup()` actually
uses at render time (read from `.venv/…/misaki/en.py:228`, misaki 0.9.4).
An inventory built from it is complete with respect to what the engine can
get wrong, and it already carries the exact phoneme strings a render needs
to force a reading, in both dialects (gb for lang_code "b", us for lang_code
"a"). A generic word list would still need every entry mapped into misaki's
own compressed, non-IPA phoneme alphabet -- the gold lexicon skips that step
because it already speaks that alphabet.

Two families of dict-valued gold entries never reach the inventory:

1. **ALL-CAPS abbreviation entries** ("AA", "ABS", …). `word.islower()`
   drops them; they are initialisms, not heteronyms of running prose.
2. **Prosodic weak/strong-form entries** ("be", "has", "would", "that", …,
   33 words measured). Their only non-DEFAULT key is "None" or "DT". "None"
   is not a part-of-speech tag -- it is misaki's own flag for
   `ctx.future_vowel is None` (`Lexicon.lookup()`, en.py:237), the "nothing
   else is known about what follows" case. Every one of the 33 is stress-only
   (class C) once stress marks are stripped, and misaki already resolves the
   alternation itself from sentence context, not from a POS tag. A homograph
   disambiguator keyed on POS tags has nothing to add for these, so they are
   left out rather than padding the inventory with entries a consumer could
   never usefully act on. Measured: 723 lowercase dict entries per dialect,
   723 - 33 = 690 feed the automatic classifier.

## The reading-name algorithm, and why it is conservative

Each dict entry maps a Penn tag (or a misaki *parent* tag: NOUN, VERB, ADJ,
ADV -- see `Lexicon.get_parent_tag()`, en.py:203) to a phoneme string, plus
one DEFAULT. Step 4 of the task asks for readings grouped and named
"verb"/"noun"/"adjective"/"default"/"alt1"/"alt2" by inspecting which tags
share a string. Measured over all 690 words: every *explicit* non-DEFAULT
tag already falls cleanly into one family (verb/noun/adjective/adverb by
prefix or literal parent tag) -- alt1/alt2 never actually triggers on the
current lexicon. It stays in the code as a defensive fallback for a future
misaki update that adds a tag shape this module has not seen.

DEFAULT itself carries no tag of its own, so its name is inferred by
complement: when exactly one other family is present, DEFAULT takes the
"classic heteronym" opposite (verb <-> noun; adjective -> noun; adverb ->
adjective) -- this reproduces "wound" DEFAULT="noun" opposite its VBD/VBN/VBP
"verb" family, and "produce" DEFAULT="verb" opposite its NOUN "noun" family.
**When two or more other families are already present, DEFAULT is named the
literal "default"** rather than guessed, and pos_map is built ONLY from the
literal tags misaki's own dict lists (no expansion to the full Penn family).
This matters for correctness, not just tidiness: "read" has both an ADJ tag
and VBD/VBN/VBP explicitly present (all four sharing one string, the
past-tense/adjective reading /rɛd/), and its own DEFAULT is a *second, still
distinct* verb reading -- the present tense /riːd/. Guessing "noun" for that
DEFAULT (the naive complement of nothing-in-particular) would be wrong, and
blindly expanding VBD/VBN/VBP's family to the full verb tag set (VB, VBZ,
VBG) would wrongly steer "I read this book every year" (VBP) onto the past
reading too. "read" and "close" are hand-curated below instead (§ note in
CURATION) rather than left to a guess the algorithm cannot make safely.

**Expansion to the full standard Penn tag set (e.g. VBD,VBN,VBP -> the whole
VB/VBD/VBG/VBN/VBP/VBZ family) only happens when exactly one other family is
present** -- the case with no collision risk, matching the worked example in
the task brief (`wound`'s pos_map lists NN, NNS, VB, VBZ, VBG even though
misaki's own dict only ever lists VBD, VBN, VBP explicitly).

## Classification (class A/B/C)

Per entry: strip stress marks (U+02C8, U+02CC). If every reading is then
byte-identical, class C (stress only, e.g. "record"/"present" as the plan
already judges them -- see the CURATION overrides below). Otherwise compare
the *vowel-symbol sequence* of each reading, built from misaki's own VOWELS
set (`misaki.en.VOWELS`, which already includes the compressed diphthong
letters A/I/O/Q/W/Y). Differing vowel sequences -> class A (gross vowel
difference). Same vowel sequence, different consonants -> class B (voicing
or a final consonant, e.g. "house" /s~z/).

Classification runs on the **gb** reading set (lang_code "b", Book A's
dialect) when gb has 2+ distinct strings, falling back to us otherwise. Both
dialects were measured to have parallel structure for all 690 words (same
tag keys, same family split) except three (compact, inside, obverse) where
gb and us group the SAME tags into a different number of distinct strings;
the family-based namer (not a string-cohort namer) sidesteps that split
automatically -- see the module's worker report for the measurement.

## CURATION

`CURATION` is a plain dict, keyed by word, of *overrides* merged over the
generated entry (shallow merge: a key present in the override replaces the
generated key outright; a key absent is left as the generator made it). Any
word CURATION touches gets `"curated": true` set automatically. Three uses:

1. **A class override** — the automatic rule sometimes measures "gross vowel
   difference" where a stress-driven reduction is the honest description
   (see the note on each such entry). Only `class` and `note` are set; the
   generated readings/pos_map are kept.
2. **A reading-name override** — "read" and "close" (see the algorithm note
   above): the whole `readings`/`pos_map`/`default_reading` is replaced by a
   hand-checked version.
3. **A `missing_from_misaki` entry** — a word where misaki has only ONE
   reading for the whole word (not dict-valued at all), so no POS tag can
   ever recover the missing sense. These entries are built from scratch:
   every hand-written phoneme string here was derived from a real,
   currently-in-lexicon rhyme (documented in each `note`) and then round-
   tripped through `misaki.en.G2P` with the `[word](/phonemes/)` inline
   override markup, confirming the token's `.phonemes` equals the string
   and that no `[`, `]`, `(`, `)`, `/` leaks into the output. That check is
   mandatory per the task brief; it was run for every entry below (see the
   worker report, not reproduced here to keep this module import-light).
"""

from __future__ import annotations

import json
from importlib import metadata as importlib_metadata
from importlib import resources as importlib_resources
from pathlib import Path
from typing import Any

from misaki.en import VOWELS

SCHEMA = 1
OUT_PATH = Path(__file__).with_name("heteronyms.json")

# ˈ = primary stress (U+02C8), ˌ = secondary stress (U+02CC).
STRESS_MARKS = "ˈˌ"

# Tags whose ONLY job is to flag "no sentence context is known" (misaki's
# own `ctx.future_vowel is None` branch, en.py:237) or a determiner reading
# of "that". Neither is a part-of-speech signal a tagger could hand to a
# disambiguator, so a word whose non-DEFAULT tags are entirely drawn from
# this set is a prosodic weak/strong-form pair, not a heteronym -- excluded.
# Refer to the module docstring for the measurement (33 words, all class C).
_NON_POS_TAGS = frozenset({"None", "DT"})

# The standard Penn tag family for each name this module can assign. Used
# only to EXPAND a cleanly-named group's pos_map when there is no collision
# risk (exactly one other family present besides DEFAULT). Refer to the
# module docstring, "Expansion to the full standard Penn tag set".
#
# "noun" includes NNP/NNPS (proper-noun tags) alongside NN/NNS: a real tagger
# (en_core_web_trf) tags a capitalised common noun NNP whenever it heads a
# proper-name-shaped phrase -- Book A calls its setting "the House",
# and every sentence-initial heteronym gets NNP too. Measured: 0 of 701
# entries carried NNP/NNPS before this fix, which was the single largest
# driver of tier-1 audit occurrences falling through unresolved (a real
# audit run measured 20 of 61 unresolved in one chapter, traced to this).
_FAMILY_PENN_TAGS: dict[str, list[str]] = {
    "verb": ["VB", "VBD", "VBG", "VBN", "VBP", "VBZ"],
    "noun": ["NN", "NNS", "NNP", "NNPS"],
    "adjective": ["JJ", "JJR", "JJS"],
    "adverb": ["RB", "RBR", "RBS"],
}

# The complement name for DEFAULT when exactly one other family is present.
# "verb" opposite is "noun" and vice-versa (the huge majority pattern:
# wound, house, mouth, use, dove, bow, sow, produce, content, refuse, …).
# "adjective" opposite is "noun" (minute, bass). "adverb" opposite is
# "adjective" (overall, underground, uphill -- rare compounds; the guess
# barely matters for these since they are unlikely to occur often, but a
# real guess is still better than a meaningless one).
_DEFAULT_COMPLEMENT: dict[str, str] = {
    "verb": "noun",
    "noun": "verb",
    "adjective": "noun",
    "adverb": "adjective",
}


def _tag_family(tag: str) -> str | None:
    """Return the reading family a dict key names, or None.

    Accepts both a raw Penn tag (VBD, NNS, JJR, RBR, …) and misaki's own
    literal parent-tag key (VERB, NOUN, ADJ, ADV -- see
    `Lexicon.get_parent_tag()`, en.py:203). Returns None for DEFAULT, and
    for the non-POS tags in _NON_POS_TAGS (the caller filters those first).
    """
    if tag == "VERB" or tag.startswith("VB"):
        return "verb"
    if tag == "NOUN" or tag.startswith("NN"):
        return "noun"
    if tag == "ADJ" or tag.startswith("JJ"):
        return "adjective"
    if tag == "ADV" or tag.startswith("RB"):
        return "adverb"
    return None


def strip_stress(ps: str) -> str:
    """Return the phoneme string with every stress mark removed."""
    return "".join(c for c in ps if c not in STRESS_MARKS)


def vowel_sequence(ps: str) -> str:
    """Return only the vowel symbols of a phoneme string, in order.

    Uses misaki's own VOWELS set, so a diphthong letter like W or Q counts
    correctly -- the plan (§1.4) calls this comparison "gross vowel
    difference", and a wrong vowel alphabet would silently under-count it.
    """
    return "".join(c for c in ps if c in VOWELS)


def classify_class(strings: list[str]) -> str:
    """Return "A", "B", or "C" for a set of distinct phoneme readings.

    C: identical once stress is stripped (stress-only difference).
    A: the vowel-symbol sequences differ (a gross, audible difference).
    B: same vowel sequence, different consonants (voicing or a final
       consonant -- audible, but subtle. Refer to CONTRACT §1.4's own
       "house"/"use"/"close"/"mouth" examples, which this rule reproduces).
    """
    stripped = {strip_stress(s) for s in strings}
    if len(stripped) == 1:
        return "C"
    vowel_seqs = {vowel_sequence(s) for s in strings}
    if len(vowel_seqs) > 1:
        return "A"
    return "B"


def _load_gold(package_data_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (gb_gold, us_gold), the two raw misaki lexicon dicts."""
    gb = json.loads((package_data_dir / "gb_gold.json").read_text(encoding="utf-8"))
    us = json.loads((package_data_dir / "us_gold.json").read_text(encoding="utf-8"))
    return gb, us


def _misaki_data_dir() -> Path:
    """Return the directory holding misaki's gb_gold.json / us_gold.json."""
    # misaki ships its lexicons as package data (misaki/data/*.json). This
    # walks through importlib.resources rather than hard-coding a venv path,
    # so the generator survives a future misaki version bump or a different
    # virtualenv layout.
    data_traversable = importlib_resources.files("misaki") / "data"
    with importlib_resources.as_file(data_traversable) as data_path:
        return Path(data_path)


def _build_auto_entry(
    word: str, gb_entry: dict[str, Any] | None, us_entry: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Build one inventory entry from a word's raw gb/us dict-valued entries.

    Returns None when the word does not qualify: fewer than 2 distinct
    phoneme strings in EITHER dialect, or every non-DEFAULT tag is a
    prosodic weak/strong-form flag (refer to _NON_POS_TAGS).
    """
    # At least one dialect must actually hold the dict-valued entry. A word
    # present as a dict in only one dialect is not yet observed in the real
    # lexicons (measured: 0 of 723 words), but the branch below still
    # degrades gracefully -- the missing dialect's readings stay {} and the
    # merge step later records it as a gap, per task step 5.
    reference = gb_entry if gb_entry is not None else us_entry
    if reference is None:
        return None

    tags = set(reference.keys())
    non_default = tags - {"DEFAULT"}
    if non_default and non_default <= _NON_POS_TAGS:
        return None  # a prosodic weak/strong-form pair, not a heteronym

    # Group the explicit non-DEFAULT tags by family (verb/noun/adjective/
    # adverb). A tag whose family cannot be determined is defensive-only on
    # the current lexicon (measured: never triggers) and becomes alt1/alt2.
    family_tags: dict[str, list[str]] = {}
    unnamed_tags: list[str] = []
    for tag in sorted(non_default - _NON_POS_TAGS):
        family = _tag_family(tag)
        if family is None:
            unnamed_tags.append(tag)
        else:
            family_tags.setdefault(family, []).append(tag)

    real_families = set(family_tags)
    reading_name_of_family = {family: family for family in real_families}
    alt_name_of_tag = {tag: f"alt{i}" for i, tag in enumerate(unnamed_tags, start=1)}

    # Name DEFAULT by complement when it is unambiguous (exactly one other
    # family present); otherwise use the literal "default" rather than
    # guess among two or more candidates. Refer to the module docstring.
    if len(real_families) == 1:
        (only_family,) = real_families
        default_name = _DEFAULT_COMPLEMENT[only_family]
    else:
        default_name = "default"

    # Build readings per dialect. Every family's value must be the SAME
    # phoneme string across every tag mapped to it, within one dialect --
    # true for all 690 words (measured; see the module docstring) -- so the
    # first tag's value stands for the whole family.
    readings: dict[str, dict[str, str]] = {"gb": {}, "us": {}}
    for dialect, entry in (("gb", gb_entry), ("us", us_entry)):
        if entry is None:
            continue
        readings[dialect][default_name] = entry["DEFAULT"]
        for family, raw_tags in family_tags.items():
            readings[dialect][reading_name_of_family[family]] = entry[raw_tags[0]]
        for tag, alt_name in alt_name_of_tag.items():
            readings[dialect][alt_name] = entry[tag]

    # pos_map: always include the literal tags misaki's own dict lists.
    pos_map: dict[str, str] = {}
    for family, raw_tags in family_tags.items():
        for tag in raw_tags:
            pos_map[tag] = reading_name_of_family[family]
    for tag, alt_name in alt_name_of_tag.items():
        pos_map[tag] = alt_name

    # Safe expansion to the full standard Penn family -- only when there is
    # exactly one other family, so expanding it (and DEFAULT's complement)
    # cannot collide with a second, differently-tensed/differently-cased
    # reading hiding under DEFAULT. Refer to the module docstring's "read"
    # example for why this guard exists.
    if len(real_families) == 1:
        (only_family,) = real_families
        for penn_tag in _FAMILY_PENN_TAGS[only_family]:
            pos_map.setdefault(penn_tag, only_family)
        if default_name in _FAMILY_PENN_TAGS:
            for penn_tag in _FAMILY_PENN_TAGS[default_name]:
                pos_map.setdefault(penn_tag, default_name)

    # Classify on gb when it has 2+ distinct strings, else fall back to us.
    gb_strings = list(readings["gb"].values())
    us_strings = list(readings["us"].values())
    class_source = gb_strings if len(set(gb_strings)) >= 2 else us_strings
    if len(set(class_source)) < 2:
        return None  # neither dialect actually has 2+ distinct readings

    return {
        "class": classify_class(class_source),
        "readings": readings,
        "pos_map": pos_map,
        "default_reading": default_name,
        "missing_from_misaki": False,
    }


def _generate_from_lexicons(gb_gold: dict[str, Any], us_gold: dict[str, Any]) -> dict[str, Any]:
    """Return {word: entry} for every qualifying lowercase dict entry."""
    words = sorted(
        w
        for w in set(gb_gold) | set(us_gold)
        if w.islower()
        and (isinstance(gb_gold.get(w), dict) or isinstance(us_gold.get(w), dict))
    )
    entries: dict[str, Any] = {}
    for word in words:
        gb_entry = gb_gold.get(word)
        us_entry = us_gold.get(word)
        gb_entry = gb_entry if isinstance(gb_entry, dict) else None
        us_entry = us_entry if isinstance(us_entry, dict) else None
        built = _build_auto_entry(word, gb_entry, us_entry)
        if built is not None:
            entries[word] = built
    return entries


# ============================================================== CURATION ==
#
# Every word below is documented at the point of use with a `note`. Section
# references are to the plan at proofkit/plans/tts-audiobook/homographs.md.

CURATION: dict[str, dict[str, Any]] = {
    # -------------------------------------------------------- class overrides --
    # These four are the plan's own §1.4 class-C examples ("least audible").
    # The automatic rule measures class A here because the FIRST syllable's
    # vowel changes symbol when it loses stress (e.g. record: ɛ stressed ->
    # ɪ unstressed) -- but the syllable that KEEPS the primary stress uses
    # the identical vowel symbol in both readings (record's second syllable
    # is ɔː either way). That is the textbook signature of a pure stress
    # shift with the automatic English reduction that accompanies it, not
    # an independent vowel choice -- so class C is the honest call, matching
    # the plan directly.
    # Even a stress-only (class C) pair is worth a cue: tier 1 escalates
    # whenever the two taggers disagree, and a cue can resolve the
    # escalation with high precision rather than falling to tier 3.
    "present": {
        "class": "C",
        "cues": {
            "verb": ["award", "hand over", "offer", "introduce", "unveil", "will present"],
            "noun": ["gift", "wrapped", "birthday", "christmas", "moment", "currently", "time being"],
        },
        "note": "stress-shift pair; plan §1.4 names this class C directly.",
    },
    "record": {"class": "C", "note": "stress-shift pair; plan §1.4 names this class C directly."},
    "suspect": {"class": "C", "note": "stress-shift pair; plan §1.4 names this class C directly."},
    "perfect": {"class": "C", "note": "stress-shift pair; plan §1.4 names this class C directly."},
    # Same clean stress-shift signature as record/present (checked by hand:
    # the syllable that keeps the primary stress uses the same vowel symbol
    # in both readings), so the same reasoning extends to these six.
    "desert": {
        "class": "C",
        "cues": {
            "verb": ["abandon", "abandoned", "post", "duty", "troops", "flee", "fled", "left behind"],
            "noun": ["sand", "sahara", "oasis", "dunes", "arid", "camel", "cactus"],
        },
        "note": "stress-shift pair, same signature as record/present (§1.4).",
    },
    "object": {
        "class": "C",
        "cues": {
            "verb": ["protest", "protested", "objected", "disagree", "oppose", "i object"],
            "noun": ["shape", "shiny", "strange", "mysterious", "metal", "found an"],
        },
        "note": "stress-shift pair, same signature as record/present (§1.4).",
    },
    "subject": {
        "class": "C",
        "cues": {
            "verb": ["subjugate", "conquer", "rule over", "subjected", "will subject"],
            "noun": ["topic", "matter", "school", "favorite", "discuss", "change the"],
        },
        "note": "stress-shift pair, same signature as record/present (§1.4).",
    },
    "content": {
        "class": "C",
        "cues": {
            "noun": ["table of", "contents", "chapters", "pages", "material", "webpage"],
            "verb": ["satisfied", "happy", "at peace", "himself with", "herself with", "content with"],
        },
        "note": "stress-shift pair, same signature as record/present (§1.4).",
    },
    "contract": {"class": "C", "note": "stress-shift pair, same signature as record/present (§1.4)."},
    "project": {"class": "C", "note": "stress-shift pair, same signature as record/present (§1.4)."},
    "produce": {"class": "C", "note": "stress-shift pair, same signature as record/present (§1.4)."},
    # "refuse" combines a record-style stress shift with an INDEPENDENT
    # final-consonant voicing change (verb …fjuːz vs noun …fjuːs) -- the
    # same s/z pattern the plan calls class B for excuse/abuse (§1.4). The
    # voicing half is real and audible regardless of stress, so B, not C.
    "refuse": {
        "class": "B",
        "cues": {
            "noun": ["garbage", "rubbish", "trash", "waste", "collection", "dump", "heap"],
            "verb": ["decline", "declined", "reject", "rejected", "won't", "wouldn't", "refuses to"],
        },
        "note": "the noun/verb split also voices the final consonant (z/s), like excuse/abuse (§1.4) -- not pure stress.",
    },

    # ---------------------------------------------------- cue-only additions --
    # These words are already auto-generated with a correct class and correct
    # reading names (verified against the printed inventory before writing
    # these cues) -- POS alone (or trf-vs-sm tagger agreement) cannot always
    # resolve them, so tier 2 needs a cue list. Every list here is high
    # precision by design: a false cue forces a wrong pronunciation, which
    # the audit's own tier design treats as worse than escalating further.
    #
    # EXCEPTION: "wound" below also carries a `pos_map` override, not cues
    # alone -- the automatic pos_map was measurably wrong (see its own note).
    # It stays in this section because everything else about it (class,
    # reading names) is the ordinary cue-only case.
    #
    # "wound" is the crux case (CONTRACT / plan §1.1, §2.5's central exit
    # criterion: ch01/0059 "a white muffler wound round and round his neck"
    # must resolve to the verb reading). The plan measured round/around
    # beside "wound" as precision 1.0 for the verb sense on this corpus.
    #
    # POS_MAP OVERRIDE, not a cue-only entry -- "wound" spells TWO different
    # verbs, and the automatic full-family expansion (§ module docstring,
    # "Expansion to the full standard Penn tag set") wrongly merged them.
    # Caught on a real book (Book C, ch18/0062): "...and
    # kill and wound each other, there must be a remedy..." -- "to wound"
    # (injure, VB) was force-corrected wˈund -> wˈWnd, which makes the book
    # say "kill and WOWND each other". misaki's baseline (wˈund) was already
    # correct; the fault was ours.
    #   - wind (past tense / past participle of "to wind", e.g. "the muffler
    #     wound round his neck") -- misaki's own dict explicitly lists this
    #     as VBD/VBN/VBP -> wˈWnd.
    #   - wound (the plain verb "to wound", injure, e.g. "to kill and wound
    #     each other") -- homophonous with the noun "a wound", both wˈund.
    #     misaki's dict never lists VB/VBG/VBZ at all; DEFAULT (wˈund) is
    #     the only reading that covers them.
    # The automatic namer saw only the VBD/VBN/VBP family, had no collision
    # signal, and expanded it to the WHOLE verb tag set (VB, VBD, VBG, VBN,
    # VBP, VBZ) per the "exactly one other family present" safe-expansion
    # rule -- safe for a word with one verb sense, wrong here because "wound"
    # has two. Fixed by hand: VB/VBG/VBZ point at "noun" (wˈund, the injure
    # sense), VBD/VBN stay on "verb" (wˈWnd, the wind-past sense). VBP is
    # left on "verb" too, matching misaki's own gold table -- misaki appears
    # to use VBP there as a hedge for a mistagged past form; that judgement
    # is misaki's to make, not this generator's, so it is kept as-is rather
    # than "corrected" a second time.
    # DO NOT re-expand VB/VBG/VBZ back onto "verb" to "complete" the family --
    # that is the exact defect this override exists to prevent. Refer to the
    # WARNING above the missing_from_misaki entries for the same species of
    # trap in a different shape (a tag left off on purpose is a feature).
    "wound": {
        "pos_map": {
            "NN": "noun", "NNS": "noun",
            "VB": "noun", "VBG": "noun", "VBZ": "noun",
            "VBD": "verb", "VBN": "verb", "VBP": "verb",
        },
        "cues": {
            "verb": ["round", "around", "up", "down", "through", "about", "tightly", "twice"],
            "noun": ["bullet", "knife", "gaping", "gunshot", "heal", "healed", "dressing", "bandage", "open", "deep", "flesh"],
        },
        "note": (
            "'wound' spells two different verbs (wind-past vs. the injure "
            "verb) sharing one spelling; the automatic full-family "
            "expansion wrongly merged VB/VBG/VBZ (injure, wund) onto the "
            "VBD/VBN/VBP family (wind-past, wWnd). Caught on America's "
            "First Cuisines ch18/0062: \"...and kill and wound each other, "
            "there must be a remedy...\" was force-corrected from misaki's "
            "correct baseline wund to the wrong wWnd. Fixed here: VB/VBG/"
            "VBZ -> noun (wund, injure); VBD/VBN -> verb (wWnd, wind-past); "
            "VBP left on verb, matching misaki's own gold table as-is (a "
            "hedge for a mistagged past form, misaki's judgement, not "
            "ours). Do not re-expand VB/VBG/VBZ back onto verb."
        ),
    },
    "wind": {
        "cues": {
            "verb": ["clock", "watch", "spring", "gears", "crank", "handle", "unwind", "tightly"],
            "noun": ["blew", "blowing", "gust", "breeze", "gale", "howling", "northerly", "southerly"],
        },
    },
    "tear": {
        "cues": {
            "verb": ["rip", "ripped", "shred", "tore", "torn", "apart", "fabric"],
            "noun": ["cheek", "eye", "eyes", "crying", "sobbing", "wept", "rolled down", "wiped"],
        },
    },
    "bow": {
        "cues": {
            "verb": ["curtsy", "bowed", "audience", "stage", "respect", "before", "low"],
            "noun": ["ribbon", "arrow", "violin", "ship's", "archer", "hair", "tie a"],
        },
    },
    "dove": {
        "cues": {
            "verb": ["dove into", "dove under", "dove off", "plunged", "water", "pool"],
            "noun": ["white", "peace", "wings", "cooed", "nest", "olive branch"],
        },
    },
    "minute": {
        "cues": {
            "adjective": ["tiny", "microscopic", "infinitesimal", "smallest", "particle", "trace", "detail", "scraps", "scrap"],
            "noun": ["clock", "hour", "second", "wait a", "five", "ten", "half a"],
        },
    },
    "bass": {
        "cues": {
            "adjective": ["guitar", "voice", "drum", "speaker", "baritone", "deep tone", "singer"],
            "noun": ["fish", "fishing", "lake", "river", "caught", "bait", "hook", "reel"],
        },
    },
    "sow": {
        "cues": {
            "verb": ["seed", "seeds", "field", "crop", "plant", "harvest", "reap"],
            "noun": ["pig", "piglet", "litter", "boar", "sty"],
        },
    },
    "use": {
        "cues": {
            "noun": ["of no use", "any use", "what use", "no further use", "some use", "little use"],
            "verb": ["to use", "please use", "can use", "will use"],
        },
    },
    "house": {
        "cues": {
            "verb": ["shelter", "accommodate", "inmates", "refugees", "prisoners", "livestock", "will house"],
            "noun": ["roof", "door", "window", "chimney", "cottage", "farmhouse"],
        },
    },
    "mouth": {
        "cues": {
            "verb": ["silently", "mouthed", "words", "without speaking", "voicelessly"],
            "noun": ["lips", "teeth", "tongue", "chin", "kissed", "smiled", "wide open"],
        },
    },
    "moderate": {
        "cues": {
            "verb": ["moderate the", "will moderate", "moderating", "panel", "debate", "discussion", "chair"],
            "noun": ["political", "moderates", "centrist"],
        },
    },
    "separate": {
        "cues": {
            "verb": ["separate the", "will separate", "separating", "divorce", "split up", "part ways"],
            "noun": ["rooms", "beds", "entrances", "occasions", "kept separate"],
        },
    },

    # ------------------------------------------------- reading-name overrides --
    # "read": the automatic namer cannot place DEFAULT safely. Non-DEFAULT
    # tags are ADJ + VBD + VBN + VBP, all four sharing ONE string (the past
    # tense / adjective reading, /rɛd/) -- that is a single real family
    # ("verb", since VBD/VBN/VBP outnumber the lone ADJ), so DEFAULT's
    # complement would be "noun". But DEFAULT here ('ɹˈiːd' gb / 'ɹˈid' us)
    # is not a noun at all -- it is the PRESENT tense, /riːd/ ("I read this
    # every year"). Naming it by hand, and mapping VB/VBZ/VBG to it
    # explicitly (never expanding VBD/VBN/VBP's family onto them) is the
    # only way to avoid steering present-tense "read" onto the wrong sound.
    # DELIBERATE DEPARTURE FROM MISAKI'S OWN TABLE, not a copying error: gb
    # gold lists VBP -> past ('ɹˈɛd'). VBP is grammatically the non-3rd-
    # person PRESENT tense ("I read books every day"), so mapping it to the
    # past sound is linguistically wrong -- misaki appears to hedge this
    # because its small tagger (en_core_web_sm) routinely mislabels past
    # "read" as VBP. Measured with en_core_web_trf on 8 "read" sentences:
    # trf tags every one correctly (present -> VB/VBP, past -> VBD/VBN), so
    # copying misaki's hedge into this pos_map would make tier 1 auto-pass a
    # silently wrong reading whenever trf and misaki's own sm both say VBP.
    # A later reader must not "fix" this back to match misaki's table.
    "read": {
        "class": "A",
        "readings": {
            "gb": {"present": "ɹˈiːd", "past": "ɹˈɛd"},
            "us": {"present": "ɹˈid", "past": "ɹˈɛd"},
        },
        "pos_map": {
            "VB": "present", "VBZ": "present", "VBG": "present", "VBP": "present",
            "VBD": "past", "VBN": "past", "JJ": "past",
        },
        "cues": {
            "past": ["had", "has", "have", "having", "yesterday", "already", "once", "aloud", "just", "never", "then", "last", "when"],
            "present": ["can", "could", "will", "would", "cannot", "must", "always", "every", "often", "usually", "learn", "write"],
        },
        "default_reading": "present",
        "missing_from_misaki": False,
        "note": "VBP is deliberately mapped to 'present', NOT to misaki's own gold-table 'past' hedge -- see the comment above this entry. Also: DEFAULT is a SECOND verb tense (present /riːd/), not the complement of the ADJ+VBD+VBN+VBP group (past/adjective /rɛd/); the automatic namer cannot express a two-verb-tense split safely, so this entry is hand-checked.",
    },
    # "reread" inherits read's exact defect (same root word, same VBP hedge
    # in misaki's own table) plus a NOUN sense ("a reread") that shares
    # present's pronunciation. Same VBP fix, same cue lists (reused as-is --
    # "had reread"/"will reread" work identically to "had read"/"will read").
    "reread": {
        "class": "A",
        "readings": {
            "gb": {"present": "ɹiːɹˈiːd", "past": "ɹiːɹˈɛd", "noun": "ɹˈiːɹiːd"},
            "us": {"present": "ɹˌiɹˈid", "past": "ɹiɹˈɛd", "noun": "ɹˈiɹid"},
        },
        "pos_map": {
            "VB": "present", "VBZ": "present", "VBG": "present", "VBP": "present",
            "VBD": "past", "VBN": "past",
            "NN": "noun", "NNS": "noun",
        },
        "cues": {
            "past": ["had", "has", "have", "having", "yesterday", "already", "once", "aloud", "just", "never", "then", "last", "when"],
            "present": ["can", "could", "will", "would", "cannot", "must", "always", "every", "often", "usually"],
        },
        "default_reading": "present",
        "missing_from_misaki": False,
        "note": "Same species of defect as 'read' (misaki's own VBP key maps to the past reading; corrected here to present -- refer to the 'read' entry's comment). Rare word; included for consistency with 'read' rather than for measured impact.",
    },
    # "close": non-DEFAULT tags are NOUN + VERB, both sharing ONE string --
    # two real families present, so the automatic namer leaves DEFAULT as
    # the literal "default" rather than guess. DEFAULT ('klˈQs' gb /
    # 'klˈOs' us) is the adjective ("a close call", "stay close"), so it is
    # named by hand instead of left generic.
    "close": {
        "class": "B",
        "readings": {
            "gb": {"adjective": "klˈQs", "noun": "klˈQz", "verb": "klˈQz"},
            "us": {"adjective": "klˈOs", "noun": "klˈOz", "verb": "klˈOz"},
        },
        "pos_map": {
            "NN": "noun", "NNS": "noun",
            "VB": "verb", "VBD": "verb", "VBG": "verb", "VBN": "verb", "VBP": "verb", "VBZ": "verb",
            "JJ": "adjective", "JJR": "adjective", "JJS": "adjective", "RB": "adjective",
        },
        "cues": {
            "verb": ["door", "window", "shut", "closing", "eyes", "book", "lid", "gate"],
            "noun": ["day", "evening", "session", "at the close", "end of"],
        },
        "default_reading": "adjective",
        "missing_from_misaki": False,
        "note": "DEFAULT is the adjective/adverb reading (near, shut), the complement of neither noun nor verb alone -- both are already explicit tags, so the automatic namer would only produce the generic 'default'.",
    },
    # "live": non-DEFAULT is a single explicit VERB tag ("to live", reside),
    # so the automatic complement rule names DEFAULT "noun" -- but DEFAULT
    # ('lˈIv' gb/us) is the ADJECTIVE ("a live wire", "live music", "went
    # live"). There is no bare-noun sense of "live" at all (that is the
    # separate word "lives", already correctly named noun/verb since its
    # own non-DEFAULT tag set has no ADJ). Hand-named, with the pos_map's
    # NN/NNS removed (they do not apply) and JJ/JJR/JJS/RB added instead.
    "live": {
        "class": "A",
        "readings": {
            "gb": {"adjective": "lˈIv", "verb": "lˈɪv"},
            "us": {"adjective": "lˈIv", "verb": "lˈɪv"},
        },
        "pos_map": {
            "JJ": "adjective", "JJR": "adjective", "JJS": "adjective", "RB": "adjective",
            "VB": "verb", "VBD": "verb", "VBG": "verb", "VBN": "verb", "VBP": "verb", "VBZ": "verb",
        },
        "cues": {
            "adjective": ["wire", "broadcast", "concert", "coal", "ammunition", "grenade", "went live", "coverage"],
            "verb": ["reside", "resides", "resided", "dwell", "dwelt", "nearby", "alone"],
        },
        "default_reading": "adjective",
        "missing_from_misaki": False,
        "note": "DEFAULT is the adjective (live wire), not a noun -- 'live' has no bare-noun sense. The automatic complement rule would wrongly guess 'noun' (the majority-pattern complement of an explicit VERB tag) and would wrongly attach NN/NNS to the pos_map; both are corrected here by hand.",
    },
    # "invalid": non-DEFAULT is a single explicit NOUN tag (a sick/disabled
    # person, 'ˈɪnvəlɪd'), so the automatic complement rule falls through
    # its priority chain to "verb" -- but DEFAULT ('ɪnvˈalɪd') is the
    # ADJECTIVE (not valid: a claim, an argument, a contract). "invalid" has
    # no real verb sense. Hand-named, with JJ* replacing VB* in the pos_map.
    "invalid": {
        "class": "A",
        "readings": {
            "gb": {"adjective": "ɪnvˈalɪd", "noun": "ˈɪnvəlɪd"},
            "us": {"adjective": "ɪnvˈælɪd", "noun": "ˈɪnvəlɪd"},
        },
        "pos_map": {
            "JJ": "adjective", "JJR": "adjective", "JJS": "adjective",
            "NN": "noun", "NNS": "noun",
        },
        "cues": {
            "noun": ["bedridden", "wheelchair", "nurse", "sickroom", "invalid's", "nursed"],
            "adjective": ["claim", "argument", "contract", "ticket", "passport", "license", "null and void", "reasoning"],
        },
        "default_reading": "adjective",
        "missing_from_misaki": False,
        "note": "DEFAULT is the adjective (not valid), not a verb -- 'invalid' has no verb sense. The automatic complement rule guesses 'noun' first, finds it taken by the explicit NOUN tag, and falls through to the next candidate 'verb'; corrected here by hand. Kept class A per the plan §1.4 list, which names 'invalid' as a class-A word to check, not a stress-reduction artifact.",
    },
    # "used" -- GAP 5: misaki special-cases this word BEFORE its dict-based
    # tag resolution even runs (`Lexicon.lookup()`, en.py, the
    # `elif word in ('used', 'Used', 'USED')` branch): the idiom reading
    # ('juːst', "used to") fires only when the tag is VBD/JJ AND the NEXT
    # TOKEN is literally "to"; every other case, including plain VBD/VBN/VBP
    # ("a room that was used by the occupants"), gets the ordinary reading
    # ('juːzd'). Copying misaki's dict-level VBD -> 'juːst' entry into a flat
    # pos_map (as the automatic namer did) reproduces the idiom sound for
    # EVERY past-tense "used", which is wrong far more often than it is
    # right -- measured: ch01/0050 ("used by the occupants", VBN, no
    # following "to") was force-corrected to the wrong sound this way.
    # FIX (option a from the gap report): every verb tag points at the
    # ordinary reading; the idiom is carried by a cue on a following "to".
    # A pos_map cannot express a next-token condition, so this is the
    # closest a tag-keyed schema can get -- a later reader must not
    # "simplify" this back to VBD/JJ -> idiom, matching misaki's own table;
    # that was the bug.
    "used": {
        "class": "B",
        "readings": {
            "gb": {"regular": "jˈuːzd", "idiom": "jˈuːst"},
            "us": {"regular": "jˈuzd", "idiom": "jˈust"},
        },
        "pos_map": {
            "VB": "regular", "VBD": "regular", "VBG": "regular",
            "VBN": "regular", "VBP": "regular", "VBZ": "regular",
        },
        "cues": {"idiom": ["to"]},
        "default_reading": "regular",
        "missing_from_misaki": False,
        "note": "misaki resolves 'used' by a NEXT-TOKEN check (is the following word literally 'to'?), not by the tag alone -- see the comment above this entry. Every verb tag here points at the ordinary reading; do not map VBD/JJ to 'idiom' the way misaki's own gold table does, or ordinary past-tense 'used' (the common case) is wrongly voiced as the idiom every time.",
    },

    # ------------------------------------------------ missing_from_misaki --
    # Every phoneme string below was derived from a real, currently-in-
    # lexicon rhyme word (named in the note) and round-tripped through
    # misaki.en.G2P with inline override markup: token.phonemes matched the
    # string exactly and no [ ] ( ) / leaked into the output. Refer to the
    # module docstring and the worker report for the check itself.
    #
    # WARNING -- DO NOT "COMPLETE" THESE pos_map TABLES. Several entries
    # below deliberately map only the VERB tags and leave NN and NNS out.
    # That looks like an omission. It is not. It is what makes the audit
    # able to find the fault at all.
    #
    # A trap word holds ONE reading in misaki, so misaki's baseline is
    # always that reading. Adding `"NN": "line"` to `row` would let tier 1
    # decide `line` from the tag, misaki's baseline is also `line`, the two
    # would AGREE, and the occurrence would never be forced. On Book A
    # that one edit would have silently lost all five `row` faults
    # ("kick up a row") and both `lead` faults ("fill ye full o' lead") --
    # seven of the sixteen the audit found.
    #
    # Leaving the noun tags unmapped is what pushes a trap word past tier 1
    # to the cues and to tier 3, which are the only two things that can
    # tell the two senses apart. Refer to HOMOGRAPHS-PROGRESS.md.
    "lead": {
        "class": "A",
        "readings": {"gb": {"guide": "lˈiːd", "metal": "lˈɛd"}, "us": {"guide": "lˈid", "metal": "lˈɛd"}},
        "pos_map": {"VB": "guide", "VBP": "guide", "VBZ": "guide", "VBG": "guide"},
        "cues": {"metal": ["pipe", "pipes", "pencil", "bullet", "bullets", "poisoning", "solder", "paint", "ore", "weight", "sinker"]},
        "default_reading": "guide",
        "missing_from_misaki": True,
        "note": "misaki has only the guide/leash/first-place sense (liːd, both dialects); the metal sense /lɛd/ is absent. Derived from led/bed/red/said, which are all lˈɛd in both dialects. Both senses are NN, so a cue carries the metal reading; verb tags are unambiguous.",
    },
    "row": {
        "class": "A",
        "readings": {"gb": {"line": "ɹˈQ", "quarrel": "ɹˈW"}, "us": {"line": "ɹˈO", "quarrel": "ɹˈW"}},
        "pos_map": {"VB": "line", "VBP": "line", "VBZ": "line", "VBG": "line", "VBD": "line", "VBN": "line"},
        "cues": {"quarrel": ["shouting", "argument", "loud", "screaming", "furious", "big row", "terrible row", "had a row"]},
        "default_reading": "line",
        "missing_from_misaki": True,
        "note": "misaki has only the line-of-seats/rowing sense (both dialects); the quarrel sense /raʊ/ is absent. Derived from cow/how/now, which use the same W symbol in both dialects. Both senses are NN, so a cue carries the quarrel reading; the boat verb is unambiguous.",
    },
    "does": {
        "class": "A",
        "readings": {"gb": {"verb": "dˈʌz", "deer": "dˈQz"}, "us": {"verb": "dˈʌz", "deer": "dˈOz"}},
        "pos_map": {"VBZ": "verb", "NNS": "deer"},
        "default_reading": "verb",
        "missing_from_misaki": True,
        "note": "misaki has only 'does' the verb (he does); the plural of doe (female deer) is absent. Derived from goes/toes, which add z to the same Q/O vowel used by doe itself. VBZ vs NNS separates the two cleanly -- no cues needed.",
    },
    "bases": {
        "class": "A",
        "readings": {"gb": {"basis": "bˈAsiːz", "base": "bˈAsɪz"}, "us": {"basis": "bˈAsiz", "base": "bˈAsᵻz"}},
        "pos_map": {},
        "cues": {
            "base": ["military", "army", "baseball", "loaded", "touch", "home plate", "naval", "air force", "camp"],
            "basis": ["scientific", "legal", "moral", "theoretical", "broad", "on which", "form the"],
        },
        "default_reading": "basis",
        "missing_from_misaki": True,
        "note": "misaki has only the plural of basis (long i); the plural of base (short i) is absent. Derived the short-i plural suffix from axes' own VERB reading (axe plural), substituting the base/bAs stem; both tags are NNS, so cues carry the split.",
    },
    "sewer": {
        "class": "A",
        "readings": {"gb": {"drain": "sˈuːə", "sewing": "sˈQə"}, "us": {"drain": "sˈuəɹ", "sewing": "sˈOəɹ"}},
        "pos_map": {},
        "cues": {
            "sewing": ["seamstress", "tailor", "quilter", "stitching", "needle", "fine sewer", "good sewer"],
            "drain": ["pipe", "gutter", "waste", "manhole", "stench", "rat", "underground", "flood", "sewage"],
        },
        "default_reading": "drain",
        "missing_from_misaki": True,
        "note": "misaki has only the drain-pipe sense; 'one who sews' (a homophone of 'sower') is absent. Derived directly from sower/mower/grower/goer, all built on the same soʊ/soʊ vowel as 'sew'. Both tags are NN, so cues carry the split.",
    },
    "aged": {
        "class": "A",
        "readings": {"gb": {"participle": "ˈAʤd", "adjective": "ˈAʤɪd"}, "us": {"participle": "ˈAʤd", "adjective": "ˈAʤɪd"}},
        "pos_map": {"VBD": "participle", "VBN": "participle"},
        "cues": {"adjective": ["elderly", "infirm", "frail"]},
        "default_reading": "participle",
        "missing_from_misaki": True,
        "note": "misaki has only the one-syllable participle (aged cheese, aged 42); the two-syllable elderly adjective /ˈeɪdʒɪd/ is absent. Derived the -ɪd adjectival suffix from crooked/dogged/ragged/jagged, which show the identical DEFAULT=-ɪd / VERB=-d split already in the gold lexicon. JJ alone cannot separate the two uses, so a narrow, high-precision cue set carries the adjective reading; kept short on purpose (§ cue guidance: high precision over high recall).",
    },
    "blessed": {
        "class": "A",
        "readings": {"gb": {"participle": "blˈɛst", "adjective": "blˈɛsɪd"}, "us": {"participle": "blˈɛst", "adjective": "blˈɛsɪd"}},
        "pos_map": {"VBD": "participle", "VBN": "participle"},
        "cues": {"adjective": ["virgin", "sacred", "holy", "blessed event", "blessed memory"]},
        "default_reading": "participle",
        "missing_from_misaki": True,
        "note": "misaki has only the one-syllable participle (he blessed them); the two-syllable adjective /ˈblɛsɪd/ (the blessed virgin) is absent. Same -ɪd suffix derivation as 'aged'. Cue set kept narrow and high precision.",
    },
    "tarry": {
        "class": "A",
        "readings": {"gb": {"covered": "tˈɑːɹi", "linger": "tˈaɹi"}, "us": {"covered": "tˈɑɹi", "linger": "tˈɛɹi"}},
        "pos_map": {"JJ": "covered", "VB": "linger", "VBP": "linger", "VBG": "linger", "VBD": "linger", "VBN": "linger", "VBZ": "linger"},
        "default_reading": "covered",
        "missing_from_misaki": True,
        "note": "misaki has only the tar-covered adjective (matches 'starry''s vowel exactly); the archaic verb 'to tarry' (linger) uses the short-a vowel of marry/carry instead, us dialect further mapping that to the marry-merry-mary merger like those words do. JJ vs V* separates the two cleanly.",
    },
    "mow": {
        "class": "A",
        "readings": {"gb": {"cut": "mˈQ", "haystack": "mˈW"}, "us": {"cut": "mˈO", "haystack": "mˈW"}},
        "pos_map": {"VB": "cut", "VBP": "cut", "VBZ": "cut", "VBG": "cut", "VBD": "cut", "VBN": "cut"},
        "cues": {"haystack": ["hay", "barn", "loft", "stack of hay"]},
        "default_reading": "cut",
        "missing_from_misaki": True,
        "note": "misaki has only 'to mow' (grass); the archaic noun 'a mow' (a stack of hay in a barn) is absent, same W-family pattern as bow/sow/dove. Rare in modern prose -- included for completeness of the sweep, low expected impact.",
    },
    "number": {
        "class": "B",
        "readings": {"gb": {"quantity": "nˈʌmbə", "comparative": "nˈʌmə"}, "us": {"quantity": "nˈʌmbəɹ", "comparative": "nˈʌməɹ"}},
        "pos_map": {"NN": "quantity", "NNS": "quantity", "JJR": "comparative"},
        "default_reading": "quantity",
        "missing_from_misaki": True,
        "note": "misaki has only the quantity noun; the comparative of 'numb' (his hands grew number, silent b) is absent. Derived by dropping the b exactly as 'numb' itself already drops it in the gold lexicon. NN/NNS vs JJR separates the two cleanly.",
    },
    "resume": {
        "class": "A",
        "readings": {"gb": {"continue": "ɹɪzjˈuːm", "cv": "ɹˈɛzjʊmA"}, "us": {"continue": "ɹəzˈum", "cv": "ɹˌɛzəmˈA"}},
        "pos_map": {"VB": "continue", "VBP": "continue", "VBZ": "continue", "VBG": "continue", "VBD": "continue"},
        "cues": {"cv": ["job", "position", "hiring", "candidate", "interview", "attached", "enclosed", "cover letter"]},
        "default_reading": "continue",
        "missing_from_misaki": True,
        "note": "misaki has only 'to resume' (continue); the noun (a résumé/CV, often spelled without accents) is absent. Derived from the ballet/cabaret/matinee/buffet family's -A ending and stress pattern (gb stresses the first syllable, us the last, matching that whole family), reusing the zj cluster from resume's own verb reading. Lower confidence than the other entries here -- flagged for a human second look if it matters for a specific book.",
    },
}


def _apply_curation(entries: dict[str, Any], curation: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Return entries with every CURATION override applied (shallow merge).

    A key present in the override replaces the generated key outright,
    matching the schema's `readings`/`pos_map`/`default_reading` being
    replaced as a whole rather than deep-merged (avoids a partial merge
    silently mixing an old reading name into a new pos_map). Every touched
    word gets curated: true, whether or not it already existed.
    """
    merged = dict(entries)
    for word, override in curation.items():
        base = dict(merged.get(word, {}))
        base.update(override)
        base["curated"] = True
        base.setdefault("missing_from_misaki", False)
        merged[word] = base
    return merged


def _mirror_proper_noun_tags(entries: dict[str, Any]) -> dict[str, Any]:
    """Return entries with NNP mirroring NN, and NNPS mirroring NNS.

    Runs AFTER curation, over every entry (auto-generated and curated
    alike), so a curated word's hand-written pos_map (e.g. "close", which
    lists NN/NNS literally) gets the same proper-noun coverage as an
    auto-generated one -- this is a single pass over the whole 701-word
    inventory, not a per-CURATION-entry change. Gap 1 (worker report,
    homographs audit round 1): `en_core_web_trf` tags a capitalised common
    noun NNP whenever it is used as a name -- Book A's "the House" is
    the measured case, and every sentence-initial heteronym hits the same
    wall -- and a pos_map with no NNP/NNPS entry sends every such occurrence
    straight past tier 1 into an unnecessary escalation.

    Uses plain assignment, not setdefault: NN's reading name is authoritative
    for NNP (a proper-noun-tagged occurrence of a common heteronym still
    means the common-noun sense), so a stale/mismatched NNP already present
    would be a bug, not an intentional override, and should be corrected.
    """
    for entry in entries.values():
        pos_map = entry.get("pos_map")
        if not pos_map:
            continue
        if "NN" in pos_map:
            pos_map["NNP"] = pos_map["NN"]
        if "NNS" in pos_map:
            pos_map["NNPS"] = pos_map["NNS"]
    return entries


def _self_check(entries: dict[str, Any]) -> list[str]:
    """Return a list of dangling-reading-name violations (empty when clean).

    Every reading name used in pos_map, cues, or default_reading MUST exist
    as a key of readings.gb or readings.us -- a dangling name is a defect
    that would crash a later consumer (the audit tool) trying to look up a
    phoneme string that was never written. Refer to the task's schema note.
    """
    violations: list[str] = []
    for word, entry in entries.items():
        known_names = set(entry.get("readings", {}).get("gb", {})) | set(
            entry.get("readings", {}).get("us", {})
        )
        if not known_names:
            violations.append(f"{word}: readings.gb and readings.us are both empty")
            continue
        default_reading = entry.get("default_reading")
        if default_reading not in known_names:
            violations.append(f"{word}: default_reading {default_reading!r} not in readings")
        for tag, name in entry.get("pos_map", {}).items():
            if name not in known_names:
                violations.append(f"{word}: pos_map[{tag!r}] -> {name!r} not in readings")
        for name, keywords in entry.get("cues", {}).items():
            if name not in known_names:
                violations.append(f"{word}: cues[{name!r}] not in readings")
            if not keywords:
                violations.append(f"{word}: cues[{name!r}] is empty")
    return violations


def _misaki_version() -> str:
    try:
        return importlib_metadata.version("misaki")
    except importlib_metadata.PackageNotFoundError:
        return "unknown"


def build_inventory() -> dict[str, Any]:
    """Return the full heteronyms.json document (schema, counts, entries)."""
    gb_gold, us_gold = _load_gold(_misaki_data_dir())
    entries = _generate_from_lexicons(gb_gold, us_gold)
    entries = _apply_curation(entries, CURATION)
    entries = _mirror_proper_noun_tags(entries)

    violations = _self_check(entries)
    if violations:
        print(f"heteronyms self-check: {len(violations)} dangling reading name(s):")
        for line in violations:
            print(f"  - {line}")
    else:
        print("heteronyms self-check: 0 dangling reading names")

    counts = {"A": 0, "B": 0, "C": 0, "missing_from_misaki": 0}
    for entry in entries.values():
        counts[entry["class"]] = counts.get(entry["class"], 0) + 1
        if entry.get("missing_from_misaki"):
            counts["missing_from_misaki"] += 1

    return {
        "schema": SCHEMA,
        "generated_by": "abpipe/data/build_heteronyms.py",
        "misaki_version": _misaki_version(),
        "counts": counts,
        "entries": entries,
    }


def main() -> None:
    document = build_inventory()
    text = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    OUT_PATH.write_text(text, encoding="utf-8")
    counts = document["counts"]
    total = len(document["entries"])
    print(
        f"wrote {OUT_PATH} : {total} entries "
        f"(A={counts['A']} B={counts['B']} C={counts['C']} "
        f"missing_from_misaki={counts['missing_from_misaki']})"
    )


if __name__ == "__main__":
    main()
