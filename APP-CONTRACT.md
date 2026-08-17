# Narratarr app contract

This document defines the database schema, the module interfaces, and the `/api/v1`
specification of Narratarr.

**Warning: this document is the single source of truth.** If a program and this document do
not agree, this document is correct. Correct the program. Do not change this document to
match a program.

**No worker edits this document.** Only the overlord edits this document.

Workers meet at this document and nowhere else. A worker that needs a change asks the
overlord.

---

## 1. What Narratarr is

Narratarr is a servarr-style companion application. Narratarr reads an ebook. Narratarr
writes one m4b audiobook. Narratarr sends the audiobook to one or more targets.

Narratarr wraps the `abpipe` pipeline as a library. `abpipe` does the work of the eight
stages. Refer to `vendor/abpipe/CONTRACT.md`.

### 1.1 The acquisition-free principle

**Narratarr never acquires a book. Narratarr consumes a book that a person gives it.**

Narratarr holds no indexer, no tracker, no download client, no torrent code, and no NZB
code. Narratarr sends no search to any external index. This rule has no exception, and no
future version relaxes it.

Bazarr is the correct model. Bazarr improves media that a person already holds. Narratarr
does the same for an ebook that a person already holds.

**A worker that adds an acquisition feature has broken the product.** This rule is what
keeps the repository publishable.

### 1.2 What v1 promises

Narratarr is a **batch appliance**. One book renders at a time. The target machine is a
4-core Intel i5-6500T. A book takes between one night and one day.

**Never describe Narratarr as fast.** Honesty about the speed is a product requirement.
Refer to section 12 for the measured numbers.

---

## 2. Directory layout

```
narratarr/
  APP-CONTRACT.md           this document
  PROGRESS.md               the run status
  README.md                 how to run Narratarr
  LICENSE                   GPL-3.0
  .env.example              every environment variable, with a safe example value
  docker-compose.yml        the deployment
  Dockerfile                the image
  pyproject.toml            the package definition
  vendor/abpipe/            the vendored pipeline. Refer to section 3.
  narratarr/                the application package
    __init__.py
    config.py               the settings object
    db.py                   the connection, the migrations
    schema.sql              the schema of section 4
    models.py               the row types
    queue.py                the job queue
    runner.py               the job runner
    api/                    the HTTP layer
      __init__.py           the FastAPI application factory
      auth.py               the API key check
      jobs.py  review.py  targets.py  settings.py  system.py
    adapter/
      __init__.py           the pipeline adapter of section 6
      ingest.py             the watch folder
      targets/
        __init__.py  base.py  folder.py  audiobookshelf.py
  web/                      the React and Vite single-page application
  tests/                    the tests
  docs/                     the API documentation and the screenshots
```

### 2.1 The runtime layout

Narratarr writes nothing inside the image. Narratarr writes to two mounted directories.

```
/config/                    the state. One Docker volume or one bind mount.
  narratarr.db              the sqlite database
  models/                   the downloaded models. Refer to section 11.
  library/                  the ingested ebook files
  work/<slug>/              the abpipe artifacts of one book. Refer to abpipe CONTRACT 2.
  logs/
/output/                    the finished audiobooks. The folder target writes here.
/watch/                     the watch folder. Refer to section 7.
```

**A path is never hard-coded.** Every path above comes from the settings object of
`narratarr/config.py`, and every setting has an environment variable. Refer to section 10.

---

## 3. The vendored pipeline

`vendor/abpipe/` holds a plain copy of the `abpipe` package at one recorded commit.
`vendor/abpipe/UPSTREAM.txt` records the commit.

Three rules govern it.

1. **The copy is one-way.** `~/work/tts-audiobook` is canonical. A change to the pipeline
   happens there first, and the copy is made again.
2. **No worker edits a file under `vendor/abpipe/`.** A worker that needs a pipeline change
   asks the overlord. The overlord makes the change upstream.
3. **The history is fresh.** This repository holds none of the upstream git history,
   because that history holds copyrighted EPUB files. `git log` starts at this repository's
   first commit.

**The adapter is the only code that imports `abpipe`.** Refer to section 6. No module under
`narratarr/api/` imports `abpipe`. This rule keeps the API layer testable without the
pipeline, and it keeps one seam to maintain when the pipeline changes.

### 3.1 Narratarr does not use `abpipe.deliver`

`abpipe/deliver.py` is the upstream author's own delivery stage. It holds a server address, and it reads
the Audiobookshelf database over SSH.

**Narratarr never calls `abpipe.deliver`, and Narratarr never reads `absdatabase.sqlite`.**
Narratarr implements its own targets. Refer to section 8. Narratarr uses the Audiobookshelf
HTTP API with a token that a person creates in the Audiobookshelf user interface.

Narratarr therefore runs the pipeline through stage 7 (bind) only. Stage 8 is Narratarr's
own target layer.

---

## 4. The database

sqlite, at `/config/narratarr.db`. **WAL mode is mandatory**, with
`PRAGMA journal_mode=WAL` and `PRAGMA foreign_keys=ON` on every connection. The runner
writes while the API reads, and WAL is what lets both happen.

`narratarr/schema.sql` holds the statements below. `narratarr/db.py` applies them.

**The schema version lives in the `meta` table**, under the key `schema_version`. The
current version is `1`. A migration adds a numbered step. A migration never drops a column
that holds delivered data.

### 4.1 meta

```sql
CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
```

### 4.2 jobs

```sql
CREATE TABLE jobs (
  id             TEXT PRIMARY KEY,             -- uuid4 hex
  slug           TEXT NOT NULL UNIQUE,         -- lower case, hyphens only
  title          TEXT,
  author         TEXT,
  year           TEXT,
  genre          TEXT,
  language       TEXT NOT NULL DEFAULT 'en',
  source_path    TEXT NOT NULL,                -- the ebook, under /config/library
  source_sha256  TEXT NOT NULL,
  cover_path     TEXT,
  state          TEXT NOT NULL,                -- refer to section 5
  stage          TEXT,                         -- refer to section 5.1
  worker         TEXT NOT NULL DEFAULT 'local',
  priority       INTEGER NOT NULL DEFAULT 0,   -- a higher number runs first
  progress_done  INTEGER NOT NULL DEFAULT 0,
  progress_total INTEGER NOT NULL DEFAULT 0,
  error          TEXT,
  book_config    TEXT NOT NULL DEFAULT '{}',   -- JSON. The abpipe book config.
  qc_config      TEXT NOT NULL DEFAULT '{}',   -- JSON. The abpipe qc-config.
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  started_at     TEXT,
  finished_at    TEXT
);
CREATE INDEX idx_jobs_state ON jobs(state);
CREATE INDEX idx_jobs_priority ON jobs(priority DESC, created_at ASC);
```

| Field | Rule |
|---|---|
| `slug` | The name of the work directory. The API derives it from the title. A collision gets a numeric suffix. |
| `source_sha256` | The hash of the ebook file. Two jobs with the same hash are a duplicate; the API refuses the second one unless the caller sets `allow_duplicate`. |
| `worker` | The machine that renders this job. v1 always writes `local`. The column exists so that the v2 Mac render agent needs no migration. **Do not remove it.** |
| `book_config` | The `abpipe` book config of section 4.1 of the pipeline contract. The configuration editor writes it. |
| `progress_total` | `0` means unknown. The user interface then shows an indeterminate bar, never a false percentage. |

**Every timestamp in this database is a UTC stamp in the form `YYYYMMDDThhmmssZ`**, the same
form the pipeline uses.

### 4.3 gates

A gate is a point where the run stops and waits for a person.

```sql
CREATE TABLE gates (
  id          TEXT PRIMARY KEY,
  job_id      TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  kind        TEXT NOT NULL,      -- sample | homograph | qc
  state       TEXT NOT NULL,      -- open | resolved | voided
  payload     TEXT NOT NULL DEFAULT '{}',   -- JSON. Refer to section 9.
  open_items  INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL,
  resolved_at TEXT,
  resolved_by TEXT,
  resolution  TEXT,               -- approved | rejected | rerender | edited
  reason      TEXT
);
CREATE INDEX idx_gates_open ON gates(state, job_id);
```

A gate becomes `voided` when the job re-renders the work the gate examined. A voided gate
keeps its row, because the record that a person looked once has value.

### 4.4 review_items

One row for each thing a person must answer inside a gate.

```sql
CREATE TABLE review_items (
  id           TEXT PRIMARY KEY,
  job_id       TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  gate_id      TEXT NOT NULL REFERENCES gates(id) ON DELETE CASCADE,
  kind         TEXT NOT NULL,     -- qc_chunk | homograph_occurrence
  chapter      TEXT NOT NULL,
  chunk        TEXT,
  word         TEXT,              -- homograph only
  occurrence   INTEGER,           -- homograph only. Counts from 1.
  source_text  TEXT,
  transcript   TEXT,
  context      TEXT,
  wer          REAL,
  coverage     REAL,
  duration_s   REAL,
  flags        TEXT NOT NULL DEFAULT '[]',  -- JSON list
  wav_sha256   TEXT,              -- the pin. Refer to section 9.3.
  candidates   TEXT,              -- JSON. homograph only. Refer to section 9.2.
  state        TEXT NOT NULL,     -- open | accepted | rerendered | resolved | voided
  resolution   TEXT,
  reason       TEXT,
  resolved_at  TEXT,
  created_at   TEXT NOT NULL
);
CREATE INDEX idx_review_open ON review_items(state, job_id);
CREATE UNIQUE INDEX idx_review_identity
  ON review_items(job_id, kind, chapter, chunk, word, occurrence);
```

**`reason` is mandatory on an acceptance.** Refer to section 9.3.

### 4.5 events

The job log. The user interface streams it.

```sql
CREATE TABLE events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id     TEXT REFERENCES jobs(id) ON DELETE CASCADE,
  level      TEXT NOT NULL,       -- debug | info | warning | error
  stage      TEXT,
  message    TEXT NOT NULL,
  data       TEXT,                -- JSON
  created_at TEXT NOT NULL
);
CREATE INDEX idx_events_job ON events(job_id, id);
```

**The events table is capped.** The runner deletes the oldest rows of a job once that job
holds more than `events_per_job_max` rows, default 5000. A render of 2,000 chunks would
otherwise grow the database without a bound.

### 4.6 targets and deliveries

```sql
CREATE TABLE targets (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL UNIQUE,
  kind       TEXT NOT NULL,       -- folder | audiobookshelf
  enabled    INTEGER NOT NULL DEFAULT 1,
  config     TEXT NOT NULL DEFAULT '{}',   -- JSON. Refer to section 8.
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE deliveries (
  id           TEXT PRIMARY KEY,
  job_id       TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  target_id    TEXT NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
  state        TEXT NOT NULL,     -- pending | delivering | delivered | failed
  remote_ref   TEXT,              -- the item id at the target
  url          TEXT,
  bytes        INTEGER,
  error        TEXT,
  created_at   TEXT NOT NULL,
  delivered_at TEXT
);
CREATE UNIQUE INDEX idx_delivery_pair ON deliveries(job_id, target_id);
```

**A secret never goes into `targets.config` in plain form.** Refer to section 10.2.

### 4.7 api_keys and settings

```sql
CREATE TABLE api_keys (
  id           TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  key_sha256   TEXT NOT NULL UNIQUE,
  created_at   TEXT NOT NULL,
  last_used_at TEXT
);

CREATE TABLE settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,       -- JSON
  updated_at TEXT NOT NULL
);
```

**The database holds only the SHA-256 of an API key. The database never holds the key.**
The API shows the key once, at the moment it makes the key, and never again.

---

## 5. The job state machine

```
                    +-----------------------------+
                    v                             |
 queued -> running -+-> awaiting_sample_approval --+
                    |
                    +-> awaiting_homograph_review -+
                    |                              |
                    +-> awaiting_qc_review --------+
                    |
                    +-> delivering -> done
                    |
                    +-> failed
 (any state) -> cancelled
 (any state) -> paused -> queued
```

The closed set of `state`:

| State | Meaning |
|---|---|
| `queued` | The job waits for the runner. |
| `running` | The runner works on the job. `stage` names the stage. |
| `awaiting_sample_approval` | The sample renders and waits for a person. Gate kind `sample`. |
| `awaiting_homograph_review` | An unresolved class A homograph waits. Gate kind `homograph`. |
| `awaiting_qc_review` | One or more chunks hold `needs_human`. Gate kind `qc`. |
| `delivering` | The targets run. |
| `done` | Every enabled target reports `delivered`. |
| `failed` | The run stopped on a fault. `error` holds the message. |
| `cancelled` | A person stopped the job. |
| `paused` | A person paused the job. The artifacts stay on disk. |

**The three gate states are first-class states, not a flag.** A job list must show at a
glance which books wait for a person. That is the whole product idiom.

### 5.1 The stage values

`stage` holds one of `extract`, `normalize`, `chunk`, `sample`, `homographs`, `render`,
`qc`, `assemble`, `bind`, `deliver`, or `null`.

The runner walks them in that order. `sample` and `homographs` are Narratarr's own steps,
and they map onto triage steps T-2 and T-2.5 of the pipeline contract.

### 5.2 The rules the runner obeys

1. **One book at a time.** The runner holds one in-process worker. A second job waits.
2. **Every stage is idempotent, so a kill is safe.** Refer to pipeline contract section 3.
   On start the runner sets every `running` job back to `queued`, and the job resumes at
   the first missing or stale artifact.
3. **A gate stops the runner and frees it.** The runner picks up the next job while a book
   waits for a person. The gated book resumes when a person resolves the gate.
4. **Prune only when the job is `done` AND its review queue is empty.** Refer to pipeline
   contract section 15.1. Pruning turns a one-chunk fix into a whole-chapter re-render, and
   that mistake is already recorded upstream. Pruning is off by default.
5. **The runner writes an event for every state change.**

---

## 6. The pipeline adapter

`narratarr/adapter/__init__.py`. Owner: W2.

**This module is the only place that imports `abpipe`.**

```python
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

class Pipeline:
    """One book. One work directory. The adapter over abpipe."""

    def __init__(self, workspace: Path, slug: str, source: Path,
                 book_config: dict, qc_config: dict) -> None: ...

    def run_stage(self, stage: str, chapters: list[str] | None = None,
                  force: bool = False,
                  progress: Callable[[Progress], None] | None = None) -> StageResult: ...

    def status(self) -> dict:
        """Return the fresh, stale, and absent count of each stage."""

    def render_sample(self, chapter: str | None = None,
                      seconds: float = 90.0) -> Path:
        """Render a hazard passage. Return the WAV path. Triage step T-2."""

    def homograph_audit(self, write: bool = False, llm: bool = True) -> dict:
        """Run the audit. Return the decisions and the open occurrences."""

    def homograph_candidates(self, chapter: str, chunk: str, word: str,
                             occurrence: int) -> list[dict]:
        """Render BOTH readings of one occurrence. Refer to section 9.2."""

    def qc_report(self) -> dict: ...

    def accept_chunk(self, chapter: str, chunk: str, reason: str) -> None:
        """Write qc-accept.json. The reason is mandatory."""

    def rerender_chunk(self, chapter: str, chunk: str) -> dict: ...

    def artifacts(self) -> dict:
        """Return the paths of book.json, the cover, and the m4b."""

    def chunk_audio_path(self, chapter: str, chunk: str) -> Path | None: ...

    def prune_chapters(self, chapters: list[str] | None = None,
                       dry_run: bool = False) -> dict:
        """Remove the intermediate audio of finished chapters. Refer to 5.2 rule 4."""
```

### 6.1 `preflight_engine()` — the load-bearing safety seam

```python
def preflight_engine(engine: str, voice: str, lang_code: str) -> dict:
    """Load the engine, check the espeak fallback, render a warmup, and report."""
```

It returns the engine's own `preflight()` report, unchanged:

```json
{"espeak_fallback": true, "warmup_samples": 64200, "warmup_sample_rate": 24000,
 "oov_probe_word": "Zyrkovian Quaddlemorph", "oov_probe_nonempty": true}
```

**Three workers depend on this shape**: the adapter returns it, the runner gates on it, and
the image build fails on it. It is written here so that no copy of it drifts.

**The adapter never reshapes the report.** It passes it through. Every failure — a missing
`preflight` attribute, an unknown engine, a missing dependency, a genuinely broken
fallback — becomes `PipelineError`, so one `except` clause in the runner covers all of them.

**Why this exists.** When the espeak fallback fails to construct, misaki is built with
`unk=""` and **every out-of-lexicon word is deleted from the audio, silently.** QC cannot
see the loss. The log cannot show it either, because the `kokoro` package disables its own
logger. Reading the object is the only check that cannot be silenced. Refer to
`vendor/abpipe/CONTRACT.md` section 17.2.

**Where the thread cap lives.** `preflight_engine()` takes no thread count on purpose. A
thread count must never enter `engine.describe()`, because that dict is hashed into stage
4's `config_hash`, and a speed setting must not stale a rendered file. The runner instead
calls `torch.set_num_threads(settings.num_threads)` **once, at start-up**, before the first
preflight. The call is process-global, so once is enough. Refer to section 11.3: the
container is capped at 3 CPUs, and torch would otherwise start one thread per core and
fight the cgroup.

### 6.2 The model fetcher

```python
scripts.fetch_models.fetch_all(progress: Callable[[str, int, int], None] | None = None) -> None
```

It raises `ModelFetchError`. `POST /api/v1/system/models/fetch` starts it on a background
thread and returns `202`, like every other route that does work later.

Rules:

1. **The adapter converts, it does not decide.** Every policy lives in `abpipe` or in the
   runner. The adapter turns an `abpipe` summary dict into a `StageResult`.
2. **The adapter never writes outside `workspace`.**
3. **The adapter raises `PipelineError`** on a fault. It never returns a partial result and
   calls it success. The upstream contract's own rule applies: a stage that produces
   nothing and reports success is worse than a crash.
4. **The adapter honours the engine ordering rule.** Homograph markup goes in first, then
   the pronunciation map. `abpipe/render.py` already does this. The adapter must not
   duplicate it. Refer to pipeline contract 18.5.
5. **A test never loads a model.** A test passes a fake pipeline object to the runner.

---

## 7. Ingest

`narratarr/adapter/ingest.py`. Owner: W2.

Narratarr accepts a book in three ways:

1. **The watch folder.** Narratarr polls `/watch` every `watch_interval_s` seconds,
   default 60. A new `.epub` file makes a job in the state `queued`.
2. **An upload**, through `POST /api/v1/jobs`.
3. **A path**, through `POST /api/v1/jobs` with `source_path`.

A fourth way arrives in v2. Refer to section 16.

Rules:

- **Narratarr copies the file into `/config/library/` and never renders from `/watch`.**
  The copy is what makes the source stable while the render runs.
- **Narratarr waits for the write to finish.** A file whose size changes between two polls
  is still being copied. Narratarr ingests it only when the size is the same twice.
- **Narratarr moves nothing in `/watch` and deletes nothing in `/watch`** unless
  `watch_delete_after_ingest` is set, default false. The watch folder belongs to the user.
- **A DRM-protected file fails at ingest with a clear message.** `abpipe/extract.py`
  already refuses one. Narratarr never circumvents DRM. Refer to pipeline contract 5.4.
- The supported input is EPUB. An unsupported extension makes a `failed` job with a
  message that names the extension, never a silent skip.

---

## 8. The targets

`narratarr/adapter/targets/`. Owner: W2.

```python
@dataclass(frozen=True)
class DeliverBook:
    slug: str
    title: str
    author: str
    year: str | None
    genre: str | None
    m4b: Path
    cover: Path | None
    duration_s: float
    chapters: int

@dataclass(frozen=True)
class DeliveryResult:
    ok: bool
    remote_ref: str | None = None
    url: str | None = None
    bytes: int = 0
    message: str = ""

class Target(Protocol):
    kind: str

    def validate(self, config: dict) -> None:
        """Raise ValueError when the configuration is wrong. Never touch the network."""

    def test(self, config: dict) -> DeliveryResult:
        """Check that the target is reachable. Write nothing."""

    def deliver(self, config: dict, book: DeliverBook,
                progress: Callable[[Progress], None] | None = None) -> DeliveryResult: ...

    def deliver_fix(self, config: dict, book: DeliverBook,
                    progress: Callable[[Progress], None] | None = None) -> DeliveryResult:
        """Re-deliver after a post-delivery correction. Refer to section 9.5."""
```

**Every target is idempotent.** A second delivery of the same book copies nothing new and
verifies again.

### 8.3 `deliver_job()` — one book, every target

The `Target` protocol above covers one target. The runner needs one call that covers a
whole job, so `narratarr/adapter/targets/__init__.py` holds:

```python
def deliver_job(job, *, progress=None) -> list[DeliveryResult]:
    """Deliver one finished book to every enabled target. Return one result per target."""

def deliver_job_fix(job, *, progress=None) -> list[DeliveryResult]:
    """Re-deliver after a post-delivery correction. Refer to section 9.5."""
```

Rules:

1. It reads every `targets` row where `enabled` is 1.
2. **One target that fails never stops another.** It catches per target, records the
   failure, and returns a result for every target. A folder that is full must not stop an
   Audiobookshelf delivery that would have worked.
3. It upserts the `deliveries` row for each `(job_id, target_id)` pair. That pair carries a
   unique index.
4. **No token reaches a `deliveries` row, an event, or a returned result.** Refer to 10.2.

### 8.1 The folder target

`kind: "folder"`. The configuration:

```json
{ "root": "/output", "layout": "{author}/{title}/{title}.m4b", "copy_cover": true }
```

The folder target writes the m4b and the cover. It serves Audiobookshelf, Plex, and any
other reader that watches a directory. **This is the default target, and it is the target
that makes Narratarr useful to a stranger.**

The target refuses a `layout` that escapes `root`, so a `..` in a title cannot write
outside the tree.

### 8.2 The Audiobookshelf target

`kind: "audiobookshelf"`. The configuration:

```json
{ "base_url": "http://audiobookshelf:13378",
  "library_id": "…",
  "token_env": "NARRATARR_ABS_TOKEN",
  "folder_target": { "root": "/output", "layout": "{author}/{title}/{title}.m4b",
                     "copy_cover": true } }
```

**`folder_target` is an embedded folder-target configuration, not the name of another
target.** The Audiobookshelf target copies the file itself, with the same code the folder
target uses, and then asks the server to scan. An embedded object keeps the target
self-contained: deleting a folder target cannot then break an Audiobookshelf target that
pointed at it by name.

Rules:

1. **A person creates the token in the Audiobookshelf user interface.** Narratarr never
   reads `absdatabase.sqlite`, and Narratarr never asks for a password.
2. **The token comes from an environment variable, named by `token_env`.** The token never
   enters the database and never enters a log. Refer to section 10.2.
3. The target copies the book with the folder target first, then calls
   `POST /api/libraries/<id>/scan`, then polls the items API.
4. **The poll paginates through every page.** A fixed page size silently breaks the check
   once a library outgrows it. This fault is measured and recorded upstream; do not repeat
   it. Refer to pipeline contract section 12 step 4.
5. The poll stops on an empty page, on a repeated page, or at a hard page cap, so a
   malformed server cannot spin it for ever. The timeout is 300 seconds.
6. The target asserts the title, the author, the chapter count, and the duration. The
   duration is correct within 5 percent.

---

## 9. The human loop

Owner: W3 for the API, W4 for the user interface.

The idiom is Radarr's manual import: show the evidence, offer a small set of actions, and
record why.

### 9.1 The sample gate

Triage step T-2. After the chunk stage the runner renders a passage of about 90 seconds and
opens a gate of kind `sample`. **The passage is chosen for the hazards, not for the
prose**: the worst proper noun, a foreign term, a number, and a caps run.

The user interface plays the sample and offers `approve`, `reject`, or `edit config`. An
edit re-renders the sample.

**A sample gate holds no review item.** The sample is one audio file for the whole book,
not a list of chunks to answer. The audio therefore hangs off the gate, at
`GET /gates/{id}/audio`. The other two gate kinds carry review items; this one does not.

The payload of a `sample` gate:

```json
{"wav_path": "/config/work/<slug>/review/sample.wav", "chapter": "ch01"}
```

**The key is `wav_path`.** The route and the runner were written by different hands and an
earlier draft of this document named neither, so the two spelled it differently and the
audio returned 404 on a file that existed. Name every payload key here.

**The sample gate is ON by default.** A person who wants an unattended run turns it off in
the settings.

### 9.2 The homograph gate

Triage step T-2.5. The audit runs after the chunk stage and before the full render. The
audit reads text only. It never listens to audio.

**An unresolved class A disagreement opens the gate.** Class B and class C are reported and
never gate. Refer to pipeline contract section 18.

`review_items.candidates` holds both readings:

```json
[{"reading": "verb", "phonemes": "wˈWnd", "audio": "…/cand-1.wav"},
 {"reading": "noun", "phonemes": "wˈuːnd", "audio": "…/cand-2.wav"}]
```

**The user interface plays BOTH candidate renderings.** A person cannot choose a
pronunciation from a phoneme string. This is the whole reason the gate exists.

A resolution writes a decision with `human: true` into `work/<slug>/homographs.json`. **The
audit never overwrites a `human: true` decision.**

### 9.3 The QC gate

A chunk that reaches `needs_human` opens a gate of kind `qc`, with one review item for each
chunk. The user interface shows, for each item:

- the source text and the transcript, as a word-level diff;
- `wer`, `coverage`, `duration_s`, and the flags;
- an audio player for the rendered chunk.

The actions are `accept`, `rerender`, and `edit config`.

**`accept` requires a written reason. The API rejects an empty reason with 422.** The
reason is not decoration. An acceptance says that a person judged the audio correct, and a
later reader needs to know why.

**The acceptance is pinned to `wav_sha256`.** Refer to pipeline contract 9.7.

**Warning: every pin voids on every re-render, always.** Kokoro is not deterministic, so a
re-render changes the bytes even when the words, the length, and the reading are the same.
A voided pin is not evidence that the audio changed. It means only that a person judged
this text's audio acceptable once. The user interface must say this where a person sees a
voided item, or the user interface teaches a false belief.

### 9.4 Gate resolution and the runner

A gate resolution is one API call. The API writes the resolution, sets the gate to
`resolved`, and returns the job to `queued`. The runner then resumes the job.

**A gate resolution does not answer the gate's review items.** The two are separate on
purpose. A person answers each item, one at a time, with its own reason. Resolving the gate
says only that the person has finished with the queue. **The runner refuses to leave a
`qc` or `homograph` gate while that gate still holds an item in the state `open`**, and it
returns `409` to a resolve call that arrives too early. A cascade would silently accept
every open item with no reason, and a reason is mandatory. Refer to 9.3.

**The API never runs the pipeline.** The API writes state and returns. The runner does the
work. A request must never block on a render.

### 9.5 The Fix flow

A person finds a fault after delivery. The Fix flow corrects seconds of audio and delivers
again, in minutes.

```
POST /api/v1/jobs/{id}/fix
{ "items": [{"chapter": "ch07", "chunk": "0130",
             "action": "rerender" | "pronunciation" | "homograph",
             "value": {...}, "reason": "…"}] }
```

The steps:

1. Apply the correction. A pronunciation entry changes `book_config`. A homograph decision
   changes `homographs.json`.
2. Re-render **only the stale chunks**. The per-chunk input hash rule of pipeline contract
   18.6 is what makes this small: a new homograph decision stales exactly the chunks it
   touches.
3. Re-assemble only the affected chapters, then bind again.
4. Deliver again to every target that already holds the book.

**Warning: a pruned chapter defeats this flow.** Pruning removes the chunk WAV files, so a
one-chunk fix costs a whole chapter. This is why rule 4 of section 5.2 exists.

---

## 10. Configuration and secrets

`narratarr/config.py`. Owner: W1.

Every setting has an environment variable with the prefix `NARRATARR_`. `.env.example`
lists every one of them with a safe example value.

| Variable | Default | Meaning |
|---|---|---|
| `NARRATARR_CONFIG_DIR` | `/config` | The state directory. |
| `NARRATARR_OUTPUT_DIR` | `/output` | The default folder target root. |
| `NARRATARR_WATCH_DIR` | `/watch` | The watch folder. |
| `NARRATARR_PORT` | `8000` | The listen port inside the container. |
| `NARRATARR_LOG_LEVEL` | `info` | |
| `NARRATARR_API_KEY` | *(none)* | A bootstrap key. Narratarr makes one on first run when this is empty, and prints it once. |
| `NARRATARR_ENGINE` | `kokoro_cpu` | The TTS engine. |
| `NARRATARR_VOICE` | `bm_george` | |
| `NARRATARR_LANG_CODE` | `b` | |
| `NARRATARR_NUM_THREADS` | `3` | The torch thread count. It matches the CPU cap. |
| `NARRATARR_WHISPER_BACKEND` | `faster` | |
| `NARRATARR_WHISPER_MODEL` | *(set by P0)* | Refer to section 12. |
| `NARRATARR_SAMPLE_GATE` | `true` | The sample gate of section 9.1. |
| `NARRATARR_WATCH_INTERVAL_S` | `60` | How often Narratarr polls the watch folder. |
| `NARRATARR_WATCH_DELETE_AFTER_INGEST` | `false` | Refer to section 7. The watch folder belongs to the user. |
| `NARRATARR_EVENTS_PER_JOB_MAX` | `5000` | The events cap of section 4.5. |
| `NARRATARR_PRUNE` | `false` | Refer to rule 4 of section 5.2. |
| `NARRATARR_ABS_TOKEN` | *(none)* | The Audiobookshelf token. Refer to section 8.2. |
| `HF_HUB_DISABLE_XET` | `1` | **Required.** Refer to section 11.1. |

### 10.1 The API key

Every `/api/v1` route needs the header `X-Api-Key`, with two exceptions:
`GET /api/v1/system/health` and the static files of the single-page application.

**The key never goes into a URL.** A URL enters a log, a browser history, and a referrer
header. The user interface therefore fetches audio with `fetch()` and the header, then
makes a blob URL for the `<audio>` element. **A worker must not add a `?apikey=` fallback.**

The check is a constant-time compare of the SHA-256 of the presented key.

### 10.2 Secrets

1. **No secret is ever committed.** `.env` is in `.gitignore`. `.env.example` holds only
   example values.
2. **A secret is read from the environment at the moment of use.** A secret never enters
   the database, a log line, an event row, or an API response.
3. **The API redacts a secret in every response.** A target configuration returns
   `"token_env": "NARRATARR_ABS_TOKEN"` and never the token itself.
4. `GET /api/v1/system/status` reports whether each named secret is **present**, and never
   its value.

---

## 11. Packaging

`Dockerfile`, `docker-compose.yml`. Owner: W5.

### 11.1 The slim image

The image is slim, about 2.5 GB. The image holds:

- Python, FastAPI, torch (CPU wheels only — **never the CUDA wheels**), `kokoro`,
  `misaki[en]`, `faster-whisper`, `spacy` with `en_core_web_sm`, `ffmpeg`, and `espeak-ng`;
- the built single-page application;
- the vendored `abpipe`.

**The image does NOT hold the TTS and whisper weights.** Narratarr downloads them on first
run into `/config/models`, checks a recorded checksum, and refuses to start a render when
the disk has too little space.

**`HF_HUB_DISABLE_XET=1` is set in the image.** The HuggingFace xet transport fails on at
least one machine in this project, and the failure is confusing. The variable is a default,
not a workaround a user must find.

### 11.2 The build-time espeak warmup

**Warning: this step prevents a silent data-loss fault, and it is not optional.**

When `EspeakFallback` fails to construct, misaki is built with `unk=""` and **every
out-of-lexicon word is deleted from the audio, silently**. QC cannot see the loss, because
the transcript and the source lose the same word. The measured cause is a full disk: the
fallback unpacks `libespeak-ng` into a temporary directory. Refer to pipeline contract 17.1.

Two defences, both required:

1. **The build renders a short passage that holds an out-of-lexicon word.** The build
   fails when the fallback did not construct, and the build fails when the probe produced
   near-silence. The unpacked library then lives in the image, so no run needs to unpack it
   on a full disk.
2. **Every run greps its log for `EspeakFallback not Enabled`** and fails the job when the
   warning is present. The runner does this. One line at construction is the whole
   detection surface.

### 11.3 The compose service

```yaml
services:
  narratarr:
    container_name: narratarr
    restart: unless-stopped
    ports: ["5164:8000"]
    cpus: 3
    volumes: ["…:/config", "…:/output", "…:/watch"]
```

**`cpus: 3` on the reference server.** The machine has 4 cores and runs 53 other containers. An
uncapped render starves them.

The image builds for `linux/amd64`. The healthcheck calls `GET /api/v1/system/health`.

---

## 12. The measured performance

Measured on 2026-08-16, on `.80`: an Intel i5-6500T, 4 cores, in a throwaway container
capped at `--cpus=3 --memory=6g`, while the machine ran its usual 53 containers.

| Measurement | Value |
|---|---|
| Render rate | **22.42 characters a second** |
| Render speed against realtime | **1.43x** (it makes audio faster than the audio plays) |
| QC real-time factor, `small.en` int8 | **0.267** (about 3.7x faster than realtime) |
| Peak resident memory, render only | **2,479 MB** |
| Peak resident memory, render and QC together | **2,591 MB** |
| Engine load and preflight | 38.4 s |
| Whisper first call, model load included | 9.6 s |
| Stages 1 to 3, a whole 19-chapter book | 5.7 s |

**The whisper default is `small.en`.** The measured peak of 2,591 MB is the reason. The
machine holds 15 GiB with about 4.9 GiB available, so `small.en` leaves real headroom
beside 53 other containers. `distil-large-v3` would add about 1 GB and is the right choice
on a machine with more memory. `NARRATARR_WHISPER_MODEL` changes it in one place.

### 12.1 What that means for one book

The measured book: 19 chapters, 2,042 chunks, 390,271 characters.

| Step | Measured rate | This book |
|---|---|---|
| Render | 22.42 characters a second | **4.8 hours** |
| QC | real-time factor 0.267 | **1.9 hours** |
| Audio produced | 1.43x realtime | **6.9 hours** |

**About 7 hours for a 7-hour audiobook.** One night. Refer to section 1.2, and never round
this down in any document a user reads.

**Warning: never write an estimate into this table.** A number here is measured on the
target machine, in the capped container, or it is absent. The two rows of 12.1 that are
arithmetic on a measured rate say so.

---

## 13. The API

Base path `/api/v1`. Every response is JSON. Every error is
`{"error": {"code": "…", "message": "…", "detail": {...}}}`.

Status codes: `200` on success, `201` on create, `202` when the runner will do the work
later, `400` on a bad request, `401` on a missing or wrong key, `404` on an unknown id,
`409` on a state conflict, `422` on a failed validation, `500` on a fault.

**Every list route paginates**, with `?limit=` (default 50, maximum 200) and `?offset=`.
A list response is `{"items": [...], "total": N, "limit": N, "offset": N}`.

### 13.1 System

| Route | Action |
|---|---|
| `GET /system/health` | **No key needed.** Returns `{"status": "ok", "version": "…"}`. |
| `GET /system/status` | Refer to 13.1.1 for the exact shape. |
| `GET /system/models` | The model list, with the downloaded state and the size. |
| `POST /system/models/fetch` | Start the first-run download. Returns `202`. |

#### 13.1.1 The shape of `GET /system/status`

```json
{
  "runner_state": "idle",
  "queue_depth": 0,
  "disk_free_bytes": 14739197952,
  "models":  {"<name>": {"present": true, "size_bytes": 480000000}},
  "secrets": {"NARRATARR_ABS_TOKEN": {"present": false}},
  "engine_preflight": {
    "espeak_fallback": true, "warmup_samples": 64200, "warmup_sample_rate": 24000,
    "oov_probe_word": "Zyrkovian Quaddlemorph", "oov_probe_nonempty": true,
    "job_slug": null, "checked_at": "20260817T011731Z"
  }
}
```

**Warning: write a response shape down, or two workers will invent two of them.** This
document once described this route in one sentence of prose. The backend returned a flat
`runner_state` and two objects keyed by name. The frontend guessed a nested
`runner: {state}` and two arrays. The top bar reads this route on **every** screen, so the
guess threw a `TypeError` and blanked the whole application on every page, while every
test on both sides passed.

`models` and `secrets` are **objects keyed by name**, not arrays. `engine_preflight` is
the report of section 6.1, or `null` when no preflight has run. **A secret reports only
`present`.** Refer to 10.2.

### 13.2 Jobs

| Route | Action |
|---|---|
| `GET /jobs` | List. Filters: `?state=`, `?q=`. |
| `POST /jobs` | Make a job. Body: an upload, or `{"source_path": "…"}`. Returns `201`. |
| `GET /jobs/{id}` | One job, with its gates and its deliveries. |
| `DELETE /jobs/{id}` | Delete the job. `?purge=true` also deletes the work directory. |
| `POST /jobs/{id}/start` | `queued` to the runner. `202`. |
| `POST /jobs/{id}/pause` | `202`. |
| `POST /jobs/{id}/cancel` | `202`. |
| `POST /jobs/{id}/retry` | Clear the error and queue again. `202`. |
| `GET /jobs/{id}/config` | The book config and the QC config. |
| `PUT /jobs/{id}/config` | Replace them. `409` while the job runs. |
| `GET /jobs/{id}/events` | The log. `?since=` and `?level=`. |
| `GET /jobs/{id}/events/stream` | Server-sent events. |
| `GET /jobs/{id}/artifacts` | The paths and the sizes. |
| `GET /jobs/{id}/status` | The per-stage fresh, stale, and absent count. |
| `POST /jobs/{id}/deliver` | Deliver to every enabled target. `202`. |
| `POST /jobs/{id}/fix` | The Fix flow of section 9.5. `202`. |

### 13.3 Gates and review

| Route | Action |
|---|---|
| `GET /gates` | Every open gate, across every job. This is the review queue. |
| `GET /gates/{id}` | One gate, with its review items. |
| `GET /gates/{id}/audio` | The audio of a `sample` gate. `audio/wav`. `404` on another kind. |
| `POST /gates/{id}/resolve` | Body `{"resolution": "…", "reason": "…"}`. |
| `GET /review/items` | Filters: `?job_id=`, `?gate_id=`, `?state=`, `?kind=`. |
| `GET /review/items/{id}` | One item, with the diff. |
| `POST /review/items/{id}/accept` | Body `{"reason": "…"}`. **`422` when the reason is empty.** |
| `POST /review/items/{id}/rerender` | `202`. |
| `POST /review/items/{id}/resolve` | The homograph choice. Body `{"reading": "…"}`. |
| `GET /review/items/{id}/audio` | The rendered chunk. `audio/wav`. |
| `GET /review/items/{id}/audio/{n}` | Candidate `n` of a homograph item. |

### 13.4 Targets and settings

| Route | Action |
|---|---|
| `GET /targets` `POST /targets` | |
| `GET /targets/{id}` `PUT /targets/{id}` `DELETE /targets/{id}` | |
| `POST /targets/{id}/test` | Reachability. Writes nothing. |
| `GET /settings` `PUT /settings` | The `settings` table. Refer to 13.4.1. |
| `GET /library` | The ingested files. |
| `POST /library/scan` | Poll the watch folder now. `202`. |
| `GET /keys` `POST /keys` `DELETE /keys/{id}` | The key value returns once, on create. |

#### 13.4.1 The two kinds of setting

**These are two different things, and they never merge.**

1. **The environment settings** of section 10. They are read-only at run time. They set the
   paths, the port, and every secret. A person changes one by editing `.env` and restarting
   the container. `GET /settings` reports them, and `PUT /settings` **refuses** to write
   one.
2. **The stored settings**, in the `settings` table. They are the preferences a person
   changes while the application runs: the sample gate, the prune switch, the events cap,
   the watch interval. `PUT /settings` writes these.

`GET /settings` returns both, in two named blocks, and marks the first block read-only. A
secret in the first block reports only whether it is **present**.

**This specification is frozen.** A worker that needs a route asks the overlord.

---

## 14. The ownership map

**Two workers never hold the same file.** This table is the whole map.

| Owner | Files |
|---|---|
| Overlord | `APP-CONTRACT.md`, `PROGRESS.md`, `README.md`, `LICENSE`, `vendor/`, `pyproject.toml` |
| W1 backend-core | `narratarr/config.py`, `db.py`, `schema.sql`, `models.py`, `queue.py`, `runner.py`, `api/__init__.py`, `api/auth.py`, `api/common.py`, `api/system.py`, `api/jobs.py`, `tests/test_db.py`, `tests/test_queue.py`, `tests/test_runner.py`, `tests/test_api_jobs.py`, `tests/conftest.py` |
| W2 pipeline-adapter | `narratarr/adapter/**`, `tests/test_adapter.py`, `tests/test_ingest.py`, `tests/test_targets.py` |
| W3 review-backend | `narratarr/api/review.py`, `api/targets.py`, `api/settings.py`, `tests/test_api_review.py`, `tests/test_api_targets.py` |
| W4 frontend | `web/**`, `docs/screenshots/**` |
| W5 packaging | `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `.env.example`, `scripts/**`, `.github/**`, `docs/api.md` |

**Only the overlord commits.** A worker never runs a git command that writes.

### 14.1 The shared Python seam

W1 writes these. Every other worker imports them and writes none of them. **This list is
the whole seam between the backend workers.**

```python
# narratarr/config.py
settings: Settings            # a module-level singleton, read from the environment
def get_settings() -> Settings: ...

# narratarr/db.py
def connect() -> sqlite3.Connection
    """Return a connection. row_factory is sqlite3.Row. WAL and foreign_keys are on."""
@contextmanager
def transaction() -> Iterator[sqlite3.Connection]
    """Commit on success. Roll back on an exception."""
def now() -> str
    """Return the UTC stamp, in the form YYYYMMDDThhmmssZ."""
def new_id() -> str
    """Return a uuid4 hex."""
def init_db() -> None
    """Apply schema.sql and every migration."""

# narratarr/api/common.py
def require_key(request: Request) -> ApiKey
    """The FastAPI dependency that checks the X-Api-Key header."""
class ApiError(Exception)
    def __init__(self, code: str, message: str, status: int = 400, detail: dict | None = None)
def paginate(items, limit, offset) -> dict
    """Return the {"items", "total", "limit", "offset"} envelope of section 13."""

# narratarr/api/__init__.py
def create_app() -> FastAPI
    """Make the application. Include every router. Install the ApiError handler."""
```

`create_app()` includes a router from each of these modules, and each module exposes it
under the name `router`: `api.system`, `api.jobs`, `api.review`, `api.targets`,
`api.settings`.

**Every router declares `dependencies=[Depends(require_key)]`**, except `api.system`'s
health route, which declares no dependency.

**Each router carries its own full prefix, and `create_app()` adds none.** A router
declares `APIRouter(prefix="/api/v1/jobs", ...)`, and `create_app()` calls
`app.include_router(router)` with no `prefix` argument. **Never pass a prefix in both
places**: the route then answers at `/api/v1/api/v1/jobs`, and every client breaks at once.

---

## 15. The house rules

1. **Write every docstring and comment in ASD-STE100 Simplified Technical English.** One
   instruction per sentence. Active voice. Present tense. One word, one meaning. Keep the
   articles.
2. **A test never loads a model and never renders real audio.**
3. **Never widen a threshold or weaken a test to turn a red gate green.**
4. **A stage that produces nothing and reports success is worse than a crash.**
5. **Every write of a file is atomic**, and the meta file is the last write. Refer to
   pipeline contract 3.3.
6. **No acquisition feature, ever.** Refer to section 1.1.

---

## 16. The v2 seams

v1 does not build these. v1 **leaves the seam** so that v2 needs no migration. A worker
must not build one of them without an instruction from the overlord.

| Seam | What v1 leaves |
|---|---|
| The webhook receiver | Refer to 16.1. |
| The Mac render agent | The `worker` column of `jobs`, always `local` in v1. Refer to 4.2. |
| The CUDA image | The engine name is configuration, never a constant. |
| The `-full` image | The model fetcher of 11.1 is the only thing that changes. |
| Concurrency | The atomic claim of the queue. A second runner must not double-claim. |

### 16.1 The webhook receiver

**Chaptarr is the primary named integration.** Chaptarr is the maintained Readarr
replacement, and it is a common ebook manager in a self-hosted stack. Chaptarr's
**"On Import"** webhook fires when Chaptarr imports an ebook. That event names a file that
already exists on disk, so it is the natural way to make a Narratarr job.

Readarr is the legacy secondary. Readarr is no longer maintained. Support it, name it
second, and never let its payload shape drive the design.

**A webhook is not an acquisition feature.** Narratarr receives a message that says "this
book arrived". Narratarr does not search, does not request, and does not download.
Narratarr consumes a file that another program already placed on disk. Refer to section
1.1.

The planned route is `POST /api/v1/webhooks/chaptarr`. v1 does not implement it. The
ingest module of section 7 is written so that a fourth caller needs no change to it.
