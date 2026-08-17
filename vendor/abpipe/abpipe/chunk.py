"""Stage 3 — chunk. CONTRACT.md section 7.

Splits normalized chapter text into sentences, then packs the sentences into
speakable chunks that respect the paragraph and quotation rules.
"""

from __future__ import annotations

import re
from pathlib import Path

from abpipe.context import Context
from abpipe.meta import (
    hash_file,
    hash_obj,
    hash_text,
    is_fresh,
    read_json,
    write_json,
    write_meta,
    write_text,
)

CHAR_LIMIT = 350
HARD_LIMIT = 450

_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "st", "fr", "capt", "sgt", "prof", "rev", "gen",
    "col", "lt", "jr", "sr", "vs", "etc", "messrs", "mme", "mons", "esq", "hon",
    "maj", "cmdr", "adm", "gov", "rep", "sen",
}

# A run of one or more sentence-ending punctuation marks, an optional closing
# straight double quote, then whitespace or end of string.
_BOUNDARY_RE = re.compile(r'([.!?]+)("?)(\s+|$)')

# The trailing run of letters (and internal apostrophes) right before a boundary.
_TRAILING_WORD_RE = re.compile(r"([A-Za-z']+)$")


# --------------------------------------------------------------------------- sentences


def split_sentences(text: str) -> list[str]:
    """Split text into sentences.

    A regex finds candidate sentence boundaries. An abbreviation guard and a
    single-initial guard ("J. J.") keep those periods from splitting.
    """
    text = text.strip()
    if not text:
        return []

    sentences: list[str] = []
    start = 0
    for m in _BOUNDARY_RE.finditer(text):
        punct, quote, _space = m.group(1), m.group(2), m.group(3)
        boundary_end = m.start() + len(punct) + len(quote)

        if punct == ".":
            word_match = _TRAILING_WORD_RE.search(text[start:m.start()])
            prev_word = word_match.group(1) if word_match else ""
            prev_word_bare = prev_word.strip("'")
            if prev_word_bare.lower() in _ABBREVIATIONS:
                continue
            if len(prev_word_bare) == 1 and prev_word_bare.isupper():
                continue

        sentence = text[start:boundary_end].strip()
        if sentence:
            sentences.append(sentence)
        start = m.end()

    remainder = text[start:].strip()
    if remainder:
        sentences.append(remainder)
    return sentences


# --------------------------------------------------------------------------- quote units


def _group_quote_units(sentences: list[str]) -> list[list[str]]:
    """Group sentences so a chunk boundary never lands inside an open quotation.

    A straight double quote is the only quote delimiter. An apostrophe (a
    dialect contraction like "an'" or "Dh'ye") never counts.
    """
    units: list[list[str]] = []
    current: list[str] = []
    quote_count = 0
    for sentence in sentences:
        current.append(sentence)
        quote_count += sentence.count('"')
        if quote_count % 2 == 0:
            units.append(current)
            current = []
    if current:
        # An unterminated quote at the end of a paragraph. Emit it anyway;
        # a stray unmatched quote in the source must not eat the rest of the file.
        units.append(current)
    return units


# --------------------------------------------------------------------------- packing


def _pack_pieces(pieces: list[str], limit: int) -> list[str]:
    """Greedily join pieces with a single space, each group at most `limit` chars."""
    result: list[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current} {piece}".strip() if current else piece
        if current and len(candidate) > limit:
            result.append(current)
            current = piece
        else:
            current = candidate
    if current:
        result.append(current)
    return result


def _split_keep_trailing_comma(text: str) -> list[str]:
    """Split text at commas, keeping each comma attached to the piece before it."""
    parts = text.split(",")
    pieces: list[str] = []
    for i, part in enumerate(parts):
        if i < len(parts) - 1:
            piece = (part + ",").strip()
        else:
            piece = part.strip()
        if piece:
            pieces.append(piece)
    return pieces


def _split_by_words(text: str, hard_limit: int) -> list[str]:
    return _pack_pieces(text.split(" "), hard_limit)


def _split_long_text(text: str, hard_limit: int) -> list[str]:
    """Split one sentence that alone exceeds the hard limit: comma, then space."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= hard_limit:
        return [text]

    comma_pieces = _split_keep_trailing_comma(text)
    if len(comma_pieces) > 1:
        packed = _pack_pieces(comma_pieces, hard_limit)
        result: list[str] = []
        for piece in packed:
            if len(piece) <= hard_limit:
                result.append(piece)
            else:
                result.extend(_split_by_words(piece, hard_limit))
        return result

    return _split_by_words(text, hard_limit)


def _split_oversized(sentences: list[str], hard_limit: int) -> list[str]:
    """Split a unit whose joined text exceeds the hard limit.

    A quotation spanning several sentences splits at the internal sentence
    boundaries. A single sentence that alone is too long splits at a comma,
    then at a space.
    """
    if len(sentences) == 1:
        return _split_long_text(sentences[0], hard_limit)

    grouped: list[str] = []
    current: list[str] = []
    current_len = 0
    for sentence in sentences:
        candidate_len = current_len + (1 if current else 0) + len(sentence)
        if current and candidate_len > hard_limit:
            grouped.append(" ".join(current))
            current = [sentence]
            current_len = len(sentence)
        else:
            current.append(sentence)
            current_len = candidate_len
    if current:
        grouped.append(" ".join(current))

    result: list[str] = []
    for piece in grouped:
        if len(piece) <= hard_limit:
            result.append(piece)
        else:
            result.extend(_split_long_text(piece, hard_limit))
    return result


def _pack_paragraph(text: str, char_limit: int, hard_limit: int) -> list[str]:
    """Pack one paragraph's sentences into chunk texts.

    Greedy packing to `char_limit`. A quote unit never splits across chunks
    unless it alone exceeds `hard_limit`.
    """
    sentences = split_sentences(text)
    if not sentences:
        stripped = text.strip()
        return [stripped] if stripped else []

    units = _group_quote_units(sentences)

    chunk_sentence_groups: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for unit in units:
        unit_text = " ".join(unit)
        unit_len = len(unit_text)
        candidate_len = current_len + (1 if current else 0) + unit_len
        if current and candidate_len > char_limit:
            chunk_sentence_groups.append(current)
            current = list(unit)
            current_len = unit_len
        else:
            current.extend(unit)
            current_len = candidate_len
    if current:
        chunk_sentence_groups.append(current)

    result: list[str] = []
    for group in chunk_sentence_groups:
        joined = " ".join(group)
        if len(joined) <= hard_limit:
            result.append(joined)
        else:
            result.extend(_split_oversized(group, hard_limit))
    return result


# --------------------------------------------------------------------------- chunk records


def _build_records(chapter_id: str, text: str) -> list[dict]:
    """Return chunk records ({"text", "is_heading", "ends_paragraph"}) in file order."""
    lines = [ln for ln in text.split("\n") if ln.strip() != ""]

    if chapter_id == "ch00":
        heading = None
        body = lines
    elif lines:
        heading = lines[0]
        body = lines[1:]
    else:
        heading = None
        body = []

    records: list[dict] = []
    if heading is not None:
        records.append({"text": heading, "is_heading": True, "ends_paragraph": True})

    for paragraph in body:
        chunk_texts = _pack_paragraph(paragraph, CHAR_LIMIT, HARD_LIMIT)
        last_i = len(chunk_texts) - 1
        for i, chunk_text in enumerate(chunk_texts):
            records.append({
                "text": chunk_text,
                "is_heading": False,
                "ends_paragraph": i == last_i,
            })
    return records


# --------------------------------------------------------------------------- stage entry point


def _config_hash() -> str:
    return hash_obj({"char_limit": CHAR_LIMIT, "hard_limit": HARD_LIMIT})


def run(ctx: Context, chapters: list[str] | None = None, force: bool = False, **kw) -> dict:
    """Run stage 3. Return the summary dict."""
    ids = ctx.chapter_ids(chapters)
    config_hash = _config_hash()

    done = 0
    skipped = 0
    failed = 0
    chapters_summary: dict = {}

    for chapter_id in ids:
        norm_path = ctx.stage_dir("normalize") / f"{chapter_id}.txt"
        if not norm_path.exists():
            failed += 1
            chapters_summary[chapter_id] = {"error": "missing normalize output"}
            continue

        chunk_dir = ctx.stage_dir("chunk") / chapter_id
        index_path = chunk_dir / "index.json"
        input_hash = hash_file(norm_path)

        if not force and is_fresh(index_path, input_hash, config_hash):
            existing = read_json(index_path) or {}
            skipped += 1
            chapters_summary[chapter_id] = {
                "chunks": len(existing.get("chunks", [])),
                "skipped": True,
            }
            continue

        text = norm_path.read_text(encoding="utf-8")
        records = _build_records(chapter_id, text)

        chunk_dir.mkdir(parents=True, exist_ok=True)

        keep_names = {f"{i:04d}.txt" for i in range(1, len(records) + 1)}
        for existing_file in chunk_dir.glob("*.txt"):
            if existing_file.name not in keep_names:
                existing_file.unlink()

        chunks_meta = []
        for i, record in enumerate(records, start=1):
            chunk_id = f"{i:04d}"
            file_name = f"{chunk_id}.txt"
            write_text(chunk_dir / file_name, record["text"])
            chunks_meta.append({
                "id": chunk_id,
                "file": file_name,
                "chars": len(record["text"]),
                "words": len(record["text"].split()),
                "sha256": hash_text(record["text"]),
                "is_heading": record["is_heading"],
                "ends_paragraph": record["ends_paragraph"],
            })

        index = {"schema": 1, "chapter": chapter_id, "chunks": chunks_meta}
        write_json(index_path, index)
        write_meta(index_path, "chunk", input_hash, config_hash, extra={"chunks": len(chunks_meta)})

        done += 1
        chapters_summary[chapter_id] = {"chunks": len(chunks_meta)}

    return {"stage": "chunk", "done": done, "skipped": skipped, "failed": failed, "chapters": chapters_summary}
