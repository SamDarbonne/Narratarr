-- The Narratarr schema.
--
-- APP-CONTRACT.md section 4 defines every table, column, index, and default
-- below. This file matches that section exactly, with one addition:
-- `IF NOT EXISTS` on every statement, so that `db.init_db()` can apply this
-- file again on an already-initialized database without an error. Refer to
-- APP-CONTRACT.md section 14.1, `init_db()`.

-- 4.1 meta
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 4.2 jobs
CREATE TABLE IF NOT EXISTS jobs (
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
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_jobs_priority ON jobs(priority DESC, created_at ASC);

-- 4.3 gates
CREATE TABLE IF NOT EXISTS gates (
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
CREATE INDEX IF NOT EXISTS idx_gates_open ON gates(state, job_id);

-- 4.4 review_items
CREATE TABLE IF NOT EXISTS review_items (
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
CREATE INDEX IF NOT EXISTS idx_review_open ON review_items(state, job_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_review_identity
  ON review_items(job_id, kind, chapter, chunk, word, occurrence);

-- 4.5 events
CREATE TABLE IF NOT EXISTS events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id     TEXT REFERENCES jobs(id) ON DELETE CASCADE,
  level      TEXT NOT NULL,       -- debug | info | warning | error
  stage      TEXT,
  message    TEXT NOT NULL,
  data       TEXT,                -- JSON
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, id);

-- 4.6 targets and deliveries
CREATE TABLE IF NOT EXISTS targets (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL UNIQUE,
  kind       TEXT NOT NULL,       -- folder | audiobookshelf
  enabled    INTEGER NOT NULL DEFAULT 1,
  config     TEXT NOT NULL DEFAULT '{}',   -- JSON. Refer to section 8.
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
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
CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_pair ON deliveries(job_id, target_id);

-- 4.7 api_keys and settings
CREATE TABLE IF NOT EXISTS api_keys (
  id           TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  key_sha256   TEXT NOT NULL UNIQUE,
  created_at   TEXT NOT NULL,
  last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,       -- JSON
  updated_at TEXT NOT NULL
);
