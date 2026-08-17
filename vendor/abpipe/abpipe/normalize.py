"""Stage 2 -- normalize. CONTRACT.md section 6. Owner: Worker A.

Reads ``01-extract/<id>.txt`` and writes ``02-normalize/<id>.txt``. The output keeps
the paragraph shape of the input, so a diff of the two files is readable.

The rule table (``RULES``) is data, not code, so a test can read it. Six rules --
``drop_citations``, ``drop_sic``, ``numbers``, ``caliber``, ``caps_run``, ``symbol_paragraph``
-- are book-configurable (CONTRACT.md sections 6.1, 6.1.1, 6.2, 6.3, and ``drop_citations``/
``drop_sic`` below) and are marked ``"dynamic": True`` in the table; their behaviour reads the
book config's ``normalize`` object at apply time instead of being fixed at import time. Every
other rule always runs.

**``drop_citations`` (default ``False``) and ``drop_sic`` (default ``True``) both remove text
the author (or an editor) put on the page, not just spelling/case/punctuation.** Every other
rule changes those so the engine says the author's words correctly; these two delete a span
outright.

``drop_citations`` deletes a parenthetical scholarly citation -- a parenthesis whose content
holds a 4-digit year in 1400-2099, with no nested parenthesis -- because a spoken "(Delgado
1950-1982, 12: 21-22)" is several seconds of numbers in the middle of a sentence. It is
narrow, off by default, and the maintainer's decision (maintainer-vetoable), recorded here and in
``source/book-c.config.json``, the one book that turns it on. See the
warning comment above ``_drop_citations()`` before changing it.

``drop_sic`` deletes an editorial ``[sic]`` mark -- a visual annotation of a misspelling
that, spoken aloud, is just noise ("the tyranny sic of their dictation") since the listener
cannot see what it is annotating. It defaults to ``True``: Book A and Book B hold
zero brackets of any kind, so the default is byte-identical for them; only a book with real
``[sic]`` marks (Book C, Book D) is affected. **It touches
`[sic]` only.** Every other bracketed span in both books is an editor-supplied word that
completes the sentence's grammar (`who kneads [dough]`, `salt[water]`) and must stay exactly
where it is -- see the warning comment above ``_drop_sic()``.
"""

from __future__ import annotations

import re

from num2words import num2words

from abpipe.meta import hash_file, hash_obj, is_fresh, write_meta, write_text

STAGE = "normalize"

# CONTRACT.md 4.1 / abpipe.extract.DEFAULT_NORMALIZE mirrors this table so a
# caller that passes no book config at all still gets sane behaviour. Worker
# A owns both; kept in sync by hand since normalize.py must not import
# extract.py (extract.py already imports nothing from here, and a cycle is
# not worth the coupling for five literals).
DEFAULT_NORMALIZE_CONFIG = {
    "expand_numbers": False,
    "recase_caps_run": True,
    "min_caps_run_words": 2,
    "drop_symbol_paragraphs": True,
    "caliber": True,
    "drop_citations": False,
    "drop_sic": True,
}

# Abbreviations actually present in Book A that keep their full stop.
# Stage 3 (chunk.py, Worker B) is the stage that must not treat these as a
# sentence boundary; this stage only guarantees it never strips their period
# (the "abbreviations" rule below is a documented no-op that records that
# guarantee as data, not as behaviour).
ABBREVIATIONS = ("Mr.", "Mrs.", "Fr.", "St.", "No.")


# --------------------------------------------------------------------------- roman numerals


_ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def roman_to_int(roman: str) -> int:
    """Return the integer value of a Roman numeral string (subtractive form)."""
    roman = roman.upper()
    total = 0
    prev = 0
    for ch in reversed(roman):
        val = _ROMAN_VALUES[ch]
        if val < prev:
            total -= val
        else:
            total += val
            prev = val
    return total


def int_to_words(n: int) -> str:
    """Return the English words of an integer."""
    return num2words(n)


# --------------------------------------------------------------------------- static rule bodies


_HEADING_RE = re.compile(r"^(Chapter\s+)([IVXLCDM]+)(\s*)$")


def _heading_roman(line: str) -> str:
    """`Chapter IV` -> `Chapter Four`, on a full line only."""
    m = _HEADING_RE.match(line)
    if not m:
        return line
    prefix, roman, trailing = m.groups()
    words = int_to_words(roman_to_int(roman)).capitalize()
    return f"{prefix}{words}{trailing}"


# A single capital letter standing alone, followed by one or two em dashes and
# then whitespace, is a Gutenberg redacted proper noun: `B-- Road` -> `B Road`
# (real em dash, U+2014). CONTRACT.md 6's `redacted_name` rule. The letter is
# kept; the dash(es) are deleted outright, not converted to a comma.
_REDACTED_NAME_RE = re.compile("\\b([A-Z])(?:\u2014{1,2})(?=[ \\t]|$)")

# A dash immediately before a closing quote is interrupted speech, not a
# parenthetical: `The doctor--"` -> `The doctor...'`. CONTRACT.md 6's
# `broken_speech` rule.
_BROKEN_SPEECH_RE = re.compile("\u2014{1,2}(?=[\"\u201d])")

_EM_DASH_RE = re.compile("\u2014{1,2}")


def _em_dash(text: str) -> str:
    return _EM_DASH_RE.sub(", ", text)


# The recorded decision names U+2012 FIGURE DASH as the real character. U+2013
# (en dash) and the ASCII hyphen are tolerated in the same position as a safety
# net. Book A only (CONTRACT.md 6 table).
_REDACTED_YEAR_RE = re.compile("192[\u2012\u2013-](?!\\d)")

_EN_DASH_RANGE_RE = re.compile("(?<=\\d)\u2013(?=\\d)")

_NUMBERED_HOUSE_RE = re.compile(r"\bNo\.\s*(\d+)")


def _numbered_house(match: re.Match) -> str:
    return f"Number {num2words(int(match.group(1)))}"


_QUOTE_TRANSLATE = str.maketrans(
    {
        "\u201c": '"',
        "\u201d": '"',
        "\u2018": "'",
        "\u2019": "'",
    }
)


def _curly_quotes(text: str) -> str:
    return text.translate(_QUOTE_TRANSLATE)


# Gutenberg's transcription of an ellipsis in Book A is three periods
# separated by NBSP or hair space (". . ."), not the U+2026 character. Both
# forms collapse to the same three-dot output. A run absorbs an adjacent real
# sentence-ending period too (e.g. "informer." immediately followed by the
# ellipsis convention) -- that reads correctly as a single trailing pause.
_ELLIPSIS_RE = re.compile("\u2026|\\.(?:[ \\t\u00a0\u200a]*\\.)+")


def _ellipsis(text: str) -> str:
    return _ELLIPSIS_RE.sub("...", text)


# CONTRACT.md 6's `spaces` rule: NBSP and the hair space (and a handful of
# other Unicode space characters that occur in the wild) collapse to a plain
# space. Newlines are untouched here -- they carry the paragraph shape, and
# `whitespace` below is the rule that touches run-length and trailing space.
_HSPACE_CHARS = "\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000"
_HSPACE_RE = re.compile(f"[{_HSPACE_CHARS}]")


def _spaces(text: str) -> str:
    return _HSPACE_RE.sub(" ", text)


# CONTRACT.md 6's `punctuation_cleanup` rule.
_DOUBLE_COMMA_RE = re.compile(r",\s*,")
_SPACE_BEFORE_COMMA_RE = re.compile(r"\s+,")
_CLEANUP_MULTI_SPACE_RE = re.compile(r" {2,}")


def _punctuation_cleanup(text: str) -> str:
    # CONTRACT.md section 6 also names a rule removing "a comma directly
    # before a closing quote or full stop". That rule is deliberately NOT
    # implemented -- verified against the real book text, it is unsafe.
    # `broken_speech` (run earlier) already converts every dash-directly-
    # before-a-closing-quote to an ellipsis, so no dash-born comma ever lands
    # there; a comma before a closing quote left after that is pre-existing,
    # correct punctuation (`"No," he muttered`). A version scoped to single
    # quotes is worse: U+2019 is also this book's elision mark (`'cos`,
    # `gev'`), so it deletes real commas (`gev, 'cos` -> `gev'cos`). This is
    # a correction to CONTRACT.md section 6, recorded here and accepted by
    # the overlord for the abpipe run.
    text = _DOUBLE_COMMA_RE.sub(",", text)
    text = _SPACE_BEFORE_COMMA_RE.sub(",", text)
    text = _CLEANUP_MULTI_SPACE_RE.sub(" ", text)
    return text


_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")
_TRAILING_LINE_SPACE_RE = re.compile(r"[ \t]+(?=\n)")
_TRAILING_SPACE_RE = re.compile(r"[ \t]+$")


def _whitespace(text: str) -> str:
    text = _MULTI_SPACE_RE.sub(" ", text)
    text = _TRAILING_LINE_SPACE_RE.sub("", text)
    text = _TRAILING_SPACE_RE.sub("", text)
    return text


def _abbreviations(text: str) -> str:
    """No-op: Mr./Mrs./Fr./St./No. keep their full stop untouched here."""
    return text


# --------------------------------------------------------------------------- CONTRACT.md 6.1 -- the number rule
#
# Shared with abpipe/qc.py (Worker C), which imports `expand_number` and
# `NUMBER_RE` from here so the source side and the whisper-transcript side of
# the QC comparison agree on every number form -- CONTRACT.md 6.1: "the rule
# must give the same words as qc._expand_number() for the same input, or
# every number in the book false-flags."
#
# Course correction, 2026-08-15: `normalize.expand_numbers` now defaults to
# false. misaki (Kokoro's text front end) already reads almost every number
# form correctly on its own -- measured against Book B's real digit set,
# including comma groups, decimals, and years -- so a hand-written rule only
# earns its risk where it demonstrably beats the front end. It does not, with
# one exception: CONTRACT.md 6.1.1's `caliber` rule, kept separate below.

_NUMBER_RE = re.compile(
    r"\d{1,3}(?:,\d{3})+(?:\.\d+)?%?"  # comma-grouped: 117,000 / 117,000.50%
    r"|\d+\.\d+%?"  # decimal, with or without a percent sign: 17.50 / 40.5%
    r"|\d+%"  # a bare integer percent: 40%
    r"|\d+(?:st|nd|rd|th)\b"  # an ordinal: 15th
    r"|\d+"  # any other integer: 1949, 44
)

NUMBER_RE = _NUMBER_RE  # public alias -- CONTRACT.md 6.1's shared regex, for qc.py.

_ORDINAL_RE = re.compile(r"^(\d+)(st|nd|rd|th)$")


def expand_number(match: re.Match) -> str:
    """Expand one number token (a `NUMBER_RE` match) to words. CONTRACT.md 6.1.

    | Form | Reading |
    |---|---|
    | A 4-digit number from 1000 to 2099, no comma grouping | a year |
    | A number with comma groups | the cardinal, spelled out |
    | An ordinal (`15th`) | `fifteenth` |
    | A decimal (`17.50`) | `seventeen point five zero` -- each digit read alone |
    | A percentage (`40%`) | `forty percent` |
    | Any other integer | the cardinal, spelled out |

    This is the exact formula `qc.py` must also use (CONTRACT.md 6.1) -- kept
    here as the one shared implementation; qc.py imports both this function
    and `NUMBER_RE` rather than keeping its own copy.
    """
    s = match.group(0)
    pct = s.endswith("%")
    core = s[:-1] if pct else s

    ordinal_match = _ORDINAL_RE.match(core)
    if ordinal_match:
        words = num2words(int(ordinal_match.group(1)), to="ordinal")
        return f"{words} percent" if pct else words

    if "." in core:
        int_part, _, frac_part = core.partition(".")
        int_part_clean = int_part.replace(",", "") or "0"
        words = num2words(int(int_part_clean))
        digit_words = " ".join(num2words(int(d)) for d in frac_part)
        words = f"{words} point {digit_words}" if digit_words else words
        return f"{words} percent" if pct else words

    has_comma = "," in core
    n = int(core.replace(",", ""))
    if not has_comma and 1000 <= n <= 2099:
        words = num2words(n, to="year")
    else:
        words = num2words(n)
    return f"{words} percent" if pct else words


def expand_numbers_in_text(text: str) -> str:
    """Expand every number in `text` to words, per `expand_number()` above.
    A whole-text convenience wrapper around `NUMBER_RE.sub(expand_number, ...)`
    -- this is the exact name/shape `qc.py`'s own test suite (test_qc.py)
    already expects (CONTRACT.md 6.1's shared implementation), so qc.py can
    import this directly wherever it needs to expand a raw string end to end.

    Overlord note (2026-08-15): defining this now reopens a transient window
    where test_qc.py's `test_qc_and_normalize_agree_on_number_expansion` fails
    rather than skips, because qc.py's pre-migration `_expand_number` has no
    comma support ("117,000" -> "one hundred and seventeen" + "zero"). That
    window closes once qc.py migrates its own `_expand_number`/`_NUMBER_RE` to
    import `expand_number`/`NUMBER_RE` from here -- the overlord's call to
    keep, not this worker's to route around a second time.
    """
    return _NUMBER_RE.sub(expand_number, text)


# --------------------------------------------------------------------------- CONTRACT.md 6.1.1 -- the caliber rule
#
# misaki's one measured miss (CONTRACT.md 6.1.1): ".22 caliber" -> "point two
# two caliber". A period that follows whitespace and holds exactly two digits
# becomes words: " .22" -> " twenty-two". The whitespace guard is what keeps
# this off "17.50" and off "$800.", where a digit sits before the period.

_CALIBER_RE = re.compile(r"(?<=\s)\.(\d{2})\b")


def _caliber_words(match: re.Match) -> str:
    return num2words(int(match.group(1)))


# --------------------------------------------------------------------------- the citation-drop rule
#
# *** WARNING -- READ THIS BEFORE TOUCHING drop_citations ***
#
# Every other rule in this table changes spelling, case, or punctuation so the engine SAYS
# the author's own words correctly. This is the first rule in this pipeline that DELETES
# words the author wrote. The maintainer's decision, book-scoped and off by default (CONTRACT.md 4.1):
# a spoken parenthetical scholarly citation like "(Delgado 1950-1982, 12: 21-22)" is several
# seconds of numbers read aloud in the middle of a sentence, and Book C is
# thick with them. Treat this rule as narrow, off by default, and easy to audit -- never
# widen its trigger condition without a fresh measurement against the real book, the way the
# 369/195 split below was measured.
#
# The rule: a parenthesis whose content holds a 4-digit year in 1400-2099, with NO nested
# parenthesis, is removed. Measured against the 18 body chapters of Book C:
# 564 parentheses total, 369 hold a year (100% genuine citations, zero false positives), 195
# hold no year (Latin binomials, authorial asides, list markers "(1)"/"(2)") and all 195 must
# survive untouched. `normalize.drop_citations` defaults to False; only a book config that
# explicitly sets it True is affected, and with it False the output is byte-identical to
# today's -- no other book's audio can change from this rule existing.
#
# Runs FIRST among the body rules (CONTRACT.md 6's rule table), before em_dash/redacted_year/
# en_dash_range/numbered_house/numbers/caliber ever see the parenthesis's contents: the year
# check must see the author's own punctuation, not a transformed version of it, and once a
# span is dropped no later rule gets a chance to mutate a fragment of it into something that
# no longer looks like the citation it was. punctuation_cleanup and whitespace still run last
# (CONTRACT.md 6), so any double-space or stray leading space this rule's own cleanup misses
# still gets mopped up by the existing final passes -- deliberately relied on, not duplicated.

_CITATION_YEAR_RE = re.compile(r"\b(?:1[4-9]\d{2}|20\d{2})\b")


def _find_flat_paren_spans(text: str) -> list[tuple[int, int]]:
    """Return `(start, end)` (end exclusive, just past the `)`) for every
    TOP-LEVEL "(...)" span in `text` that itself holds NO nested parenthesis
    -- CONTRACT.md's matching condition. A hand-rolled scan, not a regex:
    `[^()]*` alone would also match the INNERMOST piece of a nested group
    (e.g. the "(1999)" inside "(see Smith (1999) for detail)"), deleting a
    fragment out of the middle of a larger parenthetical and leaving the
    outer parens stranded around what remains. That never happens against
    the real book -- zero of its 369 citations nest -- but the rule stays
    correct in the case it exists to guard: a whole group that DOES nest is
    skipped in its entirety, inner spans included, not just the outer one.

    A "(" with no matching ")" anywhere, or whose matching ")" is only found
    after crossing a "\\n" (a paragraph boundary, or the join of two
    paragraphs by an unbalanced parenthetical -- measured once, in a quoted
    poem stanza), is also skipped: this rule never reaches across a
    paragraph break.
    """
    spans: list[tuple[int, int]] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] != "(":
            i += 1
            continue
        depth = 1
        j = i + 1
        nested = False
        crossed_newline = False
        while j < n and depth > 0:
            c = text[j]
            if c == "\n":
                crossed_newline = True
                break
            if c == "(":
                nested = True
                depth += 1
            elif c == ")":
                depth -= 1
            j += 1
        if depth == 0 and not crossed_newline:
            if not nested:
                spans.append((i, j))
            i = j  # either way, resume scanning after the whole group
        else:
            i += 1  # unmatched "(" -- skip just this character
    return spans


def _drop_citations(text: str) -> str:
    """Remove every flat parenthetical that holds a 4-digit year, 1400-2099.

    Consuming the one LEADING space/tab alongside the parenthesis (never a
    trailing one) is what gives every measured whitespace shape the right
    result with no extra cases:
      - mid-sentence ("Aztecs (cite) ate"): the trailing space survives as
        the single space between the two real words.
      - citation before the closing stop ("maize (cite)."): nothing follows
        the removed span before the period.
      - citation after the stop, end of paragraph ("maize. (cite)"): same
        leading-space removal, nothing left to clean up.
      - after a closing quote ("whites.” (cite)"): same shape again.
    A paragraph that is only a citation must vanish, not leave an empty
    line (CONTRACT.md 6) -- handled below by dropping any paragraph segment
    that goes empty (or whitespace-only) after the substitution, the same
    way `_symbol_paragraph` does, but unconditionally: this vanish is a
    property of THIS rule, not contingent on `drop_symbol_paragraphs`.
    """
    spans = _find_flat_paren_spans(text)
    if spans:
        pieces = []
        cursor = 0
        for start, end in spans:
            inner = text[start + 1 : end - 1]
            if not _CITATION_YEAR_RE.search(inner):
                continue  # not a citation -- leave it exactly where it is
            remove_from = start
            if remove_from > cursor and text[remove_from - 1] in " \t":
                remove_from -= 1
            pieces.append(text[cursor:remove_from])
            cursor = end
        pieces.append(text[cursor:])
        text = "".join(pieces)

    if not text:
        return text
    parts = text.split("\n\n")
    kept = [p for p in parts if p.strip()]
    text = "\n\n".join(kept)
    # Defensive mop-up for the one shape leading-space consumption cannot
    # reach on its own: a citation that opens a paragraph or opens the whole
    # text, with no leading space to eat, leaves a single leading space
    # behind from whatever followed the closing paren.
    text = re.sub(r"(?:\A|(?<=\n\n))[ \t]+", "", text)
    return text


# --------------------------------------------------------------------------- the [sic]-drop rule
#
# *** WARNING -- READ THIS BEFORE TOUCHING drop_sic ***
#
# `[sic]` is a visual proofreading mark: it tells a READER "yes, the word before this is
# spelled the way the original does, that's not a transcription error." Spoken aloud it is
# just noise in the middle of a sentence -- "the tyranny sic of their dictation" -- and the
# listener can't see the misspelling it's pointing at anyway, so the annotation carries no
# information once it's audio. Measured: 3 in Book C, 7 in
# Book D, 0 in Book A and Book B (which hold zero brackets of any kind), so the
# True default is byte-identical for every book that isn't one of the two with real [sic]
# marks -- CONTRACT.md's same safety property `drop_citations` has, just with the polarity of
# "safe default" flipped, because unlike a citation drop this one never has a false positive:
# every real occurrence in both books is a bare `[sic]`, case varying, sometimes with internal
# spaces (`[ sic ]`), and there is no other way for the literal word "sic" to appear inside
# square brackets in this book set.
#
# *** DO NOT WIDEN THIS TO A GENERAL BRACKET RULE ***
#
# Every other bracketed span across both books (183 total, measured) is an editor- or
# translator-supplied word that COMPLETES the sentence's grammar and must stay exactly where
# it is, spoken inline with no punctuation added around it:
#   "who kneads [dough]; who makes things sour"   -> "who kneads dough; ..."
#   "[She is] wiry, energetic"                    -> "She is wiry, energetic"
#   "salt[water] evaporated"                      -> "saltwater evaporated"  (mid-WORD bracket)
#   "banqueting in [the city of] Mexico"
#   "the first regularly ordained [A.M.E.] minister"
# Dropping a bracketed span in general would break the grammar in over a hundred places;
# adding punctuation around one in general would break `salt[water]` (there is no word
# boundary to punctuate across). This rule matches the literal word "sic" and nothing else --
# it never touches `[dough]`, `[She is]`, `[water]`, `[the city of]`, or `[A.M.E.]`.

_SIC_RE = re.compile(r"[ \t]?\[[ \t]*sic[ \t]*\]", re.IGNORECASE)


def _drop_sic(text: str) -> str:
    """Remove every `[sic]` mark (case-insensitive, internal spaces tolerated).

    Same one-leading-space-or-tab-consumed trick as `_drop_citations` above,
    and for the same reason -- it is what gives both measured shapes the
    right result with no extra cases:
      - mid-sentence ("tyrany [sic] of"): the trailing space survives as the
        single space between the two real words -- "tyrany of".
      - before punctuation ("gayety [sic],", 'corn [sic]".'): nothing
        follows the removed span before the comma/quote/period.
    Every real occurrence in both books is inline, mid-sentence -- neither
    book has a paragraph that is only a `[sic]` mark, so unlike
    `_drop_citations` this needs no paragraph-vanish step.
    """
    return _SIC_RE.sub("", text)


# --------------------------------------------------------------------------- CONTRACT.md 6.2 -- the ALL-CAPS run rule
#
# A token is a run of only uppercase ASCII letters (optionally with an
# internal apostrophe, e.g. "DON'T"), guarded on both sides so it can never
# match inside a mixed-case word ("aBC") or against a trailing digit
# ("ABC1"). A token that holds a period (an initialism like "U.S." or
# "I.R.B.") never matches this pattern at all -- CONTRACT.md 6.2: "the rule
# tests for a period inside the token" -- so an initialism is kept as-is with
# no extra logic needed; it simply never becomes part of a run.

_CAPS_TOKEN = r"(?<![A-Za-z0-9])[A-Z]+(?:['\u2019][A-Z]+)*(?![A-Za-z0-9])"
_CAPS_RUN_RE = re.compile(rf"{_CAPS_TOKEN}(?:[ \t]+{_CAPS_TOKEN})*")

def _title_case_token(token: str) -> str:
    # str.capitalize() already does the right thing through an internal
    # apostrophe -- "DON'T".capitalize() == "Don't" -- since it only
    # uppercases the first character and lowercases everything after it,
    # apostrophe included (unaffected, since it is not a letter).
    return token.capitalize()


def _make_caps_run_repl(min_words: int):
    def repl(m: re.Match) -> str:
        run = m.group(0)
        pieces = re.split(r"([ \t]+)", run)
        word_pieces = [p for p in pieces if p and not p.isspace()]
        if len(word_pieces) < min_words:
            return run
        return "".join(p if (not p or p.isspace()) else _title_case_token(p) for p in pieces)

    return repl


# --------------------------------------------------------------------------- CONTRACT.md 6.3 -- the symbol paragraph rule


def _has_letter_or_digit(s: str) -> bool:
    return any(ch.isalpha() or ch.isdigit() for ch in s)


def _symbol_paragraph(text: str) -> str:
    if not text:
        return text
    parts = text.split("\n\n")
    kept = [p for p in parts if _has_letter_or_digit(p)]
    return "\n\n".join(kept)


# --------------------------------------------------------------------------- the table


# Each static record holds a name, and either (pattern + replacement),
# (pattern + repl_func, a per-match callable), or (func, a whole-text
# callable) -- never more than one of those three shapes. A dynamic record
# (`"dynamic": True`) carries no pattern/func/replacement at all; its
# behaviour is applied by name in `normalize_text()` below, gated by
# `config_key` in the book config's `normalize` object.
RULES = (
    {"name": "heading_roman", "scope": "heading", "func": _heading_roman},
    # drop_citations runs FIRST among the body rules, deliberately -- see the
    # warning comment above _drop_citations(). It must see the author's own
    # punctuation before any other rule transforms a dash or a digit inside a
    # citation, and once it deletes a span, nothing later can react to it.
    {"name": "drop_citations", "scope": "body", "dynamic": True, "config_key": "drop_citations"},
    # drop_sic runs right after it, for the same reason: a text-removal rule,
    # grouped with drop_citations at the front of the table, ahead of every
    # rule that transforms punctuation -- there is no interaction between the
    # two (they match "(" and "[" respectively), so their relative order does
    # not matter to each other, only that both precede everything else.
    {"name": "drop_sic", "scope": "body", "dynamic": True, "config_key": "drop_sic"},
    {"name": "redacted_name", "scope": "body", "pattern": _REDACTED_NAME_RE, "replacement": r"\1"},
    {"name": "broken_speech", "scope": "body", "pattern": _BROKEN_SPEECH_RE, "replacement": "..."},
    {"name": "em_dash", "scope": "body", "func": _em_dash},
    {"name": "redacted_year", "scope": "body", "pattern": _REDACTED_YEAR_RE, "replacement": "nineteen twenty"},
    # en_dash_range runs before any digit-to-words rule: a "12-15" range needs
    # both digits still intact when its lookaround fires.
    {"name": "en_dash_range", "scope": "body", "pattern": _EN_DASH_RANGE_RE, "replacement": " to "},
    {"name": "numbered_house", "scope": "body", "pattern": _NUMBERED_HOUSE_RE, "repl_func": _numbered_house},
    {"name": "numbers", "scope": "body", "dynamic": True, "config_key": "expand_numbers"},
    {"name": "caliber", "scope": "body", "dynamic": True, "config_key": "caliber"},
    {"name": "caps_run", "scope": "body", "dynamic": True, "config_key": "recase_caps_run"},
    {"name": "symbol_paragraph", "scope": "body", "dynamic": True, "config_key": "drop_symbol_paragraphs"},
    {"name": "curly_quotes", "scope": "body", "func": _curly_quotes},
    {"name": "ellipsis", "scope": "body", "func": _ellipsis},
    {"name": "abbreviations", "scope": "body", "func": _abbreviations, "data": {"abbreviations": ABBREVIATIONS}},
    {"name": "spaces", "scope": "body", "func": _spaces},
    # CONTRACT.md 6: "punctuation_cleanup runs last but one" -- these two stay
    # the final pair, in this order, so that guarantee holds literally, not
    # just in spirit.
    {"name": "punctuation_cleanup", "scope": "body", "func": _punctuation_cleanup},
    {"name": "whitespace", "scope": "body", "func": _whitespace},
)

_RULES_BY_NAME = {r["name"]: r for r in RULES}


def _apply_static_rule(rule: dict, text: str) -> str:
    pattern = rule.get("pattern")
    if pattern is not None:
        if rule.get("replacement") is not None:
            return pattern.sub(rule["replacement"], text)
        if rule.get("repl_func") is not None:
            return pattern.sub(rule["repl_func"], text)
        return text
    func = rule.get("func")
    if func is not None:
        return func(text)
    return text


def _serialize_rule(rule: dict) -> dict:
    d = {"name": rule["name"], "scope": rule.get("scope", "body")}
    if rule.get("dynamic"):
        d["dynamic"] = True
        d["config_key"] = rule["config_key"]
        return d
    if rule.get("pattern") is not None:
        d["pattern"] = rule["pattern"].pattern
    if rule.get("replacement") is not None:
        d["replacement"] = rule["replacement"]
    if rule.get("repl_func") is not None:
        d["repl_func"] = rule["repl_func"].__name__
    if rule.get("func") is not None:
        d["func"] = rule["func"].__name__
    if rule.get("data") is not None:
        d["data"] = rule["data"]
    return d


def rules_config_hash(normalize_config: dict | None = None) -> str:
    """Return the config_hash of the whole rule table. CONTRACT.md section 6:
    "the whole rule table, and the normalize object of the book config."

    `normalize_config` defaults to `DEFAULT_NORMALIZE_CONFIG` when omitted --
    a caller with no book config (or one that has not resolved it, such as
    `abpipe status`'s current normalize check) still gets a stable value
    rather than a crash. Flagged to the overlord: `cli.py`'s
    `_status_normalize()` calls this with no argument at all, so a book whose
    config sets a non-default `normalize` object will report a false
    "stale"/"fresh" status against stage 2's *real* config_hash (which does
    receive the book config, in `run()` below) -- the same class of bug
    CONTRACT.md section 14 already documents and fixed for
    `extract.DEFAULT_CHAPTER_PATTERN`. `_status_normalize` should be updated
    to take `book_config` and pass `book_config["normalize"]` through, the
    way `_status_extract` already does for stage 1.
    """
    cfg = normalize_config if normalize_config is not None else DEFAULT_NORMALIZE_CONFIG
    return hash_obj({"rules": [_serialize_rule(r) for r in RULES], "normalize": cfg})


# --------------------------------------------------------------------------- public API


def normalize_text(text: str, normalize_config: dict | None = None) -> str:
    """Apply every body-scope rule, in order. Dialect and capitalisation survive.

    `normalize_config` is the book config's `normalize` object (CONTRACT.md
    4.1); it defaults to `DEFAULT_NORMALIZE_CONFIG` when omitted.
    """
    cfg = normalize_config if normalize_config is not None else DEFAULT_NORMALIZE_CONFIG
    for rule in RULES:
        if rule.get("scope") == "heading":
            continue
        if rule.get("dynamic"):
            if not cfg.get(rule["config_key"], DEFAULT_NORMALIZE_CONFIG.get(rule["config_key"], True)):
                continue
            name = rule["name"]
            if name == "numbers":
                text = _NUMBER_RE.sub(expand_number, text)
            elif name == "caliber":
                text = _CALIBER_RE.sub(_caliber_words, text)
            elif name == "caps_run":
                min_words = cfg.get("min_caps_run_words", DEFAULT_NORMALIZE_CONFIG["min_caps_run_words"])
                text = _CAPS_RUN_RE.sub(_make_caps_run_repl(min_words), text)
            elif name == "symbol_paragraph":
                text = _symbol_paragraph(text)
            elif name == "drop_citations":
                text = _drop_citations(text)
            elif name == "drop_sic":
                text = _drop_sic(text)
            continue
        text = _apply_static_rule(rule, text)
    return text


def normalize_chapter(text: str, normalize_config: dict | None = None) -> str:
    """Apply the heading rule to line 1 only, and the body rules to the rest.

    A chapter file's shape is ``heading\\n\\nbody`` (CONTRACT.md 5.4); a
    synthetic credits file has no heading line at all and is passed through
    untouched here, matching the shape stage 1 wrote it in.
    """
    if not text:
        return text
    heading_line, sep, rest = text.partition("\n\n")
    new_heading = _apply_static_rule(_RULES_BY_NAME["heading_roman"], heading_line)
    new_rest = normalize_text(rest, normalize_config) if sep else rest
    return new_heading + sep + new_rest


# --------------------------------------------------------------------------- run()


def run(ctx, chapters: list[str] | None = None, force: bool = False, book_config: dict | None = None, **kw) -> dict:
    """Run stage 2. Return a summary dict. CONTRACT.md section 6."""
    normalize_config = (book_config or {}).get("normalize") or DEFAULT_NORMALIZE_CONFIG

    in_dir = ctx.stage_dir("extract")
    out_dir = ctx.stage_dir("normalize")
    config_hash = rules_config_hash(normalize_config)

    ids = ctx.chapter_ids(chapters)

    done = 0
    skipped = 0
    failed: list[str] = []

    for cid in ids:
        in_path = in_dir / f"{cid}.txt"
        out_path = out_dir / f"{cid}.txt"

        if not in_path.exists():
            failed.append(cid)
            continue

        input_hash = hash_file(in_path)

        if not force and is_fresh(out_path, input_hash, config_hash):
            skipped += 1
            continue

        try:
            text = in_path.read_text(encoding="utf-8")
            new_text = normalize_chapter(text, normalize_config)
            write_text(out_path, new_text)
            write_meta(out_path, STAGE, input_hash, config_hash, extra={})
            done += 1
        except Exception:
            failed.append(cid)

    return {
        "stage": STAGE,
        "done": done,
        "skipped": skipped,
        "failed": failed,
        "chapters": len(ids),
    }
