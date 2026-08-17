<div align="center">

# Narratarr — overview

**An ebook goes in. An audiobook comes out.**

A self-hosted, servarr-style companion app that turns the ebooks you already own into
chaptered m4b audiobooks, with local neural text-to-speech and an automatic
speech-recognition quality gate.

</div>

---

## What it is

Narratarr is the audiobook factory for your media stack. You give it an EPUB. It reads the
book, splits it into chunks, renders each chunk with a local neural voice, transcribes the
result to check the audio against the text, assembles a chaptered m4b, and delivers it to a
folder or to Audiobookshelf.

Everything runs on your own machine. No cloud service reads your books. No API key goes to
a text-to-speech vendor.

**Narratarr is a companion, in the Bazarr sense.** Bazarr improves media you already hold.
Narratarr does the same for an ebook you already hold.

### Narratarr never acquires a book

**Narratarr holds no indexer, no tracker, and no download client.** It sends no search
anywhere. It consumes a file that you give it, and nothing else. This is a design rule
with no exception and no planned exception.

If you want the acquisition side, that is a different tool. Look at
[Chaptarr](https://github.com/), the maintained Readarr replacement, or at Listenarr.
Narratarr is built to sit **downstream** of one of those, and v2 will accept Chaptarr's
"On Import" webhook.

---

## Honest expectations

**Narratarr is a batch appliance. It is not fast, and it never claims to be.**

It renders one book at a time on a CPU. On a 4-core Intel i5-6500T — a low-power desktop
chip from 2015 — a full-length novel takes **between one night and one day**.

That is the trade. You get a private, local, unmetered audiobook factory that costs
nothing per book and runs on hardware you already own. You do not get a result in ten
minutes. Start a book before bed.

### Measured, on a 2015 4-core i5-6500T capped at 3 CPUs

| | |
|---|---|
| Render | 22.4 characters a second, **1.43x realtime** |
| Quality control | real-time factor **0.267** |
| Peak memory | **2.6 GB** |
| **A 7-hour audiobook** | **about 7 hours, start to finish** |

A faster CPU does proportionally better. There is no GPU path in v1.

Every number in this README is measured on real hardware. None is estimated.

---

## The human gates

An automatic pipeline that never asks a person produces confident, wrong audio. Narratarr
stops and asks, three times, and each gate is a first-class state in the queue.

| Gate | What it asks | Why it exists |
|---|---|---|
| **Sample** | "Here is 90 seconds. Does this voice work for this book?" | The passage is chosen for the hazards — the worst proper noun, a foreign term, a number, an ALL-CAPS run — not for the nicest prose. |
| **Homograph** | "Is this *wound* the injury or the verb?" **It plays you both.** | The engine picks a reading from a part-of-speech tag, and it sometimes picks wrong. Quality control is blind to this: the transcript spells both readings the same way. |
| **Quality control** | "The transcript does not match the text here. Listen." | Shows a word-level diff, the error rate, the coverage, and the audio. |

The review queue is modelled on Radarr's manual import: the evidence, a small set of
actions, and a **mandatory written reason** for every acceptance. Six months later you can
read why a chunk was let through.

**An acceptance is pinned to the audio it approved.** Re-render that chunk and the
acceptance dies with it. Nobody can approve a chunk and then quietly change the audio
underneath it.

### The Fix flow

You find a mispronounced name in chapter 7, after the book is delivered. You do not rebuild
the book. Narratarr re-renders **only the affected chunks**, re-assembles that one chapter,
and re-delivers. Minutes, not hours.

---

## Quick start

```bash
git clone <this-repo> narratarr
cd narratarr
cp .env.example .env      # edit if you like; the defaults work
docker compose up -d
```

Open `http://localhost:8000`. The first run prints an API key to the log — save it.

Then drop an EPUB into `./watch/` and watch the queue.

The first run downloads the voice model and the speech-recognition model into `./config/models`.
That is a one-time download of about 1.5 GB, with a disk check before it starts.

### What you need

- Docker and Docker Compose
- About 4 GB of RAM free
- About 10 GB of disk for the image, the models, and one book's intermediate files
- A **DRM-free** EPUB. Narratarr refuses a file with DRM and never circumvents it.

---

## Delivery targets

| Target | What it does |
|---|---|
| **Folder** | Writes `Author/Title/Title.m4b` plus the cover. Serves Audiobookshelf, Plex, Jellyfin, or anything that watches a directory. This is the default. |
| **Audiobookshelf** | Copies the book, triggers a library scan, then verifies the title, the author, the chapter count, and the duration. |

The Audiobookshelf target uses a token **you create in the Audiobookshelf interface** and
pass as an environment variable. Narratarr never reads Audiobookshelf's database and never
asks for your password.

---

## Configuration

Every setting is an environment variable. `.env.example` documents all of them.

The ones that matter most:

| Variable | Default | Meaning |
|---|---|---|
| `NARRATARR_VOICE` | `bm_george` | The Kokoro voice. |
| `NARRATARR_LANG_CODE` | `b` | `a` for US English, `b` for British. |
| `NARRATARR_NUM_THREADS` | `3` | Match this to your CPU limit. |
| `NARRATARR_WHISPER_MODEL` | see `.env.example` | A larger model is more accurate and much slower. |
| `NARRATARR_SAMPLE_GATE` | `true` | Turn off for unattended runs. |

---

## How it works

```
EPUB ─▶ extract ─▶ normalize ─▶ chunk ─▶ [sample gate] ─▶ [homograph gate]
                                              │
                                              ▼
       m4b ◀── bind ◀── assemble ◀── [QC gate] ◀── quality control ◀── render
        │
        └─▶ folder target ─▶ Audiobookshelf target
```

Every stage is **idempotent**. Each output carries a meta file that records the hash of its
inputs, the hash of its configuration, and its own size. A stage skips work whose output is
already correct, so a killed run resumes at the first missing or stale artifact rather than
starting again.

The pipeline itself is [`abpipe`](vendor/abpipe/), vendored in this repository. Its own
contract, in `vendor/abpipe/CONTRACT.md`, documents every stage, every threshold, and every
fault that shaped them. It is worth reading if you want to know why a rule exists.

### The one thing worth knowing about the engine

Kokoro is **not deterministic**. Two renders of the same text give different bytes. This is
measured, not assumed. It is why a re-render always voids an acceptance, and it is why the
quality-control ladder scores every attempt and keeps the best one rather than the last one.

---

## Development

```bash
uv venv --python 3.12 && source .venv/bin/activate
uv pip install -e ".[dev,pipeline]"
pytest

cd web && npm install && npm run dev
```

`APP-CONTRACT.md` is the specification: the database schema, the adapter interface, the
target interface, and the frozen `/api/v1` spec. Read it before you change anything. If the
code and that document disagree, the document is right.

The API is documented in [`docs/api.md`](docs/api.md). Every route takes an `X-Api-Key`
header. **The key never goes in a URL.**

---

## Licence

GPL-3.0-or-later. See [LICENSE](LICENSE).

Narratarr builds on [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M),
[misaki](https://github.com/hexgrad/misaki), [espeak-ng](https://github.com/espeak-ng/espeak-ng),
and [faster-whisper](https://github.com/SYSTRAN/faster-whisper). Thanks to all of them.
