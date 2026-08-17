# abpipe contract

**About this document.** This file is the contract of `abpipe`, the pipeline vendored into
this repository. Every book title, author name, and character name below is a generic
placeholder. Every measured number is real: each one comes from a real production run of
the pipeline, before the identifying details were replaced.

This document defines the file layout, the data formats, and the module interfaces of the
`abpipe` pipeline. Every stage obeys this document.

**Warning: this document is the single source of truth.** If a program and this document do
not agree, this document is correct. Correct the program. Do not change this document to
match a program.

**No worker edits this document.** Only the overlord edits this document.

---

## 1. What the pipeline does

The pipeline reads an EPUB file. The pipeline writes one m4b audiobook. The pipeline sends
the audiobook to Audiobookshelf on the server.

Eight stages do the work. Each stage reads the output of the stage before it.

| Number | Stage | Module | Reads | Writes |
|---|---|---|---|---|
| 1 | extract | `abpipe/extract.py` | the EPUB file | `01-extract/`, `book.json`, `cover.jpg` |
| 2 | normalize | `abpipe/normalize.py` | `01-extract/` | `02-normalize/` |
| 3 | chunk | `abpipe/chunk.py` | `02-normalize/` | `03-chunks/` |
| 4 | render | `abpipe/render.py` | `03-chunks/` | `04-audio/` |
| 5 | qc | `abpipe/qc.py` | `03-chunks/`, `04-audio/` | `05-qc/` |
| 6 | assemble | `abpipe/assemble.py` | `04-audio/`, `05-qc/` | `06-chapters/` |
| 7 | bind | `abpipe/bind.py` | `06-chapters/` | `07-book/` |
| 8 | deliver | `abpipe/deliver.py` | `07-book/` | the server |

---

## 2. Directory layout

```
<project root>/
  CONTRACT.md              this document
  README.md                how to run the pipeline
  PROGRESS.md              the run status
  NOTES.md                 the measurements of the run
  pyproject.toml           the package definition
  source/                  the input EPUB files, and one book config for each book
  samples/                 the voice sample WAV files
  abpipe/                  the pipeline package
  tests/                   the tests
  work/<book-slug>/        the artifacts of one book
  proof/specs/             the Playwright specs of the proof
```

The artifacts of one book live in one directory:

```
work/book-a/
  book.json                     the manifest. Refer to section 4.
  cover.jpg                     the cover image from the EPUB
  qc-config.json                the QC thresholds. Refer to section 8.
  01-extract/  ch00.txt … ch18.txt          + one .meta.json for each file
  02-normalize/ ch00.txt … ch18.txt         + one .meta.json for each file
  03-chunks/   ch01/0001.txt …              + ch01/index.json
  04-audio/    ch01/0001.wav …              + one .meta.json for each file
  05-qc/       ch01/0001.json …             + qc-report.json
  06-chapters/ ch01.wav  ch01.m4a  chapters.ffmeta   + one .meta.json for each file
  07-book/     Book A.m4b                   + Book A.m4b.meta.json
  logs/        <stage>-<utc-stamp>.log
```

**A stage never writes outside its own directory.** Stage 1 is the one exception: stage 1
writes `book.json` and `cover.jpg` at the top of the book directory.

---

## 3. The idempotence rule

**Every stage is idempotent.** A stage that runs a second time repeats no work whose
output is already correct. A killed run resumes at the first missing or stale artifact.

The rule has three parts.

1. A stage writes one **meta file** beside each output file. The name of the meta file is
   the name of the output file, plus `.meta.json`. The output `0001.wav` gets the meta file
   `0001.wav.meta.json`.
2. Before a stage makes an output, the stage compares the current inputs to the meta file.
   The stage **skips** the output when all of these are true:
   - the output file exists, and
   - the meta file exists and parses, and
   - `meta["input_hash"]` equals the hash of the current inputs, and
   - `meta["config_hash"]` equals the hash of the current configuration, and
   - **the size of the output file equals `meta["output_size"]`.**

   **Warning: a size check is mandatory.** Without it a truncated output stays trusted for
   ever. A kill during a write, or a full disk, leaves a short file beside a meta file that
   still matches. Every later run then skips that file and the fault never heals. A size
   check costs one `stat` call, so the stage always makes it. A hash check reads the whole
   file, so the stage makes that one only when the caller asks.
3. The `--force` option makes a stage ignore every meta file. The stage then makes each
   output again.

### 3.1 The meta file format

```json
{
  "schema": 1,
  "stage": "render",
  "output": "0001.wav",
  "input_hash": "3f7c…",
  "config_hash": "9ab1…",
  "output_sha256": "c4d2…",
  "output_size": 286764,
  "created_at": "20260815T210000Z",
  "extra": { "duration_s": 11.94 }
}
```

| Field | Rule |
|---|---|
| `schema` | The integer `1`. |
| `stage` | The stage name from the table in section 1. |
| `output` | The name of the output file. No directory part. |
| `input_hash` | A hex SHA-256 of the input of this output. Refer to 3.2. |
| `config_hash` | A hex SHA-256 of the configuration of this output. Refer to 3.2. |
| `output_sha256` | A hex SHA-256 of the output file. |
| `output_size` | The size of the output file, in bytes. The skip rule always checks it. |
| `created_at` | A UTC stamp in the form `YYYYMMDDThhmmssZ`. |
| `extra` | An object. A stage puts its own measurements here. The skip rule ignores it. |

### 3.2 The hash rules

`abpipe/meta.py` holds every hash function. **A stage never writes its own hash code.**

| Function | Action |
|---|---|
| `hash_bytes(b)` | Returns the hex SHA-256 of the bytes. |
| `hash_text(s)` | Returns the hex SHA-256 of the UTF-8 bytes of the string. |
| `hash_file(path)` | Returns the hex SHA-256 of the content of the file. |
| `hash_obj(obj)` | Returns the hex SHA-256 of the canonical JSON of the object. |
| `hash_many(parts)` | Returns the hex SHA-256 of the joined hashes of a list of strings. |

Canonical JSON uses `sort_keys=True`, `separators=(",", ":")`, `ensure_ascii=False`, and
UTF-8 bytes.

The `config_hash` of a stage is `hash_obj()` of the part of the configuration that changes
the output of that stage. Each stage documents its own configuration below.

### 3.3 Every write is atomic, and the meta comes last

**A stage never writes an output file in place.** The stage writes a temporary file in the
same directory, then moves that file over the target with `os.replace`. The move is atomic,
so a reader sees the old file or the new file, and never a half-written file.

The order is fixed:

1. Write the temporary file.
2. Move the temporary file into place.
3. Write the meta file.

**The meta file is the last write.** A meta file is a promise that the output beside it is
complete. A meta written first turns a kill into a permanent, silent fault.

A stage that rebuilds an output with `--force` calls `clear_meta()` **before** it starts.
The old meta then cannot outlive a kill and vouch for a half-written replacement.

A failed write removes its own temporary file. A run that leaves 2,000 temporary files on a
full disk has made the problem worse.

### 3.4 The meta helpers

```python
from abpipe.meta import read_meta, write_meta, is_fresh

is_fresh(out_path, input_hash, config_hash) -> bool
write_meta(out_path, stage, input_hash, config_hash, extra=None) -> dict
read_meta(out_path) -> dict | None
```

`write_meta` computes `output_sha256`, `output_size`, and `created_at` itself.

`is_fresh` always checks the size. `is_fresh(..., check_output_hash=True)` also reads the
whole file and checks the hash. Use the hash form where the cost is acceptable.

---

## 4. book.json

Stage 1 writes `book.json`. Every later stage reads it. **No later stage writes it.**

```json
{
  "schema": 1,
  "slug": "book-a",
  "title": "Book A",
  "author": "the author",
  "year": "1925",
  "genre": "Fiction",
  "language": "en",
  "source_epub": "source/book-a.epub",
  "source_sha256": "1b9e…",
  "cover": "cover.jpg",
  "engine": {
    "name": "kokoro_mlx",
    "model": "mlx-community/Kokoro-82M-bf16",
    "voice": "bm_george",
    "speed": 1.0,
    "lang_code": "b",
    "sample_rate": 24000
  },
  "credits": { "enabled": true, "text": "Book A, by the author." },
  "pronunciations": {},
  "chapters": [
    {
      "id": "ch00",
      "index": 0,
      "label": "Opening Credits",
      "src": null,
      "synthetic": true,
      "words": 5
    },
    {
      "id": "ch01",
      "index": 1,
      "label": "Chapter I",
      "src": "OEBPS/text/chapter-01.xhtml",
      "synthetic": false,
      "words": 3550
    }
  ]
}
```

| Field | Rule |
|---|---|
| `slug` | The name of the book directory. Lower case. Hyphens only. |
| `title` | The corrected title. The OPF title of this EPUB is wrong; refer to section 5. |
| `year` | A string. The year of first publication, not the EPUB date. |
| `source_sha256` | The hash of the EPUB file. |
| `cover` | The name of the cover file, relative to the book directory. `null` if the EPUB holds no cover. |
| `engine` | The engine configuration. Refer to section 7. |
| `credits` | The opening credits. `enabled` is a boolean. |
| `pronunciations` | Optional. How the engine says a word, for the QC comparison only. Refer to section 9.6. The default is an empty object. |
| `chapters[].id` | `ch` plus two digits. `ch00` is the credits. `ch01` upward are the real chapters. |
| `chapters[].index` | An integer. `0` for the credits. |
| `chapters[].label` | The chapter label of the m4b file. Keeps the Roman numeral. |
| `chapters[].src` | The path of the source file inside the EPUB. `null` for a synthetic chapter. |
| `chapters[].synthetic` | `true` when the pipeline invents the text, not the EPUB. |
| `chapters[].words` | The word count of the extracted text. |

**The chapter id is the key of every later stage.** A directory, a file name, and a report
key all use the same id.

---

## 4.1 source/&lt;slug&gt;.config.json — the book config

**Rule: per-book data lives in the book config. Per-book data never lives in a code
constant.** A title correction, a chapter label list, a voice, a pronunciation, and an
element policy all belong to one book. A code constant makes every future book inherit
them. `TITLE_OVERRIDES` and the `Gyko` entry of `DEFAULT_PRONUNCIATIONS` were such
constants. Both move into a book config.

The overlord writes one config for each book, at `source/<slug>.config.json`. The file is
an input, like the EPUB. No stage writes it.

```json
{
  "schema": 1,
  "slug": "book-b",
  "source_epub": "source/book-b.epub",
  "title": "Book B",
  "author": "the author",
  "year": "1988",
  "genre": "Fiction",
  "language": "en",
  "chapters": {
    "select": "labels",
    "pattern": null,
    "labels": ["Part One: Rowan", "Part Two: Kessler"],
    "span_to_next_toc_entry": true
  },
  "engine": {
    "name": "kokoro_mlx",
    "model": "mlx-community/Kokoro-82M-bf16",
    "voice": "am_michael",
    "speed": 1.0,
    "lang_code": "a",
    "sample_rate": 24000
  },
  "credits": { "enabled": true, "text": "Book B, by the author." },
  "pronunciations": {},
  "inherit_default_pronunciations": true,
  "elements": {
    "note_markers": "drop",
    "footnotes": "drop",
    "tables": "drop",
    "figures": "drop",
    "captions": "drop",
    "epigraphs": "render"
  },
  "normalize": {
    "drop_citations": false,
    "drop_sic": true,
    "caliber": true,
    "expand_numbers": true,
    "recase_caps_run": true,
    "min_caps_run_words": 2,
    "drop_symbol_paragraphs": true
  },
  "qc": { "equivalences": {} }
}
```

| Field | Rule |
|---|---|
| `slug` | The book directory name. Lower case. Hyphens only. |
| `source_epub` | The EPUB path, relative to the project root. |
| `title`, `author`, `year`, `genre`, `language` | Optional. Each one overrides the OPF metadata. `year` is the year of first publication, not the EPUB date. |
| `chapters.select` | `"pattern"` or `"labels"`. |
| `chapters.pattern` | A regular expression. The stage keeps a TOC entry whose label matches. Used when `select` is `"pattern"`. |
| `chapters.labels` | A list of exact TOC labels, in reading order. Used when `select` is `"labels"`. |
| `chapters.span_to_next_toc_entry` | A boolean. Refer to section 5.1. |
| `chapters.drop_paragraph_classes` | A list of exact class names. Default `[]`. Stage 1 drops a `<p>` whose class token matches one of them exactly. Refer to section 5.5. |
| `engine` | The engine configuration. Stage 1 copies it into `book.json`. |
| `credits` | The opening credits. |
| `pronunciations` | The per-book pronunciation map. Refer to section 9.6. |
| `inherit_default_pronunciations` | A boolean. Default `true`. Refer to section 5.2. |
| `elements` | The element policy table. Each value is `"drop"` or `"render"`. Refer to section 5.3. |
| `normalize` | The per-book normalize switches. Refer to section 6. |
| `qc.equivalences` | The seed of the per-book QC equivalence table. Refer to section 9.2. |

**Every field is optional except `slug` and `source_epub`.** A missing field takes the
documented default. A config that holds an unknown key is a hard error: a silent typo in a
policy name would change the audio of a whole book.

### 4.1.1 The loader

`abpipe/extract.py` holds the loader. Worker A owns it. Every other module calls it.

```python
extract.load_book_config(path: str | Path | None, slug: str | None = None) -> dict
```

The function reads the JSON file, checks the schema, fills every default, and returns a
plain dict with every documented key present. `path` of `None` means
`source/<slug>.config.json`. A missing file returns the full default config with the given
slug, so a book with no config still runs on the defaults.

The function raises `ValueError` with a message that names the file and the fault when the
schema is wrong, a key is unknown, or a policy value is not `"drop"` or `"render"`.

---

## 5. Stage 1 — extract

`abpipe/extract.py`. Owner: Worker A.

The stage reads the OPF spine and the table of contents. The stage keeps a spine item when
the TOC label of that item matches the chapter selection of the book config. The stage
drops every other spine item. For Book A the stage drops the Project Gutenberg
header, the title page, the contents, the Transcriber's Note, and the licence.

**The stage reads the EPUB3 `nav.xhtml` TOC first, and the EPUB2 NCX second.** A publisher
EPUB may hold one or the other. The stage finds `nav.xhtml` by the `nav` property in the
OPF manifest. The stage reads the `<nav epub:type="toc">` element of that file. The stage
falls back to the NCX when the OPF holds no `nav` item. The two readers return the same
list of records: a label and a source href, in reading order.

### 5.1 A chapter can span more than one spine item

**Warning: a TOC entry does not always point at all of its own text.** Book B's
EPUB is the proof. Its TOC entry `Part One: Rowan` points at `c01a`, a page that holds one
image and no text. The 14,793 words of Part One live in `c01`, and `c01` holds no TOC entry
at all. A reader that keeps only the TOC target loses the whole first part.

When `chapters.span_to_next_toc_entry` is `true`, one chapter is **every spine item from
its own TOC target, up to but not including the next TOC entry of any kind**. The stage
joins the paragraphs of those spine items in spine order. `chapters[].src` in `book.json`
then holds the list of source paths, not one path.

**Warning: the boundary is the next TOC entry, not the next _selected_ TOC entry.** The
last selected chapter has no next selected entry. With the wrong reading, Book B's
last selected part would run to the end of the spine and swallow the Dedication, Other
Books by This Author, and About the Author.

When the value is `false`, one chapter is one spine item. That is Book A's behaviour,
and it stays the default.

**The chapter heading may be an image.** Book B's part heading is
`<h1 class="chapter"><img/></h1>`, so the heading holds no text. The stage then uses the
TOC label as the heading line of the text file. That fallback already exists and stays.

### 5.2 The pronunciation defaults

`DEFAULT_PRONUNCIATIONS` holds only entries that are true for **every** book:

```python
DEFAULT_PRONUNCIATIONS = {"givin": "guivin"}
```

`givin` is general English dialect, not one book's data. Kokoro reads `givin'` as `ʤˈɪvɪn`,
a soft "j", which sounds like `jivin'`. `guivin` measures as `ɡˈɪvɪn`, the correct hard g.
Every new book gets the fix for free.

The stage merges `pronunciations` of the book config **over** these defaults. A book config
that sets `inherit_default_pronunciations` to `false` gets its own map only.

**Book A's config sets that flag to `false`.** Its map stays `{"Gyko": "Gikko"}`. The
`givin` fix would make all 2,042 of its chunks stale, and PROGRESS.md defers that re-render
until the maintainer schedules it. The flag records the decision in data, where a reader can see it.

### 5.3 Structural stripping

Stage 1 strips structure, because stage 1 is the only stage that reads HTML. Each policy
below reads its value from `elements` in the book config.

| Policy key | `"drop"` removes |
|---|---|
| `note_markers` | An element with `epub:type="noteref"`, and a `<sup>` element whose text is a number or a symbol |
| `footnotes` | `<aside epub:type="footnote">` and `<aside epub:type="endnote">` |
| `tables` | `<table>` and everything inside it |
| `figures` | `<figure>` and every `<img>` |
| `captions` | `<figcaption>`, and a `<p>` whose class holds `caption` |
| `epigraphs` | `<blockquote epub:type="epigraph">` and a `<div class="epigraph">` |

The default of every key is `"drop"`, except `epigraphs`, whose default is `"render"`.

**Warning: strip before the text is read, not after.** A rule that works on the extracted
string cannot tell a note marker from a real number.

### 5.5 The fragment fallback, and heading furniture

**Warning: a TOC fragment can point at an empty anchor.** Book C's EPUB
writes each chapter target as `<p class="label" id="page_66"><a id="ch4"/>FOUR</p>`. The
fragment target is a self-closing anchor **inside** the first paragraph, not a wrapper
around the chapter. Scoping to it returns an element with no paragraphs.

**That fault was silent.** Stage 1 reported `done=21 skipped=0 failed=0` and wrote 21 empty
files for a book of 97,857 words. A stage that produces nothing and calls it success is
worse than a crash.

Two rules follow.

**Rule 1: fall back to the body.** When scoping to the fragment yields zero paragraphs, the
stage uses `soup.body`. The fallback can only add content where there was none, so no book
that already works can change.

**Rule 2: a chapter that yields zero paragraphs is a failure.** A non-synthetic chapter
with no text stops the stage with a message that names the chapter and its source file.
Silence is never success.

**Heading furniture.** With the fallback in place, a publisher that writes its chapter
title as ordinary paragraphs makes the listener hear the title twice. Book C's author writes
`<p class="label">FOUR</p>` then `<p class="h2a">Chapter Four</p>`, and the heading line
already holds the TOC label `4. Chapter Four`. `chapters.drop_paragraph_classes` names the
classes to drop. Book C's author uses `["label", "h2a", "h2"]`; `h2` is the front-matter title, which
repeats `Preface` and `Introduction` the same way.

**The match is on an exact class token, not a substring.** That is deliberate, and it
differs from the `captions` policy, which matches a substring so that `fcaption` is caught.
A substring match here would drop `h2a` when the config named `h2`.

### 5.4 DRM refusal

**The stage refuses a file that holds DRM. The stage never circumvents DRM.**

The stage reads `META-INF/encryption.xml`. The stage fails with a clear message when that
file exists and encrypts anything that is not a font. Font obfuscation is normal in a
retail EPUB and is not DRM: an `EncryptedData` element whose `CipherReference` URI names a
file under `fonts/`, or whose algorithm is the IDPF or the Adobe font algorithm, is
allowed. Anything else stops the run.

The message names the file and says that the pipeline needs a DRM-free source.

The stage writes one plain text file for each chapter, at `01-extract/<id>.txt`.

The text file format:

- Line 1 is the chapter heading, exactly as the EPUB writes it. Example: `Chapter I`.
- Line 2 is empty.
- Each paragraph is one line. One empty line separates two paragraphs.
  **Warning: the EPUB wraps a paragraph over many source lines.** The stage collapses every
  run of whitespace inside a paragraph to one space.
- The file holds no HTML tag and no HTML entity.
- The file ends with one newline character.

The stage writes the cover image to `cover.jpg`. The stage writes `book.json`.

The stage writes `01-extract/ch00.txt` when `credits.enabled` is true. That file holds the
credits text as one paragraph and holds no heading line.

**The title of this book needs a correction.** The OPF metadata holds the title in the
wrong case. `book.json` holds the corrected title, `Book A`.

The `config_hash` inputs of this stage: **the whole book config**, as the loader returns it
after every default is filled. The chapter selection, the credits, the element policies,
and the metadata overrides all change the output, so all of them belong in the hash.

The `input_hash` of each output: `hash_file()` of the EPUB.

---

## 6. Stage 2 — normalize

`abpipe/normalize.py`. Owner: Worker A.

The stage reads `01-extract/<id>.txt` and writes `02-normalize/<id>.txt`. The output keeps
the paragraph shape of the input, so a diff of the two files is readable.

The rules are a table in the module. The table is data, not code, so a test can read it.
Each rule holds a name, a pattern, and a replacement.

The rules:

| Rule | Action |
|---|---|
| `drop_citations` | **Runs first.** A parenthesis that holds a 4-digit year is removed. Refer to section 6.4. |
| `drop_sic` | **Runs second.** `[sic]` is removed. Refer to section 6.5. |
| `heading_roman` | On line 1 only: `Chapter IV` becomes `Chapter Four`. |
| `redacted_name` | A capital letter, then `——`, and no closing quote after it. The rule removes the two dashes. `B—— Road` becomes `B Road`. The book holds 2 of these. |
| `broken_speech` | `——` or `—` immediately before a closing quote. The rule replaces the dashes with `...`. The book holds 44 double and several single. |
| `em_dash` | Every other `—` becomes `, `. |
| `redacted_year` | `192‒` becomes `nineteen twenty`. The character is U+2012, the figure dash. Book A only. |
| `numbered_house` | `No. 44` becomes `Number forty-four`. This rule runs before `numbers`. |
| `numbers` | Every other number becomes words. Refer to section 6.1. |
| `caliber` | ` .22` becomes ` twenty-two`. Refer to section 6.1.1. |
| `caps_run` | A run of ALL-CAPS words becomes normal case. Refer to section 6.2. |
| `symbol_paragraph` | A paragraph that holds only symbols is removed. Refer to section 6.3. |
| `curly_quotes` | `“` `”` become `"`. `‘` `’` become `'`. |
| `ellipsis` | `…` becomes `...`. |
| `spaces` | `\xa0` and the hair space U+200A become a normal space. |
| `punctuation_cleanup` | Removes `, ,`, `,,`, and a space before a comma. |
| `whitespace` | Collapses a run of spaces. Strips a trailing space. |

**Order matters.** `drop_citations` runs **first**, before `redacted_name`: it must see the
author's own punctuation, and once it deletes a span no later rule can react to a fragment
of it. `redacted_name` and `broken_speech` both run before `em_dash`, and all three run
before `curly_quotes`. `punctuation_cleanup` runs last but one.

### 6.4 The citation rule

**Warning: this is the only rule in this pipeline that removes words the author wrote.**
Every other rule changes spelling, case, or punctuation so that the engine says the
author's words correctly. This one deletes text. It is off by default, one book sets it,
and the maintainer can veto it at the cost of that book's re-render.

The maintainer approved it for Book C. The book cites in running text —
`(Delgado 1950–1982, 12: 21–22, retranslated)` — and chapter 4 alone holds 22. Read aloud,
each is several seconds of numbers in the middle of a sentence. The policy is the same as
dropping an endnote marker, for the same reason.

The rule removes a parenthesis whose content holds a 4-digit year from 1400 to 2099.
`normalize.drop_citations` of `false`, the default, turns it off.

**The rule is a span scanner, not a plain regular expression.** A `[^()]*` pattern deletes
only the innermost group of a nested parenthesis and strands the outer marks. The scanner
finds a top-level group and removes it whole. It never crosses a paragraph boundary, which
protects an unbalanced `(` in quoted verse.

**Measured against the real book, before the rule was written:** 564 parentheses, 369 hold
a year, and **all 369 are citations**. 27 of them hold a common function word, and every
one of those is still a citation — the words come from multi-author lists and from
source-within-source forms such as `(Ibarra in Cordoba 1986: 387)`. The 195 with
no year are Latin binomials, authorial asides, and list markers, and all of them stay.

**The safety property that matters: with `drop_citations` off, the normalized text of every
other book is byte-identical.** A test asserts it for all four books in `source/`.

### 6.5 The `[sic]` rule, and the brackets that must stay

`[sic]` is a **visual** editorial mark. Spoken, it is noise in the middle of a sentence,
and the listener cannot see the misspelling it annotates. Measured: 3 in Book C,
**9** in Book D.

```
the tyrany [sic] of their courage        ->  the tyrany of their courage
a crescendo of wild colour [sic],        ->  a crescendo of wild colour,
the towne [sic], grew in size            ->  the towne, grew in size
```

The rule removes `[sic]` and leaves no double space and no space before a following
punctuation mark. `normalize.drop_sic` defaults to `true`. That default is free:
Book A and Book B hold no bracket of any kind, so every earlier book stays
byte-identical.

**Warning: no other bracketed text is touched, and dropping it would break both books.**
The two books hold 184 bracket spans. Apart from the 12 `[sic]`, they are **words the
translator or editor supplied to complete the sentence**, and reading them inline is the
correct reading:

| Source | Spoken |
|---|---|
| `who kneads [dough]; who works hard` | who kneads dough; who works hard |
| `[She was] tired, but resolute` | She was tired, but resolute |
| `salt[water] filled the basin` | saltwater filled the basin |
| `traveling to [the city of] Antioch` | traveling to the city of Antioch |
| `the first regularly appointed [U.S.] officer` | the first regularly appointed U.S. officer |

Dropping the span gives "who kneads ; who works hard" and "salt evaporated".
**Adding a comma for prosody is also wrong**: `salt[water]` is mid-word, and
`the [old] cities` would become "the, old, cities".

One span is a true editorial aside, 155 characters long, and it also reads correctly
inline: `[there follows a list of twenty-five varieties of "fruit" …]`.

**Kokoro has no bracket symbol in its vocabulary, so the bracket characters are already
silent.** The inline reading is not a defect to be repaired. It is the behaviour these
books need.

**The accented letters stay.** `æ`, `é`, `ô`, `ö`, and `œ` are real words. The stage
changes none of them.

**Warning: never remove a comma that sits before a closing quote.** An earlier version of
this table held that rule. The rule corrupts real text. `"No," he muttered` loses a
correct comma. Worse, U+2019 is both the closing quote and the elision mark of this
dialect, so the rule turns `gev, 'cos` into `gev'cos`. The `broken_speech` rule already
handles every dash that sits before a quote, so no cleanup is needed there.

**Dialect policy: the stage changes no dialect.** `an'`, `ye`, `Dh'ye`, and `shure` stay as
the author wrote them. The stage never modernises a word.

**Capitalisation policy: the stage changes no capitalisation.** An ALL-CAPS word stays
ALL-CAPS.

### 6.1 The number rule

**Warning: the old `year_1916` and `bare_number` rules are struck.** They were fitted to one
book whole digit set: `1916`, `192‒`, `3`, `44`, and `61`. `bare_number` sends `1949` to
"one thousand nine hundred forty-nine", and it cannot read `117,000` or `17.50` at all.

**The default is now to leave a digit alone.** `normalize.expand_numbers` defaults to
`false`. Two measurements give that answer.

**Measurement 1: the misaki front end already reads a number correctly.** Measured against
every number form in Book B:

| Source | What misaki says | Correct |
|---|---|---|
| `1949 Chevrolet` | nineteen forty-nine | yes |
| `$800` | eight hundred dollars | yes |
| `$17.50 a week` | seventeen dollars and fifty cents | yes |
| `$117,000` | one hundred seventeen thousand dollars | yes |
| `the 1890s` | the eighteen nineties | yes |
| `ordinance 404A` | four hundred four A | yes |
| `104 degrees` | one hundred four degrees | yes |
| `March 1954` | March nineteen fifty-four | yes |
| `August 16, 1987` | August sixteen, nineteen eighty-seven | yes |
| `.22 caliber` | **point two two caliber** | **no** |

A hand-written rule must beat that front end to be worth its risk. It does not.

**Measurement 2: QC is symmetric with the digits left in.** `qc.qc_normalize()` runs the
**source side and the transcript side through the same function**, and step 1 of that
function expands every number with `_expand_number()`. A digit in the source text and the
same digit in the transcript therefore become the same words. Nothing needs to change.

`numbered_house` stays, because `No. 44` is correct English everywhere.

**When a book does set `expand_numbers` to `true`,** the rule must give the same words as
`qc._expand_number()` for the same input, or every number in the book false-flags. Worker A
and Worker C keep one shared implementation, in `normalize.py`, and `qc.py` imports it. A
test asserts that both sides agree over a table of inputs.

### 6.1.1 The caliber rule

**Warning: misaki reads `.22 caliber` as "point two two caliber".** That is the one number
form in Book B that the front end gets wrong. The book holds `.22` five times, and
`.45`, `.38`, and `.30` once each.

The `caliber` rule finds a period that follows whitespace and holds exactly two digits, and
writes the number as words: ` .22` becomes ` twenty-two`. The whitespace guard is what
keeps the rule off `17.50` and off `$800.`, where a digit sits before the period.

**The rule has a known and accepted QC cost.** The source side then reads "twenty-two",
while whisper writes ".22" and QC expands that to "point two two". Those chunks flag. There
are about eight of them in a book of near 2,900 chunks. **Accept each one in
`qc-accept.json` with a written reason.** That is the honest trade: correct audio, and a
recorded reason for a known, bounded comparison failure. Do not widen a threshold for it.

`normalize.caliber` of `false` turns the rule off. The default is `true`.

### 6.2 The ALL-CAPS run rule

**Warning: Kokoro spells an unknown ALL-CAPS word letter by letter.** Measured with misaki,
on the two character names that motivated this rule: the first name gives
`ˌɑɹˌOˌɛsˌIˈi`, each phoneme group spelling one letter of the name. The surname gives
`ˌɛsˌiˌAʤˌiˌɑɹˌAvˌiˌiˈɛs`, spelled out the same way. A word that the lexicon holds, such as
`THE` or `POLICE`, is read normally, so the fault hits proper names hardest.

Book B opens 100 scenes with a small-caps run of three to five words, set as ALL CAPS
in the source: `ROWAN KESSLER COULD NOT tell time`. Read as spelled letters, the audiobook
is unusable.

The rule finds a run of `min_caps_run_words` or more consecutive ALL-CAPS words, and
recases the run to title case. `ROWAN KESSLER COULD NOT` becomes `Rowan Kessler Could Not`.
Title case is safe: misaki reads `Rowan` as `ɹˈOsi`, and case does not change the phonemes
of a common word.

The rule keeps a one-word ALL-CAPS token. A single `NO` or `HELP` is emphasis, and the
engine reads a short common word correctly.

The rule keeps a run whose every word is a known initialism, such as `U.S.` or `I.R.B.`.
The rule tests for a period inside the token.

`normalize.recase_caps_run` of `false` turns the rule off. Book A's config sets it to
`false`, because that book holds no small-caps convention and its ALL-CAPS words are
emphasis.

### 6.3 The symbol paragraph rule

A paragraph whose text holds no letter and no digit is removed. A scene break in Book B
is one paragraph that holds `∗`, U+2217. **Kokoro reads that character as the word
"asterisk".** The book holds 11 of them.

The stage removes the paragraph and leaves the paragraph break, so stage 6 still inserts a
pause where the scene changed.

`normalize.drop_symbol_paragraphs` of `false` turns the rule off.

The `config_hash` inputs of this stage: the whole rule table, and the `normalize` object of
the book config.
The `input_hash` of each output: `hash_file()` of `01-extract/<id>.txt`.

---

## 7. Stage 3 — chunk

`abpipe/chunk.py`. Owner: Worker B.

The stage splits the chapter text into sentences. The stage packs the sentences into
chunks. A chunk holds 350 characters at most.

The rules:

1. A chunk never crosses a paragraph boundary.
2. A chunk never splits inside a quotation, unless one quotation alone is longer than the
   limit. The stage then splits that quotation at an internal sentence boundary only.
3. A sentence that is longer than the limit on its own splits at a comma, then at a space.
   The stage never emits a chunk longer than 450 characters.
4. The heading line of a chapter is always its own chunk, `0001`.
5. A chunk is never empty and never holds only punctuation.

The stage writes `03-chunks/<id>/<nnnn>.txt`, numbered from `0001`, four digits, and one
index file `03-chunks/<id>/index.json`:

```json
{
  "schema": 1,
  "chapter": "ch01",
  "chunks": [
    {
      "id": "0001",
      "file": "0001.txt",
      "chars": 9,
      "words": 2,
      "sha256": "8c2f…",
      "is_heading": true,
      "ends_paragraph": true
    }
  ]
}
```

| Field | Rule |
|---|---|
| `is_heading` | `true` only for the heading chunk of a chapter. |
| `ends_paragraph` | `true` when the chunk is the last chunk of its paragraph. |

`ends_paragraph` drives the pause length at assembly. Refer to section 10.

**The index file is the list of chunks.** A later stage reads the index file. A later stage
never lists the directory to find the chunks.

The stage removes a stale chunk file. A shorter chapter must not leave the chunk files of a
longer earlier run behind.

The `config_hash` inputs of this stage: the character limit and the hard limit.
The `input_hash` of the index: `hash_file()` of `02-normalize/<id>.txt`.

---

## 8. Stage 4 — render, and the engine interface

`abpipe/render.py` and `abpipe/engines/`. Owner: Worker B.

The stage reads `03-chunks/<id>/index.json`. For each chunk the stage writes
`04-audio/<id>/<nnnn>.wav`.

The WAV format: 24000 Hz, one channel, 16-bit signed PCM.

Kokoro is deterministic. The same text and the same configuration give the same audio. The
stage manages no random seed.

### 8.1 The engine interface

Each engine is one class in `abpipe/engines/`. Every engine obeys this interface:

```python
class Engine:
    name: str                       # "kokoro_mlx", "kokoro_cpu", "chatterbox"

    def __init__(self, config: dict) -> None: ...

    def describe(self) -> dict:
        """Return the configuration that changes the audio. The render stage
        hashes this dict to make config_hash."""

    def synthesize(self, text: str) -> tuple["numpy.ndarray", int]:
        """Return mono float32 samples in the range -1.0 to 1.0, and the sample rate."""
```

`abpipe/engines/__init__.py` holds `get_engine(config: dict) -> Engine`. The function reads
`config["name"]` and returns the engine of that name. The function imports the engine module
only when it returns that engine, so a missing optional dependency never breaks an unrelated
engine.

| Module | Class | Notes |
|---|---|---|
| `engines/kokoro_mlx.py` | `KokoroMLXEngine` | The primary engine on a Mac. Uses `mlx-audio`. |
| `engines/kokoro_cpu.py` | `KokoroCPUEngine` | The engine on Linux and on any CPU. Uses the PyTorch `kokoro` package. |
| `engines/chatterbox.py` | `ChatterboxEngine` | A stub. Raises `NotImplementedError`. |

**The two Kokoro engines say the same words.** Both use the misaki front end.
`tools/phoneme_parity.py` measures the claim, and
`tests/fixtures/phoneme_parity/` holds the captured phoneme strings. Measured on
2026-08-16: **44 of 44 corpus entries are byte-identical**, over both dialects, and the
corpus holds every hazard this project has met — the pronunciation map, the dialect
apostrophe, the ALL-CAPS run, every number form of section 6.1, the foreign terms, and the
inline homograph markup of section 18.1.

**Warning: that measurement used one shared install of misaki.** It proves that the two
libraries *configure* misaki the same way. It does not prove that two independently
resolved environments hold the same misaki. **Pin misaki to an exact version on every
platform**, and run the parity tool as a canary. A silent misaki upgrade would change how
every book is pronounced, and no test outside this one would see it.

### 8.1.1 `preflight()` — an optional engine method

An engine may hold one more method:

```python
def preflight(self) -> dict:
    """Load the model, check the front end, render a warmup, and report."""
```

It returns `espeak_fallback`, `warmup_samples`, `warmup_sample_rate`, `oov_probe_word`, and
`oov_probe_nonempty`. It raises when the engine cannot render correct audio.

`KokoroCPUEngine` holds it. The method is the **only reliable detector** of the silent
word-deletion fault of section 17.1 on that engine. Refer to 17.2.

A caller runs `preflight()` once, before a long unattended render. The render stage does
not call it, because the check costs a model load.

**A test never loads a model.** A test passes a fake engine object to the render function.
The render function takes the engine as an argument, with a default of `None`, and calls
`get_engine()` only when the argument is `None`.

**The stage applies the pronunciation map before it synthesises.** Refer to section 9.6.
The map corrects a word the engine says wrongly. The chunk file on disk keeps the author's
spelling; only the text handed to the engine changes.

The `config_hash` inputs of this stage: `engine.describe()`, **and the `pronunciations` map
of `book.json`**.
The `input_hash` of each WAV: the `sha256` of the chunk from the index file.

### 8.2 The stage stops on a fault that cannot heal

The stage renders more than 2,000 chunks in one unattended run. One bad chunk must not stop
the run. A fault that repeats must stop it at once.

| Fault | Action |
|---|---|
| A full disk, or a read-only disk | Stop the run at once. |
| Five failures in a row | Stop the run. `max_consecutive_failures` sets the number. |
| One failure, then a success | Continue. The count resets. |

**Warning: `soundfile` does not raise `OSError` on a full disk.** It raises
`LibsndfileError`, a subclass of `RuntimeError`, and that object holds no `errno`. The
detector must match the message as well as the error number, or the guard never fires.

A failed write removes its own temporary file. A run that leaves 2,000 temporary files on a
full disk has made the problem worse.

The summary of the stage holds `aborted` and `abort_reason`, so a caller can tell "finished
with 3 failures" apart from "gave up at chunk 200".

---

## 9. Stage 5 — QC

`abpipe/qc.py`. Owner: Worker C.

The stage transcribes each chunk WAV with `mlx-whisper`. The stage compares the transcript
to the chunk text. The stage flags a chunk that does not match.

### 9.1 The comparison

The stage normalises both sides with the same function, `qc_normalize(text)`:

1. **Expand every number to words**, with `num2words`. Whisper writes `1920` and `15th`.
   Stage 2 already turned every number of the source into words. Without this step the two
   sides can never match. This step runs first, while the digits are still intact.
2. Lower case.
3. Remove every character that is not a letter, a digit, or a space. This step removes the
   dialect apostrophe, so `an'` and `an` become the same token.
4. Collapse a run of spaces.
5. **Collapse a degenerate repeat.** A token that repeats more than `max_token_repeat`
   times in a row collapses to one token. Refer to 9.5.
6. **The stage does not rewrite either side with the equivalence table.** Two tokens are
   equivalent when they belong to the same **equivalence class**, checked at match time by
   `tokens_equivalent()`. A class holds one or more members, and a member is one word or a
   multi-word phrase. `resolve_equivalences()` builds each class from the optional
   `equivalences` key of `qc-config.json`, merged over `DEFAULT_EQUIVALENCES`, so each book
   can add its own dialect.

   The default classes: `ye`~`yeh`~`you`, `yer`~`your`, `an`~`and`, **`o`~`of`~`oh`**,
   `dhye`~`do you`, `shure`~`sure`, `lemme`~`let me`, `atall`~`at all`, `t`~`to`,
   `mesel`~`messel`~`myself`, `ould`~`old`, `fellah`~`fella`.

   **Warning: a token can carry two meanings, and a rewrite destroys one of them.** This
   dialect writes `o'` for "of", as in `no way o' findin'`. It also writes `O` for the
   vocative "oh", as in `"O Lord!"`. An earlier version rewrote `o` to `of`, which made
   `"O Lord!"` normalise to `of lord`, so it could never match the `oh lord` that whisper
   correctly heard. That chunk failed on audio that was perfect. A class holds both
   meanings and rewrites nothing.

   **`me` is not in the table.** `me` is an ordinary English pronoun, and this dialect also
   uses it for `my`. A mapping would corrupt every ordinary use in the book to fix a few
   dialect uses.
7. **Apply the pronunciation map to the source side only.** Refer to 9.6.

The stage then computes two numbers on the token lists:

| Number | Definition |
|---|---|
| `wer` | The Levenshtein distance of the two token lists, divided by the token count of the source. |
| `coverage` | The count of source tokens that the alignment matches, divided by the token count of the source. |

**The match is also phonetic.** When the fuzzy rule below misses, the stage compares a
consonant skeleton of the two tokens, and equal skeletons match. This is the rule that
matters most for Book A. Kokoro says the name `Gyko` as "jai-po" and whisper writes
`jaipo`; the soft-g rule joins them. A final `-ng` reduces to `-n`, which joins `lookin`
and `looking`. A final `t` or `d` after a consonant drops, which joins `hist` and `his`.
The rule applies only to a token of two characters or more, so short tokens cannot collide.

**The match is fuzzy, not exact.** Whisper spells a word its own way. `grey` and `gray`,
`panelled` and `paneled`, and `McAllister` and `McAlister` are the same word for this
purpose. Two tokens match when `1 - levenshtein / max(len)` is `token_similarity_min` or
more. A source token also matches the join of two adjacent hypothesis tokens, because
whisper splits a compound: `coalheaver` becomes `coal heaver`, and `account` becomes
`a count`.

**The join is symmetric.** Whisper also merges two words into one, which is the opposite of
the split above. Two adjacent source tokens match a single hypothesis token when their
concatenation passes the same fuzzy or phonetic test. A real render of this book elides
`ye an'` into one spoken syllable, and whisper writes the single token `yen`. `ye` and
`an'` become `you` and `and` through the equivalence table, and the join `youand` shares
the phonetic key `YN` with `yen`, so the pair matches. Both source tokens then count as
matched for `coverage`, and the pair costs 0 edits for `wer`, not 2.

**The join is not limited to two tokens.** A run of up to four adjacent tokens, on either
side, joins to match a single token on the other side. That is what lets `I`, `R`, `B`
match the single hypothesis token `irb`, which is how whisper writes the initialism
`I. R. B.`

**Both directions use one predicate**, so a merge that is neither fuzzy nor phonetic is
never forgiven. `the quick brown fox` against `the fox` still fails.

Measured on one real 64-second render of this book: exact matching gives coverage 0.956,
and fuzzy matching with the join rule gives 0.983. **Exact matching false-flags a perfect
render.** The fuzzy rule is required, not an improvement.

### 9.2 qc-config.json

```json
{
  "schema": 1,
  "wer_max": 0.15,
  "coverage_min": 0.90,
  "token_similarity_min": 0.85,
  "duration_outlier_factor": 3.0,
  "min_tokens_for_wer": 8,
  "min_chars_for_duration_test": 15,
  "max_token_repeat": 2,
  "whisper_model": "mlx-community/whisper-large-v3-turbo",
  "condition_on_previous_text": false,
  "equivalences": {},

  "whisper_backend": "auto",
  "faster_whisper_model": "small.en",
  "faster_whisper_compute_type": "int8",
  "faster_whisper_cpu_threads": 0,
  "faster_whisper_beam_size": 5
}
```

These values come from a measurement, not from a guess. Refer to `NOTES.md`.

`equivalences` is optional. The stage merges it over `DEFAULT_QC_CONFIG`. Refer to 9.1.

### 9.2.1 The two transcriber backends

`mlx-whisper` runs on Apple silicon only. The pipeline also runs on Linux, on a CPU, so the
stage holds a second transcriber.

| Class | Library | Platform |
|---|---|---|
| `WhisperTranscriber` | `mlx-whisper` | Apple silicon |
| `FasterWhisperTranscriber` | `faster-whisper`, CTranslate2, int8 | Any CPU |

`whisper_backend` selects one. `auto`, the default, picks `mlx` when `mlx_whisper` imports,
and picks `faster` otherwise. Both classes obey the same interface, and `qc.run()` already
accepts a transcriber through its injection seam.

**`condition_on_previous_text` is `False` on both backends, always.** Refer to 9.5. The
measured runaway repeat is a property of whisper, not of one library.

**Warning: `faster-whisper` returns a lazy generator of segments.** No decoding happens
until a caller consumes it. A caller that forgets returns an empty transcript, and an empty
transcript passes the gate over audio that nobody checked.

### 9.2.2 The default-omission rule

**Warning: a new key in `DEFAULT_QC_CONFIG` stales every chunk of every book.** The
`config_hash` of this stage covers the merged qc-config. Four delivered, green audiobooks
would have re-run their whole QC stage for no change in behaviour.

The rule: `NEW_QC_CONFIG_KEYS_SINCE_LINUX_ENGINES` names each key added after the four
books shipped. `_config_for_hash()` drops such a key from the hashed copy **when the key
still holds its default value**. A book that never heard of these keys therefore hashes
exactly as it did before they existed. A book that sets one to a different value does go
stale, which is correct: a different transcriber gives a different transcript.

A test pins the literal hash of a representative config, measured before the change. That
is the only form of this test that is worth anything.

**`qc_config_hash()` is the one public formula.** `cli.py` calls it. Refer to the warning
in section 14: a duplicated hash formula has already shipped one fault in this project.
**A caller never writes the formula again.**

**The file lives at `work/<slug>/qc-config.json`, one for each book.** The stage writes the
file with the measured defaults when the file is absent. The stage seeds `equivalences`
from `qc.equivalences` of the book config. The stage never overwrites a file that exists,
because a human may have tuned it.

**Warning: a threshold is never widened to turn a red gate green.** `coverage_min` stays
0.90 and `wer_max` stays 0.15 for every book. `equivalences` is the correct tool for a
foreign term that whisper writes its own way. Refer to `PROGRESS.md`.

The `config_hash` of this stage covers `qc-config.json`, `engine.describe()`, **and the
`pronunciations` map of `book.json`**. The map changes the comparison, so a change to it
must make every chunk stale.

### 9.6 The pronunciation map

`book.json` holds a `pronunciations` object. The map respells a word so that the engine
says it correctly.

```json
"pronunciations": { "Gyko": "Gikko" }
```

**The map applies in two places, and both use the same table:**

1. **Stage 4 substitutes the text before it synthesises.** This changes the audio. The
   listener hears the corrected pronunciation.
2. **Stage 5 substitutes the source side before it compares.** The transcript then matches
   what the engine actually said, so the correction does not become a QC failure.

**The map never changes the book.** Stage 1 and stage 2 write the author's spelling.
`01-extract`, `02-normalize`, and `03-chunks` all hold `Gyko`. Only the audio and the QC
comparison see `Gikko`. A reader of the artifacts sees the real text.

The substitution matches a whole word, with `\b` at each end. That rule also corrects the
possessive: `Gyko's` becomes `Gikko's`, because the apostrophe is a word boundary.

The map belongs to the `config_hash` of stage 4 and of stage 5. A change to it makes every
chunk stale, so the audio is rebuilt.

#### Why the respelling

Measured with the real engine, on the character name that motivated this feature. The
misaki front end gives these phonemes for the two spellings:

| Spelling | Phonemes | Reading |
|---|---|---|
| original spelling | `ʤˈIpQ` | "JYE-po", a long i |
| corrected spelling | `ʤˈɪpQ` | **"JIP-oh", a short i** |

Whisper confirms the audio: the name spoken with the original spelling transcribes with the
long-i reading, and the name spoken with the corrected spelling transcribes with the
short-i reading. The consonant is already the soft `ʤ` in both, which is correct for this
name.

**Warning: an entry here is a product decision.** The name occurs 508 times, 439 plain and
69 possessive, so the reading shapes the whole audiobook. The maintainer set this one and
can veto it. Refer to `PROGRESS.md`.

A chunk is flagged when any of these is true:

- `wer` is greater than `wer_max`, and the source holds `min_tokens_for_wer` tokens or more;
- `coverage` is less than `coverage_min`;
- the duration of the chunk is more than `duration_outlier_factor` times the **expected**
  duration for its character count, and the chunk holds `min_chars_for_duration_test`
  characters or more. This test finds a runaway: audio many times longer than the text
  warrants.

  The expected duration is fit for each chapter as `intercept + slope * chars`, with a
  Theil-Sen regression. The regression is robust, so the one outlier the test hunts for
  cannot drag the fitted line toward itself and hide.

  **Warning: never compare the ratio of duration to characters against a median.** That is
  a model with no intercept, and it is wrong. Real duration holds a fixed cost of 0.2 to
  0.3 seconds of leading and trailing silence. For a short chunk that fixed cost dominates
  the ratio, so a short chunk trips the test however good its audio is. Measured on chapter
  5: a **correct** 1.43-second render of the one-word chunk `"Gyko!"` scored 3.1 times the
  chapter median, over the 3.0 threshold, and the chapter heading scored 2.3 times.

  A chunk under `min_chars_for_duration_test` is exempt outright. That guard has the same
  shape as `min_tokens_for_wer`.

### 9.2.1 A very short chunk is checked by its audio, not by its transcript

A chunk with fewer than `min_tokens_for_coverage` source tokens **skips the wer and
coverage gate**. A transcript comparison is not meaningful at that length: whisper holds no
context and invents. It returned `oh thats not good` for a chunk whose whole content is
`"Aha!"`.

The stage checks the audio instead. The chunk passes when its RMS is at least
`min_rms_for_short_chunk`, and its duration falls between `short_chunk_duration_factor_low`
and `duration_outlier_factor` times the chapter's fitted expected duration. The resolution
is `audio_only`, which is deliberately not `ok`.

**Warning: this does not verify that the words are correct.** It verifies that real,
non-silent audio of a plausible length exists. The stage still records the wer and coverage
it measured, so the numbers are honest, but it does not gate on them. A silent or
wrong-length short chunk still flags and still runs the ladder.

### 9.3 The remediation ladder

The stage scores every attempt below **before** it decides what stays on disk. **The
best-scoring attempt survives, never merely the last one tried.**

1. Render the chunk again, into a scratch file. The real WAV and its meta are not touched.

   **Warning: Kokoro is not deterministic. An earlier version of this contract said it was,
   and that was wrong.** Measured on 2026-08-16: three consecutive calls in one process,
   with the same text, voice, speed and loaded model, returned three different SHA-256
   hashes. The sample count was identical at 76,200 and the peak level varied by near 3
   percent, so the difference is real signal, not last-bit rounding.

   The stage may still compare the scratch bytes against the current WAV and reuse the
   first score when they match. **Expect that shortcut never to fire.**

   **This rung only works because the engine is not deterministic.** Rendering a chunk
   again is worth doing precisely because it returns different audio, and the ladder
   promotes the better result.
2. Find a real internal boundary in the source text: a sentence end, then a comma, then a
   space. Each candidate must leave real content on both sides. Render each half into a
   scratch file and join them. **When no such boundary exists, skip this step.** A single
   word has nowhere safe to cut.
3. Compare every attempt that ran. The one with the fewest flags wins, and a tie goes to
   the earliest and cheapest attempt. The stage moves the winner onto the real WAV path and
   writes its meta. The stage never writes a meta beside a WAV it did not just write, so
   the hashes always match the bytes.
4. If the winner still holds a flag, mark the chunk `needs_human`. **The file left on disk
   is still the best attempt, not the worst.**

**Warning: this ladder once destroyed good audio.** Chapter 5, chunk 0002, is the single
word `"Gyko!"`. The first render was correct at 1.43 seconds, and the old duration rule
flagged it anyway. The re-render was identical, so it flagged again. The split then cut a
one-word chunk in the middle, made two nonsense fragments, joined them into 2.42 seconds
that transcribed as "jayu", and **wrote that over the good file**. A false flag became a
real fault, and the worst of the three attempts was the one that survived. Both halves of
that failure are fixed above.

**A chunk marked `needs_human` fails the stage.** The stage exits with a non-zero code.
Stage 6 refuses to run while `qc-report.json` holds one or more `needs_human` chunks.

### 9.5 The whisper settings

**Warning: whisper hallucinates a runaway repeat.** A default transcribe call on a real
render of this book produced the word `stir` about two hundred times at the end of the
audio. That transcript would flag good audio, spend a re-render and a split, and then stop
the pipeline with a false `needs_human`.

`condition_on_previous_text=False` removed the repeat completely. The stage always passes
it. The repeat-collapse step of 9.1 is the second guard.

A transcript repeat is an insertion. It leaves `coverage` near 1.0 and drives `wer` above
1.0. An audio runaway is a different fault, and the duration test of 9.2 finds that one.
The two faults record different flags.

Whisper runs at a real-time factor of 0.066 on this Mac once the weights are cached. The
first call downloads the weights and takes about 106 seconds more.

### 9.4 The output files

`05-qc/<id>/<nnnn>.json`:

```json
{
  "schema": 1,
  "chapter": "ch01",
  "chunk": "0001",
  "source_norm": "chapter one",
  "transcript_norm": "chapter one",
  "wer": 0.0,
  "coverage": 1.0,
  "duration_s": 1.82,
  "attempts": 1,
  "flags": [],
  "resolution": "ok"
}
```

`resolution` holds one of `ok`, `re_rendered`, `split`, `needs_human`, `audio_only`, or
`accepted`. The set is closed.

`05-qc/qc-report.json`:

```json
{
  "schema": 1,
  "generated_at": "20260815T210000Z",
  "thresholds": { "wer_max": 0.10, "coverage_min": 0.95, "duration_outlier_factor": 3.0 },
  "chapters": {
    "ch01": { "chunks": 42, "flagged": 1, "re_rendered": 1, "split": 0, "needs_human": 0, "duration_s": 901.4 }
  },
  "totals": { "chunks": 812, "flagged": 9, "re_rendered": 8, "split": 1, "needs_human": 0, "duration_s": 28611.2 },
  "status": "green"
}
```

`status` holds `green` or `red`. `red` means one or more chunks hold `needs_human`.

**The report is the whole book.** A QC run of one chapter updates that chapter's key and
leaves every other key as it was.

The `config_hash` inputs of this stage: the whole `qc-config.json`, and `engine.describe()`.
The `input_hash` of each chunk report: `hash_many()` of the chunk sha256 and the WAV sha256.

---

### 9.7 Human acceptance

The ladder can raise a concern. **Until now nothing could answer one.** A chunk that
reached `needs_human` stopped the pipeline for ever. Across more than 2,000 chunks a few
genuine whisper mishearings are certain — a proper noun, an archaic interjection — so the
book could never have shipped.

`work/<slug>/qc-accept.json` records that a person looked at a chunk and judged the audio
correct.

```json
{
  "schema": 1,
  "accepted": [
    { "chapter": "ch07", "chunk": "0130", "wav_sha256": "…",
      "reason": "Whisper garbles the proper noun 'You Connor' as 'Ucona'. The audio is correct.",
      "accepted_at": "20260815T…Z" }
  ]
}
```

| Rule | Detail |
|---|---|
| The pin | An acceptance applies only while the chunk's WAV hashes to `wav_sha256`. |
| **What the pin can and cannot mean** | It means *the audio was re-rendered*. It does **not** mean *the audio changed meaningfully*. Kokoro is not deterministic, so a re-render always changes the bytes even when the words, the length and the reading are the same. Refer to section 9.3. |
| **Every pin voids on every re-render, always** | Write this outright. A reader who finds a voided pin will otherwise treat it as evidence that something changed. It is not. An acceptance means only that **a person judged this text's audio acceptable once.** |
| **Re-read the reason, not only the hash** | Before re-affirming, check whether the pass made the *reason* false. A reason that records a known defect is falsified when a later pass fixes that defect, and re-pinning the old words would write a lie. A reason that describes a whisper artifact survives. Re-measure any phoneme a reason quotes. |
| **The pin is the point** | Re-render the chunk and the acceptance dies with it. Nobody can accept a chunk and then change the audio underneath it. |
| The reason | Required, and not empty. An entry with no hash or no reason is ignored, with a warning. |
| The writer | `accept_chunk()` only. **The stage never writes this file on its own.** |
| The order | The check runs **after** the ladder reaches its own verdict, never as a shortcut around scoring. |
| The count | `qc-report.json` counts `accepted` separately. An accepted chunk never counts as `needs_human`, so `status` stays green. |

**This is much narrower than `allow_unverified`,** which bypasses the gate for a whole
chapter. Acceptance names one chunk, pins one audio file, and carries one written reason.


## 10. Stage 6 — assemble

`abpipe/assemble.py`. Owner: Worker D.

The stage joins the chunk WAV files of one chapter into `06-chapters/<id>.wav`. The stage
then makes `06-chapters/<id>.m4a`.

### 10.1 The silence table

| Position | Silence |
|---|---|
| After a chunk inside a paragraph | 0.35 s |
| After a chunk with `ends_paragraph` true | 0.70 s |
| After the heading chunk | 1.20 s |
| After the last chunk of a chapter | 2.00 s |

The stage trims the trailing silence of each chunk WAV first, to a level of -50 dBFS, so
the table controls the whole pause. The stage applies a 5 ms fade in and a 5 ms fade out to
each chunk edge. A fade prevents a click.

### 10.2 The loudness pass

The stage runs a two-pass `loudnorm` filter with ffmpeg: `I=-18`, `TP=-2`, `LRA=11`. Pass
one measures. Pass two corrects with the measured values. The stage then encodes AAC at
64 kbit/s, one channel, into `06-chapters/<id>.m4a`.

### 10.3 chapters.ffmeta

The stage writes `06-chapters/chapters.ffmeta` after every chapter m4a file exists. The
file is FFMETADATA1. The offsets are exact milliseconds, measured with `ffprobe` on the m4a
files, in chapter order.

```
;FFMETADATA1
[CHAPTER]
TIMEBASE=1/1000
START=0
END=6000
title=Opening Credits
```

The `config_hash` inputs of this stage: the silence table and the loudness settings.
The `input_hash` of a chapter output: `hash_many()` of the WAV hashes of its chunks.

---

## 11. Stage 7 — bind

`abpipe/bind.py`. Owner: Worker D.

The stage joins the chapter m4a files into one file, `07-book/<title>.m4b`. The stage uses
the ffmpeg concat demuxer and copies the audio stream. The stage re-encodes nothing.

The stage adds `06-chapters/chapters.ffmeta` as the chapter list. The stage adds `cover.jpg`
as an attached picture with `disposition:v:0 attached_pic`.

The tags: `title`, `artist`, `album`, `date`, `genre`, and `media_type=2`. `title` and
`album` both hold the book title. `artist` holds the author. `media_type=2` marks an
audiobook.

The `config_hash` inputs of this stage: the tag values and the cover hash.
The `input_hash`: `hash_many()` of the m4a hashes and the ffmeta hash.

---

## 12. Stage 8 — deliver

`abpipe/deliver.py`. Owner: Worker D.

**This stage is not part of the vendored copy.** `deliver.py` hard-codes one operator's
server address, home directory, and public domain, so the vendoring tool excludes it. The
application that vendors this pipeline implements its own delivery targets. The section
stays, because the lesson below applies to any delivery stage that polls a paginated API.

**Warning: the pipeline never authors content on the server.** The stage copies a finished
file. The stage runs no editor and no generator on the server.

The steps:

1. `rsync` the m4b file and `cover.jpg` to
   `<server>:~/Audiobooks/<author>/<title>/`. The stage makes the directory first.
2. Read the Audiobookshelf API token and the library id over SSH, from the server's
   Audiobookshelf configuration database.
   **Warning: the database holds a malformed trigger.** Every query starts with
   `PRAGMA writable_schema=1;`. Every query is read-only.
3. `POST /api/libraries/<library_id>/scan` with the header `Authorization: Bearer <token>`.
4. Poll the library items API until the item appears, **paginating through every page**.
   One call returns one page, and the item is not guaranteed to land on the first one. The
   timeout is 300 seconds.

   **Warning: a fixed page size silently breaks the check.** The stage once asked for 500
   items and stopped there. The library grew to 561, the new book fell outside the first
   page, and the poll could never succeed however long it waited. The delivery was correct
   and the verification reported failure. The loop stops on an empty page, on a repeated
   page, or at a hard page cap, so a malformed server cannot spin it for ever.
5. Assert the title, the author, the chapter count, and the duration. The duration is
   correct within 5 percent of the assembled length.
6. Print the item URL at the library's public address.

The stage is idempotent. A second run copies nothing new and verifies again.

### 12.1 The ffmpeg runner

`abpipe/ffmpeg.py`. Owner: Worker D. Stages 6, 7, and 8 call ffmpeg only through this
module. A test stubs this module. A test never runs ffmpeg on real audio.

```python
run(args: list[str], check: bool = True) -> subprocess.CompletedProcess
probe_duration(path) -> float          # seconds
probe_json(path) -> dict               # the ffprobe JSON output
```

---

## 13. The stage entry point

Every stage module holds one function with this signature:

```python
def run(ctx: Context, chapters: list[str] | None = None, force: bool = False, **kw) -> dict:
    """Run this stage. Return a summary dict."""
```

`chapters` holds a list of chapter ids, for example `["ch04"]`. `None` means every chapter.

The summary dict always holds `stage`, `done`, `skipped`, and `failed`. A stage adds its own
keys after those four.

`Context` lives in `abpipe/context.py`. The class holds the paths of the book directory and
the parsed `book.json`.

```python
ctx.root          Path   the project root
ctx.slug          str    the book slug
ctx.book_dir      Path   work/<slug>
ctx.epub          Path   the source EPUB
ctx.book          dict   the parsed book.json, or {} before stage 1 runs
ctx.stage_dir(n)  Path   the directory of stage n, made if absent
ctx.chapter_ids() list   the chapter ids from book.json, in order
```

---

## 14. The command line

`abpipe/cli.py`. Owner: Worker E.

**This module is not part of the vendored copy.** `cli.py` imports `deliver.py` at the top
level, so it could not survive that module's exclusion. The application that vendors this
pipeline calls each stage module directly and needs no command-line front end. The section
stays for its lessons on wrong-book protection, in particular 14.1.

```
abpipe extract    [--book PATH] [--force]
abpipe normalize  [--chapter ID …] [--force]
abpipe chunk      [--chapter ID …] [--force]
abpipe render     [--chapter ID …] [--force]
abpipe qc         [--chapter ID …] [--force]
abpipe assemble   [--chapter ID …] [--force]
abpipe bind       [--force]
abpipe deliver    [--dry-run]
abpipe prune      [--chapter ID …] [--dry-run]
abpipe all        [--force] [--prune]
abpipe status
```

Global options: `--book PATH` names the EPUB. `--slug NAME` names the book directory.
`--config PATH` names the book config, and defaults to `source/<slug>.config.json`.

**The book config is the primary source of all three values.** `--config` names the file.
The file holds `slug` and `source_epub`, so `--slug` and `--book` are only needed to
override the file. The order of precedence is: the command line, then the book config, then
`book.json`, then the built-in defaults.

The CLI loads the config once, with `extract.load_book_config()`, and passes the result
into every stage that needs it.

### 14.1 Three rules that stop a run writing into the wrong book

**Warning: a wrong-book run destroys a shipped audiobook.** This is not a hypothesis. On
2026-08-15 `abpipe extract --config source/book-b.config.json` wrote Book B's
chapters, `book.json`, and `cover.jpg` into `work/book-a/`, over the artifacts of a
book that was already live in Audiobookshelf. The cause was a chain of three small faults,
and each one gets a rule.

**Rule 1: the `slug` argument of the loader is a fallback, never an override.** The loader
uses it only when the config file is absent, or when the file omits its own `slug`. **A
config file that declares a `slug` always wins.** The old behaviour returned the named
file's contents under the caller's slug, which is a config that claims to be one book while
carrying another book's EPUB. When the caller passes a slug that contradicts a slug the
file declares, the loader raises `ValueError` and names both.

**Rule 2: the CLI never feeds a `book.json` slug into the loader.** `book.json` sits below
the book config in the precedence order, so using it to resolve the config inverts that
order. That inversion is what manufactured the agreement that defeated the mismatch guard.
When `--config` is given and `--slug` is not, the slug comes from the config file.

**Rule 3: the identity check before any write.** Before a stage writes, the CLI compares
the resolved book against the book already in the target directory. When
`work/<slug>/book.json` exists and its `source_sha256` differs from the hash of the
resolved EPUB, the run is **refused** with a message that names both books. `--rebook`
overrides the refusal, for the real case of rebuilding a directory against a new source
file.

The mismatch guard between `--slug` and `--config` stays. It is necessary and it was not
sufficient.

`abpipe status` prints one line for each stage: the count of outputs that are fresh, stale,
and absent.

**Warning: `status` must hash the same configuration the stage hashes.** An earlier version
hashed `extract.DEFAULT_CHAPTER_PATTERN` directly, at `cli.py:553`. A book that passes its
own pattern would then report every chapter stale for ever. `status` asks the stage for its
own `config_hash`, built from the loaded book config.

Each command prints a summary line and exits with `0` on success. A command exits with `1`
when the stage reports a failure.

---

## 15. The prune stage

`abpipe/prune.py`. The stage saves disk space.

**Warning: this Mac holds near 1 GB of free disk.** The full book needs near 3.2 GB of
intermediate files: 1.33 GB of chunk WAV files, 1.33 GB of chapter WAV files, 0.23 GB of
chapter m4a files, and a 0.23 GB m4b file. The render stops with a full disk part of the
way through, near midnight, after hours of work. The prune stage prevents that.

The idea: a chapter that holds a finished, verified `.m4a` needs none of the audio that
built it. The chunk WAV files and the chapter WAV file are intermediate. The m4a file is
durable.

```python
prune_chapter(ctx, chapter_id: str, dry_run: bool = False) -> dict
```

The function removes `04-audio/<id>/*.wav` and `06-chapters/<id>.wav`, and the meta file of
each. The function **refuses** unless all of these hold:

1. `06-chapters/<id>.m4a` exists, and its meta file is fresh.
2. The QC report holds an entry for the chapter, and that entry holds no `needs_human`.
3. `probe_duration()` of the m4a file is more than zero.

The function returns `{"chapter", "files", "bytes", "pruned"}`.

**A pruned chapter stays resumable at the chapter level.** The m4a file and its meta file
survive, so stage 6 skips the chapter. Stage 4 and stage 5 would do the chapter again,
because their outputs are gone. That trade is correct: the m4a file is the artifact that
matters, and a whole chapter is a reasonable unit of lost work.

The prune stage writes `06-chapters/<id>.pruned.json` to record what it removed, so
`abpipe status` can tell "pruned" apart from "never rendered".

The command line gets `abpipe prune [--chapter ID …] [--dry-run]`, and `abpipe all` gets
`--prune`, which prunes each chapter as soon as that chapter's m4a file is verified.
**The default is not to prune.** A full disk is the only reason to prune.

### 15.1 Warning: prune a book only when no fix is pending

**Pruning turns "re-render one chunk" into "re-render the whole chapter."** Stage 4 and
stage 5 lose their outputs when a chapter is pruned, so a one-chunk correction later costs
the whole chapter.

Book B is the measured example, and the real number came in worse than the estimate.
Its render used `--prune`, and 9 wrong heteronym readings were recorded against it after
delivery. The prune had removed **all 4,011 chunk WAV files**, so every one of the six
affected chapters re-renders in full: **2,936 chunks, near 55 minutes, to correct 9
chunks.** That is 71 percent of a whole book rebuild for a fix that is a few seconds of
audio.

**Rule: prune when a book is finished and no fix is pending. Do not prune by default.** The
disk cost of keeping the audio is near 1.7 GB for a chapter. That is small against the
headroom this machine normally holds, and small against an hour of re-render.

**What stopped it being a 100 percent rebuild is worth stating, because it is the one part
of prune that is load-bearing.** `prune_chapter()` removes `04-audio/<id>/*.wav` and
`06-chapters/<id>.wav`, and it **keeps `06-chapters/<id>.m4a`**. The m4a is the durable
artifact. Because the four unaffected chapters still held theirs, stage 6 reused them
untouched and only the six affected chapters were rebuilt. **Never extend prune to the m4a
file.** Doing so would turn any later fix into a guaranteed full rebuild.

### 15.2 A pruned chapter must not crash the QC stage

`abpipe qc` with no `--chapter` flag **crashes on a pruned chapter**. `qc._process_chapter`
calls `hash_file(wav_path)` before it checks that the file exists, so a pruned chapter's
missing WAV raises `FileNotFoundError` and ends the whole command.

The stage must treat an absent WAV in a pruned chapter as a skip, and must report the count
of chapters it skipped for that reason. A pruned chapter is a normal state, not a fault.

---

## 16. The ownership map

**Two workers never hold the same file.** This table is the whole map.

| Owner | Files |
|---|---|
| Overlord | `CONTRACT.md`, `PROGRESS.md`, `NOTES.md`, `pyproject.toml`, `abpipe/__init__.py`, `abpipe/context.py`, `source/*.epub`, `source/*.config.json`, `work/`, `samples/` |
| Worker A | `abpipe/extract.py`, `abpipe/normalize.py`, `tests/test_extract.py`, `tests/test_normalize.py`, `tests/fixtures/` |
| Worker B | `abpipe/chunk.py`, `abpipe/render.py`, `abpipe/engines/*`, `tests/test_chunk.py`, `tests/test_render.py`, `tests/test_engines.py` |
| Worker C | `abpipe/qc.py`, `tests/test_qc.py` |
| Worker D | `abpipe/assemble.py`, `abpipe/bind.py`, `abpipe/deliver.py`, `abpipe/ffmpeg.py`, `tests/test_assemble.py`, `tests/test_bind.py`, `tests/test_deliver.py` |
| Worker E | `abpipe/cli.py`, `tests/test_cli.py`, `tests/test_resume.py` |
| Worker F | `README.md`, `proof/specs/*`, `proof.config.json` |
| Worker G | `abpipe/prune.py`, `tests/test_prune.py` |
| Worker H | `abpipe/homographs.py`, `abpipe/homograph_tiers.py`, `abpipe/data/*`, `tests/test_homographs.py`, `tools/build_heteronyms.py` |
| Worker D1 | `tools/cdl_to_epub.py` |

**The kernel files are frozen.** `abpipe/context.py` belongs to the
overlord. A worker that needs a change in a kernel file asks the overlord. A worker never
edits a kernel file.

**Only the overlord commits.** A worker never runs a git command that writes.

---

## 17. Triage — the pre-stage of a new book

**Warning: a wrong pronunciation is invisible to this pipeline.** The QC matcher compares
words by sound and drops every vowel that is not the first one, so `gyko`, `gikko`, and
`jikko` are one token to it. QC measures whether the words are present and in order. QC
does not measure whether the engine says them well. Only a person who listens finds a wrong
reading. Refer to `NOTES.md`.

Triage is the answer. **Every book passes triage before any full render.** Triage is
overlord work, not worker work. Triage writes no code. Triage has four steps.

**T-0 Inventory.** Read the EPUB. Confirm that the file holds no DRM. List the spine and
the TOC labels. Count the note markers, the tables, the figures, the captions, and the
epigraphs. Survey the digits, the non-ASCII characters, and the foreign terms. Measure any
suspect word with misaki, as `Gikko` and `guivin` were measured. Write the result as a
hazard table in `NOTES.md`.

**T-1 Book config.** Write `source/<slug>.config.json` from the inventory. Refer to section
4.1.

**T-2 Sample render and listen check.** Render a passage of about 90 seconds into
`samples/<slug>-<voice>.wav`. **Choose the passage for the hazards, not for the prose:**
the worst name, the foreign terms, a number, and a caps run. QC the sample. The maintainer
listens before the full render starts.

**T-2.5 Homograph audit.** Run `abpipe homographs` after the chunk stage and before the
full render. Resolve every class A disagreement. Read the class B and C report. Refer to
section 18.

**T-3 Full run.** Only after T-2. Each stage is idempotent, so each book is an independent,
resumable unit.

### 17.1 The render discipline

One book renders at a time. This Mac holds 16 GB of memory.

**Warning: the disk is the constraint, and the pipeline is rarely the cause.** The APFS
Data volume runs near 95 percent full, so the 22 GB of apparent free space is the whole
slack of the machine. Swap and every running job compete inside it. Four Book A render
attempts died this way.

Before every render:

1. Quit Docker Desktop. **A plain quit does not kill `com.docker.backend`.** Check with
   `pgrep -f com.docker.backend`, and kill the process when it survives.
2. **Kill any leaked `ugrep` process.** Run `pgrep -fl ugrep` and end anything left from a
   finished job.

   **Warning: a leaked search process holds gigabytes of swap for days.** Three of them,
   one **2.5 days old**, held near 5.7 GB of swap under both Optimus runs on 2026-08-16.
   The swap released within seconds of the kill, and macOS then reclaimed 20 swapfiles on
   its own. The apparent difference between two books' disk appetites was entirely this,
   and neither pipeline needed a change.

   A `Monitor` tool leaves one of these behind when its watch ends. **End your own
   monitors when the job they watch is finished.**
3. Check the free disk with `df -h /`. Swap self-reclaims once the pressure ends, so a
   settle pause is enough; a reboot is not needed.
4. Arm the disk watchdog.
5. Use `abpipe all --prune` only when the headroom is under 5 GB, and only when no fix is
   pending for the book. Refer to section 15.1.

After every render:

6. **Grep the render log for `EspeakFallback not Enabled`.**

   **Warning: a word the misaki lexicon does not hold is spoken by the espeak fallback. If
   that fallback fails to construct, `mlx-audio` logs this warning once and then drops
   every unknown word from the audio, silently.** The QC gate cannot see the loss, because
   the transcript and the source both lose the same word.

   The mechanism is in the library source, at
   `mlx_audio/tts/models/kokoro/pipeline.py`:

   ```python
   try:
       fallback = espeak.EspeakFallback(british=lang_code == "b")
   except Exception as e:
       logging.warning("EspeakFallback not Enabled: OOD words will be skipped")
       fallback = None
   self.g2p = en.G2P(trf=trf, british=..., fallback=fallback, unk="")
   ```

   **`unk=""`.** An unresolved word becomes the empty string. There is no placeholder, and
   there is no second warning. **One line at construction is the whole detection surface**,
   which is why this grep is required and not optional.

   **`unk=""` applies even when the fallback works.** A word that misaki misses and espeak
   also fails to resolve is deleted just the same. A per-book list of out-of-lexicon words
   is therefore a list of candidates for silent deletion, not only for an odd accent.

   The cost scales with the book. Book C holds 706 distinct foreign
   terms; on that book the difference between a working fallback and a failed one is the
   difference between an accented reading and missing words. The check is one grep.

   **Grep every log of the book, not only the last one.** A failed attempt writes its own
   log, and the warning can sit there while the successful run is clean.

   **The measured cause is a full disk.** Book A's `phase3.log` holds four
   occurrences, and each carries the reason:

   ```
   WARNING:root:EspeakFallback not Enabled: OOD words will be skipped
   WARNING:root:{"[Errno 28] No space left on device: '…/espeakng_loader/libespeak-ng.dylib'
                  -> '/var/folders/…/libespeak-ng.dylib'"}
   ```

   **The fallback unpacks `libespeak-ng.dylib` into a temporary directory, and a full disk
   stops it.** So a full disk does not only kill a render. It can also silently disable the
   fallback and delete every out-of-lexicon word from whatever still renders. That joins
   the two hazards of this section into one.

   **When the warning is present, check whether any chunk rendered after it.** For Book A
   the answer was none: the four warnings belong to `ch02` to `ch05`, each of
   which reports `done=0`, and `ch01`'s 159 chunks were rendered before the first warning.
   No delivered audio was affected. Book B's own logs hold zero occurrences.

### 17.2 Warning: the log grep does not work on `kokoro_cpu`

Section 17.1 tells you to grep the render log. **That grep cannot fire on the PyTorch
`kokoro` engine.** Measured on 2026-08-16.

The torch package holds the same fault and the same message:

```python
# kokoro/pipeline.py
try:
    fallback = espeak.EspeakFallback(british=lang_code == "b")
except Exception as e:
    logger.warning("EspeakFallback not Enabled: OOD words will be skipped")
    fallback = None
self.g2p = en.G2P(trf=trf, british=..., fallback=fallback, unk="")
```

The message is byte-identical to the one section 17.1 greps for. **But `kokoro/__init__.py`
calls `logger.disable("kokoro")` at import.** The logger is loguru, not the standard
library, and the package silences its own diagnostics. The warning is therefore never
written. The fallback can be broken, every out-of-lexicon word can be deleted from the
audio, and the log stays clean.

**A check that cannot fire is worse than no check.** It manufactures confidence.

Three rules follow.

1. **Inspect the object, do not read the log.** `preflight()` reads
   `pipeline.g2p.fallback` directly and raises when it is `None`. An object cannot be
   silenced. This is the primary check on `kokoro_cpu`, and it runs before any long render.
2. **Turn the logger back on.** A caller that wants the log channel calls
   `loguru.logger.enable("kokoro")` at start-up. Do this as well as rule 1, never instead
   of it.
3. **Keep the grep of 17.1.** It is a secondary check. It still catches the `mlx-audio`
   path and any future engine that uses the standard library logger.

**Probe with a word, not only with an attribute.** `unk=""` deletes an unresolved word even
when the fallback works. `preflight()` therefore also renders a word that the lexicon does
not hold, and it measures the RMS of the result. Measured with misaki: `Zyrkovian
Quaddlemorph` gives `zˌɪəkˈQviən kwˈɒddᵊlmˌɔːf` with a working fallback, and gives one
space — both words gone — with `fallback=None` and `unk=""`.

---

## 18. Homographs — the heteronym audit and the forced reading

A **heteronym** is a word with one spelling and two pronunciations: `wound`, `read`,
`live`, `minute`, `wind`, `bow`, `lead`, `row`. The engine picks a reading from a POS
tag. The engine sometimes picks the wrong one.

**Warning: QC cannot find this fault.** The QC matcher compares words by sound and drops
every vowel that is not the first one. Whisper writes `wound` for both readings. QC is
blind to the vowel, so the gate stays green while the audio says the wrong word. Refer to
section 17 and to `NOTES.md`.

The homograph audit is the answer. It reads text only. It never listens to audio.

### 18.1 The mechanism

misaki accepts inline per-occurrence phoneme markup: `[wound](/wˈWnd/)`. The regular
expression is `LINK_REGEX` in `misaki/en.py`. The markup sets the token's phonemes
directly, before any lexicon lookup. No bracket and no slash reaches the phoneme output.
The engine needs no change.

### 18.2 The inventory

`abpipe/data/heteronyms.json` holds the inventory. `abpipe/data/build_heteronyms.py`
generates the file from misaki's own `gb_gold.json` and `us_gold.json` multi-reading
entries, then applies a hand-curated overlay.

misaki's lexicon is the decision table the engine really uses, so an inventory made from
it is complete for what the engine can get wrong. The inventory also carries the exact
phoneme strings for both dialects: `gb` for `lang_code` `b`, `us` for `lang_code` `a`.

Each entry holds a severity class:

| Class | Meaning | Example | Forced? |
|---|---|---|---|
| A | The vowel differs. A wrong choice is a howler. | wound, read, live, lead | **Yes** |
| B | The voicing or the final consonant differs. | house, use, close, mouth | No. Reported. |
| C | The stress differs only. | record, present, suspect | No. Reported. |

`missing_from_misaki: true` marks a word that misaki holds with ONE reading only, such as
`lead` and `row`. No POS tag can make misaki right for these words. **Every occurrence of
such a word needs a decision.**

### 18.3 The three tiers

1. **Tier 1 — the transformer tagger.** Tag the chunk with `en_core_web_trf`. misaki uses
   `en_core_web_sm`. When both taggers give the same reading, the occurrence passes.
2. **Tier 2 — the cue rules.** Keywords from the inventory resolve a tier 1 disagreement,
   and resolve a pair that no POS tag can separate, such as `bass` and `sow`.
3. **Tier 3 — the LLM, or a person.** One batched `claude` CLI call carries every
   occurrence that is still open. Any fault — a bad exit, bad JSON, a timeout, or a
   missing binary — writes the occurrence to `work/<slug>/homograph-review.json` for a
   person to answer. **The audit never guesses in silence.**

### 18.4 work/&lt;slug&gt;/homographs.json

The decisions are a run artifact, beside `qc-config.json` and `qc-accept.json`. They are
**not** part of the book config.

```json
{
  "schema": 1,
  "decisions": [
    {"chapter": "ch01", "chunk": "0059", "word": "wound", "occurrence": 1,
     "phonemes": "wˈWnd", "reading": "verb", "decided_by": "tier2:cue:round",
     "misaki_baseline": "wˈuːnd", "class": "A",
     "context": "a white muffler wound round and round his neck",
     "human": false}
  ]
}
```

`occurrence` counts the whole-word, case-insensitive matches of `word` in that chunk, and
it starts at 1. `human: true` marks a decision a person wrote. **The audit regenerates its
own decisions, and it never overwrites a `human: true` decision.**

### 18.5 The markup rule at render

Stage 4 applies the markup to an **in-memory copy only**, exactly like the pronunciation
map. The chunk file on disk always keeps the author's spelling. QC compares against the
disk text, so QC never sees the markup.

**The order is fixed. The markup goes in first, then the pronunciation map.** The markup
offsets are indexed against the on-disk text. A pronunciation entry would otherwise match
inside the bracketed word and corrupt it. `homographs.validate()` refuses a book that
names one word in both tables.

The remediation ladder in stage 5 applies the same markup, in the same order, for the same
reason it applies the pronunciation map: a re-rendered chunk must not lose the correction
stage 4 gave it.

### 18.6 The input hash rule — per chunk, not global

Stage 4's `input_hash` for one chunk is:

```python
input_hash = chunk_input_hash(record["sha256"], decisions_for_this_chunk)
```

**An empty decision list returns the bare `record["sha256"]`.** A book with no decisions
therefore hashes exactly as it did before this section existed, and every rendered WAV
stays fresh.

Only the fields that change the audio go into the hash: `word`, `occurrence`, and
`phonemes`. A change to `decided_by` or to `context` does not stale a WAV.

The pronunciation map stays in the **config** hash, because it applies to the whole book.
Homograph decisions go into the **input** hash of one chunk, because they apply to one
occurrence. A new decision therefore stales exactly the chunks it touches. The global
alternative was refused: it makes every late fix a 2,000-chunk event.

**`abpipe status` must use the same rule.** `_status_render` calls
`render.render_input_hash()`, and does not reproduce the formula. Refer to the warning in
section 14.

### 18.7 The command

```
abpipe homographs [--chapter ID …] [--write] [--no-llm]
```

It reports by default. `--write` persists the decisions. `--no-llm` sends every open
occurrence to the review file instead of to the `claude` CLI.

The command **exits non-zero when an unresolved class A disagreement exists**. That is the
same gate shape as QC.

`python -m abpipe.homographs` runs the same audit without the CLI.

### 18.8 T-2.5, a new triage step

Section 17's triage gains one step between T-2 and T-3:

**T-2.5 The homograph audit.** Run `abpipe homographs` after the chunk stage and before
the full render. Resolve every class A disagreement. Review the class B and C report.
Only then start T-3.

The audit works on a delivered book too, but be exact about why.

**The acoustic model is NOT deterministic.** Two renders of the same text give different
bytes. Refer to section 9.3.

**The phonemizer IS deterministic**, and the phonemizer is what this audit reads. The
front end turns the text into one phoneme string, and the acoustic model is given only
that string. So the audit reproduces **which word the engine was told to say**, which is
the only thing a heteronym decision controls. It does not reproduce the waveform, and it
does not need to.

A wrong reading is therefore a text-level fault, and the audit finds it in a delivered
book without listening.
