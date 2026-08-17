"""The review queue. `GET /gates` is the review queue.

APP-CONTRACT.md section 9 defines the human loop. APP-CONTRACT.md section
13.3 defines every route in this file. Read both before you change this
file.

The idiom is Radarr's manual import: show the evidence, offer a small set
of actions, and record why. This module never runs the pipeline. Every
action writes state, sets the job back to `queued`, and returns `202`. The
runner does the render.

Two facts drive the state machine below, and both come from
`vendor/abpipe/CONTRACT.md`:

- Section 9.7: an acceptance needs a reason. An entry with no reason is
  ignored upstream, with a warning. This API refuses an empty reason
  outright, so a silent no-op never happens here.
- Section 9.3 and 9.7: Kokoro is not deterministic. A re-render changes the
  WAV bytes even when the words, the length, and the reading stay the
  same. Every pin voids on every re-render, always. A voided pin is not
  proof that anything changed.
"""

from __future__ import annotations

import dataclasses
import difflib
import json
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel

from narratarr.api.common import ApiError, paginate, require_key
from narratarr.config import get_settings
from narratarr.db import connect, now, transaction
from narratarr.models import Gate, GateResolveRequest, ReviewItem

router = APIRouter(prefix="/api/v1", tags=["review"], dependencies=[Depends(require_key)])

# The plain column names of the `gates` table. A query that joins `gates`
# against `jobs` for extra display columns (`list_gates` below) must strip
# those extra columns before it builds a `Gate`, because `Gate.from_row`
# passes every column of the row straight through as a keyword argument.
_GATE_COLUMNS = {f.name for f in dataclasses.fields(Gate)}


# --------------------------------------------------------------------- text

# Section 9.7 of the pipeline contract: "Write this outright. A reader who
# finds a voided pin will otherwise treat it as evidence that something
# changed. It is not." This string is the one place that teaches a reader
# what a voided pin means. Keep it exact if you edit it.
VOIDED_REASON = (
    "A re-render voided this acceptance. Kokoro does not give the same "
    "bytes twice, so the audio hash changes on every re-render, even when "
    "the words, the length, and the reading stay the same. This voided "
    "pin is not proof that the audio changed. It means only that a "
    "person judged this chunk's audio correct once, before this "
    "re-render."
)


# ----------------------------------------------------------------- request bodies


class AcceptRequest(BaseModel):
    """The body of `POST /review/items/{id}/accept`.

    The reason is mandatory. Refer to pipeline contract 9.7.
    """

    reason: str


class HomographResolveRequest(BaseModel):
    """The body of `POST /review/items/{id}/resolve`. Names one reading."""

    reading: str


# --------------------------------------------------------------------- helpers


def _get_job_row(conn, job_id: str):
    """Return the `jobs` row for `job_id`, or None."""
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def _get_gate_row(conn, gate_id: str):
    """Return the `gates` row for `gate_id`, or None."""
    return conn.execute("SELECT * FROM gates WHERE id = ?", (gate_id,)).fetchone()


def _get_item_row(conn, item_id: str):
    """Return the `review_items` row for `item_id`, or None."""
    return conn.execute(
        "SELECT * FROM review_items WHERE id = ?", (item_id,)
    ).fetchone()


def _require_gate(conn, gate_id: str):
    """Return the gate row, or raise a 404 `ApiError`."""
    row = _get_gate_row(conn, gate_id)
    if row is None:
        raise ApiError("gate_not_found", "No gate holds this id.", status=404)
    return row


def _require_item(conn, item_id: str):
    """Return the review item row, or raise a 404 `ApiError`."""
    row = _get_item_row(conn, item_id)
    if row is None:
        raise ApiError("review_item_not_found", "No review item holds this id.", status=404)
    return row


def _serialize_gate(row, job_row=None) -> dict:
    """Return one gate as a JSON-safe dict, with its job's title and slug."""
    data = Gate.from_row(row).to_dict()
    data["job_title"] = job_row["title"] if job_row is not None else None
    data["job_slug"] = job_row["slug"] if job_row is not None else None
    return data


def _word_diff(source_text: str | None, transcript: str | None) -> list[dict]:
    """Return a word-level diff between the source text and the transcript.

    Each entry names an "op" of "equal", "delete", "insert", or "replace",
    and holds the words each side has for that span. Section 9.3 of
    APP-CONTRACT.md asks for this diff on `GET /review/items/{id}`.
    """
    source_words = (source_text or "").split()
    transcript_words = (transcript or "").split()
    matcher = difflib.SequenceMatcher(a=source_words, b=transcript_words, autojunk=False)
    diff = []
    for op, a0, a1, b0, b1 in matcher.get_opcodes():
        diff.append(
            {
                "op": op,
                "source": source_words[a0:a1],
                "transcript": transcript_words[b0:b1],
            }
        )
    return diff


def _serialize_candidates(candidates: list | None) -> list | None:
    """Return the candidate readings, without the server-side audio path.

    A candidate's `audio` field is a filesystem path. The client fetches
    the audio through `GET /review/items/{id}/audio/{n}` instead. Refer to
    APP-CONTRACT.md section 10.1: a path is not a key, but there is no
    reason to hand a client a server path it cannot use.
    """
    if not candidates:
        return candidates
    result = []
    for i, candidate in enumerate(candidates, start=1):
        result.append(
            {
                "n": i,
                "reading": candidate.get("reading"),
                "phonemes": candidate.get("phonemes"),
            }
        )
    return result


def _serialize_item(row, with_diff: bool = False) -> dict:
    """Return one review item as a JSON-safe dict.

    Adds `voided_reason` when the item's state is `voided`. Refer to the
    module docstring for the exact wording and why it exists.
    """
    data = ReviewItem.from_row(row).to_dict()
    data["candidates"] = _serialize_candidates(data.get("candidates"))
    if data["state"] == "voided":
        data["voided_reason"] = VOIDED_REASON
    if with_diff:
        data["diff"] = _word_diff(data.get("source_text"), data.get("transcript"))
    return data


def _close_item_and_decrement_gate(conn, item_row, new_state: str) -> None:
    """Move a review item out of `open`, and shrink its gate's open count.

    Section 4.4 of APP-CONTRACT.md: `open_items` counts the items a person
    still must answer. This helper is the one place that keeps the count
    honest. When the count reaches zero, the gate closes.
    """
    stamp = now()
    conn.execute(
        "UPDATE review_items SET state = ?, resolved_at = ? WHERE id = ?",
        (new_state, stamp, item_row["id"]),
    )
    gate_id = item_row["gate_id"]
    conn.execute(
        "UPDATE gates SET open_items = MAX(open_items - 1, 0) WHERE id = ?",
        (gate_id,),
    )
    gate_row = conn.execute(
        "SELECT open_items FROM gates WHERE id = ?", (gate_id,)
    ).fetchone()
    if gate_row is not None and gate_row["open_items"] == 0:
        conn.execute(
            "UPDATE gates SET state = 'resolved' WHERE id = ? AND state = 'open'",
            (gate_id,),
        )


def _requeue_job(conn, job_id: str) -> None:
    """Set a job back to `queued`. The runner resumes it from there.

    APP-CONTRACT.md section 9.4: the API never runs the pipeline. Every
    review action writes state and hands the job back to the runner.
    """
    conn.execute(
        "UPDATE jobs SET state = 'queued', updated_at = ? WHERE id = ?",
        (now(), job_id),
    )


def _chunk_audio_path(job_row, item_row) -> Path:
    """Return the on-disk path of a QC chunk's rendered WAV.

    Pipeline contract section 2: a chunk's audio lives at
    `work/<slug>/04-audio/<chapter>/<chunk>.wav`. Narratarr never hard-codes
    `/config`; the path always starts at `settings.work_dir`. Refer to
    APP-CONTRACT.md section 2.1.
    """
    settings = get_settings()
    return (
        settings.work_dir
        / job_row["slug"]
        / "04-audio"
        / item_row["chapter"]
        / f"{item_row['chunk']}.wav"
    )


def _stream_wav(path: Path, request: Request) -> Response:
    """Return a WAV file as `audio/wav`. Serve a byte range when asked.

    A browser `<audio>` element seeks by issuing a `Range` request. This
    function answers one with a `206` and a `Content-Range` header. A
    request with no `Range` header gets the whole file with a `200`.
    """
    if not path.is_file():
        raise ApiError("audio_not_found", "The audio file is not on disk.", status=404)

    file_size = path.stat().st_size
    range_header = request.headers.get("range")

    if range_header:
        parsed = _parse_range(range_header, file_size)
        if parsed is not None:
            start, end = parsed
            with path.open("rb") as handle:
                handle.seek(start)
                body = handle.read(end - start + 1)
            headers = {
                "Accept-Ranges": "bytes",
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Content-Length": str(len(body)),
            }
            return Response(content=body, media_type="audio/wav", status_code=206, headers=headers)

    body = path.read_bytes()
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(file_size)}
    return Response(content=body, media_type="audio/wav", headers=headers)


def _parse_range(range_header: str, file_size: int) -> tuple[int, int] | None:
    """Parse a `Range: bytes=start-end` header. Return None when it is not valid.

    The two numbers are returned together, or not at all. An earlier
    signature returned `tuple[int | None, int | None]`, which says a start
    can arrive without an end. The code never did that, but a caller that
    trusted the type had to guard a case that cannot happen, and a caller
    that read the code guarded nothing. A type that cannot express the
    invariant makes one of the two wrong.

    An open range, `bytes=0-`, is the common form a browser sends for an
    `<audio>` element, and it ends at the last byte.
    """
    units, _, spec = range_header.partition("=")
    if units.strip() != "bytes":
        return None
    start_text, _, end_text = spec.partition("-")
    try:
        start = int(start_text) if start_text.strip() else 0
        end = int(end_text) if end_text.strip() else file_size - 1
    except ValueError:
        return None
    end = min(end, file_size - 1)
    if start < 0 or start > end:
        return None
    return start, end


# --------------------------------------------------------------------- gates


@router.get("/gates")
def list_gates(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    """Return every open gate, across every job. This is the review queue."""
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT gates.*, jobs.title AS job_title, jobs.slug AS job_slug
            FROM gates
            JOIN jobs ON jobs.id = gates.job_id
            WHERE gates.state = 'open'
            ORDER BY gates.created_at ASC
            """
        ).fetchall()
        items = []
        for row in rows:
            gate_only = {k: row[k] for k in row.keys() if k in _GATE_COLUMNS}  # noqa: SIM118 -- `row.keys()` is sqlite3.Row's own API, not a dict
            gate_dict = Gate(**gate_only).to_dict()
            gate_dict["job_title"] = row["job_title"]
            gate_dict["job_slug"] = row["job_slug"]
            items.append(gate_dict)
        return paginate(items, limit, offset)
    finally:
        conn.close()


@router.get("/gates/{gate_id}")
def get_gate(gate_id: str):
    """Return one gate, with every review item that belongs to it."""
    conn = connect()
    try:
        gate_row = _require_gate(conn, gate_id)
        job_row = _get_job_row(conn, gate_row["job_id"])
        item_rows = conn.execute(
            "SELECT * FROM review_items WHERE gate_id = ? ORDER BY created_at ASC",
            (gate_id,),
        ).fetchall()
        result = _serialize_gate(gate_row, job_row)
        result["items"] = [_serialize_item(row) for row in item_rows]
        return result
    finally:
        conn.close()


@router.post("/gates/{gate_id}/resolve", status_code=202)
def resolve_gate(gate_id: str, body: GateResolveRequest):
    """Resolve a gate as a whole. Body: `{"resolution": "…", "reason": "…"}`.

    This is the sample gate's action (approve, reject, or edit config).
    It never touches the review items under a homograph or QC gate; a
    person resolves those one at a time. Refer to APP-CONTRACT.md 9.1.
    """
    with transaction() as conn:
        gate_row = _require_gate(conn, gate_id)
        if gate_row["state"] != "open":
            raise ApiError(
                "gate_not_open",
                f"This gate is '{gate_row['state']}', not 'open'.",
                status=409,
            )
        stamp = now()
        conn.execute(
            """
            UPDATE gates
            SET state = 'resolved', resolution = ?, reason = ?, resolved_at = ?
            WHERE id = ?
            """,
            (body.resolution, body.reason, stamp, gate_id),
        )
        _requeue_job(conn, gate_row["job_id"])
        gate_row = _get_gate_row(conn, gate_id)
        job_row = _get_job_row(conn, gate_row["job_id"])
        return _serialize_gate(gate_row, job_row)


# --------------------------------------------------------------- review items


@router.get("/review/items")
def list_review_items(
    job_id: str | None = Query(None),
    gate_id: str | None = Query(None),
    state: str | None = Query(None),
    kind: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List review items. Filters: `?job_id=`, `?gate_id=`, `?state=`, `?kind=`."""
    clauses = []
    params: list = []
    if job_id:
        clauses.append("job_id = ?")
        params.append(job_id)
    if gate_id:
        clauses.append("gate_id = ?")
        params.append(gate_id)
    if state:
        clauses.append("state = ?")
        params.append(state)
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT * FROM review_items {where} ORDER BY created_at ASC",
            params,
        ).fetchall()
        items = [_serialize_item(row) for row in rows]
        return paginate(items, limit, offset)
    finally:
        conn.close()


@router.get("/review/items/{item_id}")
def get_review_item(item_id: str):
    """Return one review item, with the word-level diff."""
    conn = connect()
    try:
        row = _require_item(conn, item_id)
        return _serialize_item(row, with_diff=True)
    finally:
        conn.close()


@router.post("/review/items/{item_id}/accept", status_code=202)
def accept_review_item(item_id: str, body: AcceptRequest):
    """Accept a QC chunk. The reason is mandatory.

    Pipeline contract 9.7: an acceptance says a person judged the audio
    correct, and a later reader needs the reason to know why. An empty
    reason is refused here with `422`, so it can never silently do
    nothing the way an unreasoned entry does upstream.

    The acceptance pins `wav_sha256` at the moment of acceptance. Refer to
    the module docstring for what a later re-render does to that pin.
    """
    reason = body.reason.strip() if body.reason else ""
    if not reason:
        raise ApiError(
            "reason_required",
            "The reason is empty. Write why the audio is correct.",
            status=422,
        )

    with transaction() as conn:
        item_row = _require_item(conn, item_id)
        if item_row["kind"] != "qc_chunk":
            raise ApiError(
                "wrong_kind",
                "Only a qc_chunk item takes an accept action.",
                status=400,
            )
        if item_row["state"] != "open":
            raise ApiError(
                "item_not_open",
                f"This item is '{item_row['state']}', not 'open'.",
                status=409,
            )
        conn.execute(
            """
            UPDATE review_items
            SET state = 'accepted', resolution = 'accept', reason = ?,
                resolved_at = ?
            WHERE id = ?
            """,
            (reason, now(), item_id),
        )
        gate_row = conn.execute(
            "SELECT open_items FROM gates WHERE id = ?", (item_row["gate_id"],)
        ).fetchone()
        conn.execute(
            "UPDATE gates SET open_items = MAX(open_items - 1, 0) WHERE id = ?",
            (item_row["gate_id"],),
        )
        if gate_row is not None and gate_row["open_items"] - 1 <= 0:
            conn.execute(
                "UPDATE gates SET state = 'resolved' WHERE id = ? AND state = 'open'",
                (item_row["gate_id"],),
            )
        _requeue_job(conn, item_row["job_id"])
        return _serialize_item(_get_item_row(conn, item_id))


@router.post("/review/items/{item_id}/rerender", status_code=202)
def rerender_review_item(item_id: str):
    """Ask the runner to re-render one chunk.

    Warning: every pin voids on every re-render, always. Kokoro is not
    deterministic (pipeline contract 9.3), so a re-render changes the
    bytes even when the words, the length, and the reading are the same.
    An item with an existing accepted pin moves to `voided`, with the
    `voided_reason` of the module docstring attached. An item with no pin
    yet moves to `rerendered`, and waits for the runner's fresh result.
    """
    with transaction() as conn:
        item_row = _require_item(conn, item_id)
        if item_row["kind"] != "qc_chunk":
            raise ApiError(
                "wrong_kind",
                "Only a qc_chunk item takes a rerender action.",
                status=400,
            )
        if item_row["state"] == "accepted":
            new_state = "voided"
        elif item_row["state"] == "open":
            new_state = "rerendered"
        else:
            raise ApiError(
                "item_not_open",
                f"This item is '{item_row['state']}'. Only an 'open' or "
                "'accepted' item can be rerendered.",
                status=409,
            )
        _close_item_and_decrement_gate(conn, item_row, new_state)
        _requeue_job(conn, item_row["job_id"])
        return _serialize_item(_get_item_row(conn, item_id))


@router.post("/review/items/{item_id}/resolve", status_code=202)
def resolve_homograph_item(item_id: str, body: HomographResolveRequest):
    """Record a person's choice of reading for one homograph occurrence.

    Pipeline contract 18.4: this writes a decision with `human: true`.
    The runner, on resuming, writes that decision into
    `work/<slug>/homographs.json`. The audit never overwrites a
    `human: true` decision.
    """
    reading = body.reading.strip() if body.reading else ""
    if not reading:
        raise ApiError("reading_required", "Name a reading.", status=422)

    with transaction() as conn:
        item_row = _require_item(conn, item_id)
        if item_row["kind"] != "homograph_occurrence":
            raise ApiError(
                "wrong_kind",
                "Only a homograph_occurrence item takes a resolve action.",
                status=400,
            )
        if item_row["state"] != "open":
            raise ApiError(
                "item_not_open",
                f"This item is '{item_row['state']}', not 'open'.",
                status=409,
            )
        candidates = json.loads(item_row["candidates"]) if item_row["candidates"] else []
        known_readings = {c.get("reading") for c in candidates}
        if known_readings and reading not in known_readings:
            raise ApiError(
                "unknown_reading",
                f"'{reading}' is not one of this item's candidate readings.",
                status=422,
            )
        conn.execute(
            """
            UPDATE review_items
            SET state = 'resolved', resolution = ?, resolved_at = ?
            WHERE id = ?
            """,
            (reading, now(), item_id),
        )
        gate_row = conn.execute(
            "SELECT open_items FROM gates WHERE id = ?", (item_row["gate_id"],)
        ).fetchone()
        conn.execute(
            "UPDATE gates SET open_items = MAX(open_items - 1, 0) WHERE id = ?",
            (item_row["gate_id"],),
        )
        if gate_row is not None and gate_row["open_items"] - 1 <= 0:
            conn.execute(
                "UPDATE gates SET state = 'resolved' WHERE id = ? AND state = 'open'",
                (item_row["gate_id"],),
            )
        _requeue_job(conn, item_row["job_id"])
        return _serialize_item(_get_item_row(conn, item_id))


@router.get("/gates/{gate_id}/audio")
def get_gate_audio(gate_id: str, request: Request):
    """Stream the audio of a `sample` gate. `audio/wav`.

    A sample gate holds no review item. The sample is one passage for the
    whole book, not a list of chunks to answer, so its audio hangs off the
    gate. Refer to APP-CONTRACT.md 9.1 and 13.3.

    The passage is chosen for the hazards, not for the prose. A person
    listens for a wrong reading of the worst proper noun, a foreign term,
    a number, and an ALL-CAPS run.

    Supports a `Range` request, so a browser `<audio>` element can seek.
    The API key travels in the `X-Api-Key` header, never in the URL.
    """
    conn = connect()
    try:
        gate_row = _require_gate(conn, gate_id)
        if gate_row["kind"] != "sample":
            raise ApiError(
                "wrong_kind",
                "Only a sample gate serves audio. A qc or homograph gate "
                "serves audio per review item, at /review/items/{id}/audio.",
                status=404,
            )
        payload = json.loads(gate_row["payload"] or "{}")
        # The runner writes `wav_path`. `audio_path` is accepted too, because
        # this route and the runner were written by different hands and the
        # contract named neither. Read both rather than break on the spelling.
        raw = payload.get("wav_path") or payload.get("audio_path")
        if not raw:
            raise ApiError(
                "no_audio",
                "This sample gate records no audio path. The sample did not "
                "render, or the payload predates the audio field.",
                status=404,
            )
        path = Path(raw)
    finally:
        conn.close()
    return _stream_wav(path, request)


@router.get("/review/items/{item_id}/audio")
def get_review_item_audio(item_id: str, request: Request):
    """Stream the rendered chunk of a QC review item. `audio/wav`.

    Supports a `Range` request, so a browser `<audio>` element can seek.
    The API key travels in the `X-Api-Key` header, never in the URL.
    Refer to APP-CONTRACT.md section 10.1.
    """
    conn = connect()
    try:
        item_row = _require_item(conn, item_id)
        if item_row["kind"] != "qc_chunk":
            raise ApiError(
                "wrong_kind",
                "A homograph item serves audio at /audio/{n}.",
                status=404,
            )
        job_row = _get_job_row(conn, item_row["job_id"])
        path = _chunk_audio_path(job_row, item_row)
    finally:
        conn.close()
    return _stream_wav(path, request)


@router.get("/review/items/{item_id}/audio/{n}")
def get_review_item_candidate_audio(item_id: str, n: int, request: Request):
    """Stream candidate `n` of a homograph item. `audio/wav`.

    Pipeline contract 9.2 (via APP-CONTRACT.md 9.2): a person cannot
    choose a pronunciation from a phoneme string, so both candidates must
    play. `n` is 1-based, matching the order `candidates` holds them in.
    """
    conn = connect()
    try:
        item_row = _require_item(conn, item_id)
        if item_row["kind"] != "homograph_occurrence":
            raise ApiError(
                "wrong_kind",
                "A qc_chunk item serves audio at /audio, with no candidate number.",
                status=404,
            )
        candidates = json.loads(item_row["candidates"]) if item_row["candidates"] else []
        if n < 1 or n > len(candidates):
            raise ApiError(
                "candidate_not_found",
                f"This item holds {len(candidates)} candidate(s).",
                status=404,
            )
        audio_path = candidates[n - 1].get("audio")
        if not audio_path:
            raise ApiError("audio_not_found", "This candidate has no audio path.", status=404)
        path = Path(audio_path)
    finally:
        conn.close()
    return _stream_wav(path, request)
