# The Narratarr API

This document describes `/api/v1`, the API that `narratarr/api/` serves.
Read APP-CONTRACT.md section 13 first. **That document is the frozen
specification. This document explains it in prose. When the two
disagree, APP-CONTRACT.md is correct.**

## Authentication

Every route needs the header `X-Api-Key`, with two exceptions:
`GET /api/v1/system/health` and the static files of the single-page
application.

```
X-Api-Key: <your key>
```

**Put the key in the header. Never put the key in a URL.** A URL enters
a browser's history, a server access log, and a `Referer` header sent to
another site. A query parameter such as `?apikey=...` is not a safe
substitute; do not add one.

Make a key with `POST /api/v1/keys`. The response holds the key's plain
value once. Narratarr stores only the key's sha256, so a lost key cannot
be recovered; make a new one instead.

## The error envelope

Every error response has this shape:

```json
{
  "error": {
    "code": "not_found",
    "message": "No job with that id.",
    "detail": {}
  }
}
```

`code` is a short, stable, machine-readable string. `message` is a
sentence a person can read. `detail` holds extra structured context, or
an empty object when there is none.

## Status codes

| Code | Meaning |
|---|---|
| `200` | The request succeeded. |
| `201` | The request made a new resource. |
| `202` | Narratarr accepted the request. The runner does the work later. |
| `400` | The request is malformed. |
| `401` | The `X-Api-Key` header is missing or wrong. |
| `404` | No resource has that id. |
| `409` | The request conflicts with the resource's current state. |
| `422` | The request failed validation, for example an empty `reason`. |
| `500` | Narratarr faulted. |

## Pagination

Every route that lists resources takes `?limit=` (default 50, maximum
200) and `?offset=`. The response has this shape:

```json
{ "items": [...], "total": 137, "limit": 50, "offset": 0 }
```

## Routes

### System

| Route | Auth | Action |
|---|---|---|
| `GET /system/health` | none | `{"status": "ok", "version": "…"}`. Use this route for a container healthcheck; it needs no key. |
| `GET /system/status` | key | The runner state, the disk free, which models are present, which secrets are present, and the queue depth. Refer to APP-CONTRACT.md section 10.2: a secret's presence is reported, never its value. |
| `GET /system/models` | key | Every model Narratarr uses, with its downloaded state and its size. |
| `POST /system/models/fetch` | key | Start the first-run model download. Returns `202` immediately; the download runs in the background. Refer to `scripts/fetch_models.py`. |

### Jobs

One job is one book, from ingest through delivery.

| Route | Auth | Action |
|---|---|---|
| `GET /jobs` | key | List jobs. Filters: `?state=`, `?q=`. |
| `POST /jobs` | key | Make a job. Send an EPUB upload, or `{"source_path": "…"}` for a file already on disk. Returns `201`. |
| `GET /jobs/{id}` | key | One job, with its gates and its deliveries. |
| `DELETE /jobs/{id}` | key | Delete the job's database row. Add `?purge=true` to also delete its work directory. |
| `POST /jobs/{id}/start` | key | Queue the job for the runner. `202`. |
| `POST /jobs/{id}/pause` | key | `202`. |
| `POST /jobs/{id}/cancel` | key | `202`. |
| `POST /jobs/{id}/retry` | key | Clear the job's error and queue it again. `202`. |
| `GET /jobs/{id}/config` | key | The book config and the QC config. |
| `PUT /jobs/{id}/config` | key | Replace them. Returns `409` while the job is running. |
| `GET /jobs/{id}/events` | key | The job's log. Filters: `?since=`, `?level=`. |
| `GET /jobs/{id}/events/stream` | key | The same log, as a server-sent-events stream. |
| `GET /jobs/{id}/artifacts` | key | The output file paths and their sizes. |
| `GET /jobs/{id}/status` | key | The per-stage fresh, stale, and absent chunk count. |
| `POST /jobs/{id}/deliver` | key | Deliver to every enabled target. `202`. |
| `POST /jobs/{id}/fix` | key | The Fix flow of APP-CONTRACT.md section 9.5. `202`. |

### Gates and review

A gate is a point where a job stops and waits for a person. Refer to
APP-CONTRACT.md section 9.

| Route | Auth | Action |
|---|---|---|
| `GET /gates` | key | Every open gate, across every job. This is the review queue. |
| `GET /gates/{id}` | key | One gate, with its review items. |
| `POST /gates/{id}/resolve` | key | Body: `{"resolution": "…", "reason": "…"}`. |
| `GET /review/items` | key | Filters: `?job_id=`, `?gate_id=`, `?state=`, `?kind=`. |
| `GET /review/items/{id}` | key | One item, with its word-level diff. |
| `POST /review/items/{id}/accept` | key | Body: `{"reason": "…"}`. **Returns `422` when `reason` is empty.** An acceptance without a stated reason is not recorded. |
| `POST /review/items/{id}/rerender` | key | `202`. |
| `POST /review/items/{id}/resolve` | key | The homograph choice. Body: `{"reading": "…"}`. |
| `GET /review/items/{id}/audio` | key | The rendered chunk, as `audio/wav`. |
| `GET /review/items/{id}/audio/{n}` | key | Candidate `n` of a homograph item's two readings. |

**The audio routes need the `X-Api-Key` header, so fetch them with
`fetch()`, never with a plain `<audio src="…">` URL.** A plain URL cannot
carry a header, and this project does not add a `?apikey=` fallback.
Build a blob URL from the `fetch()` response instead.

### Targets and settings

| Route | Auth | Action |
|---|---|---|
| `GET /targets`, `POST /targets` | key | List, or make, a delivery target. |
| `GET /targets/{id}`, `PUT /targets/{id}`, `DELETE /targets/{id}` | key | Read, replace, or delete one target. |
| `POST /targets/{id}/test` | key | Check that the target is reachable. Writes nothing. |
| `GET /settings`, `PUT /settings` | key | Read or replace Narratarr's settings. |
| `GET /library` | key | The ingested files. |
| `POST /library/scan` | key | Poll the watch folder now, instead of waiting for the next interval. `202`. |
| `GET /keys`, `POST /keys`, `DELETE /keys/{id}` | key | Manage API keys. A key's value returns once, on `POST`, and never again. |

**A target's configuration never returns a secret.** A response shows
`"token_env": "NARRATARR_ABS_TOKEN"`, the name of the environment
variable that holds the token, never the token's value. Refer to
APP-CONTRACT.md section 10.2.

## Not implemented in v1

`POST /api/v1/webhooks/chaptarr` is a planned route for a future version.
Refer to APP-CONTRACT.md section 16.1. It does not exist in v1.
