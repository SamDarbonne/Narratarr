// The homograph gate item. Refer to APP-CONTRACT.md section 9.2.
//
// Warning: a person cannot choose a pronunciation from a phoneme string. The
// user interface plays BOTH candidate readings. This is the whole reason the
// gate exists.

import { useCallback, useState } from "react";
import type { ApiClient } from "../../api/client";
import type { HomographCandidate, ReviewItem } from "../../api/types";
import { AudioPlayer } from "../../components/AudioPlayer";

export interface HomographItemRowProps {
  client: ApiClient;
  item: ReviewItem;
  onResolved: () => void;
}

/** One candidate's audio player. A separate component keeps the fetcher's
 * `useCallback` at a stable position, one hook call per candidate instance,
 * never inside a loop in the parent's own render. */
function CandidatePlayer({
  client,
  itemId,
  index,
  candidate,
}: {
  client: ApiClient;
  itemId: string;
  index: number;
  candidate: HomographCandidate;
}) {
  const fetcher = useCallback(
    () => client.getReviewItemCandidateAudio(itemId, index),
    [client, itemId, index],
  );
  return <AudioPlayer label={`"${candidate.reading}" candidate`} fetcher={fetcher} />;
}

export function HomographItemRow({ client, item, onResolved }: HomographItemRowProps) {
  const [busy, setBusy] = useState<string | null>(null);
  const candidates = item.candidates ?? [];

  async function choose(reading: string) {
    setBusy(reading);
    try {
      await client.resolveHomograph(item.id, reading);
      onResolved();
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="panel">
      <h3 style={{ margin: "0 0 4px", fontFamily: "ui-monospace, monospace" }}>
        &ldquo;{item.word}&rdquo;
        <span className="muted" style={{ fontFamily: "inherit", fontWeight: 400, marginLeft: 8 }}>
          {item.chapter} · occurrence {item.occurrence}
        </span>
      </h3>
      {item.context && <p className="diff">{item.context}</p>}
      <div className="grid-2">
        {candidates.map((cand, idx) => (
          <div key={cand.reading} className="panel" style={{ marginBottom: 0 }}>
            <p style={{ margin: "0 0 8px" }}>
              <strong>{cand.reading}</strong>{" "}
              <span className="muted" style={{ fontFamily: "ui-monospace, monospace" }}>
                /{cand.phonemes}/
              </span>
            </p>
            <CandidatePlayer client={client} itemId={item.id} index={idx + 1} candidate={cand} />
            <button
              type="button"
              className="btn btn--primary"
              disabled={busy !== null}
              onClick={() => choose(cand.reading)}
            >
              {busy === cand.reading ? "Choosing…" : `Choose "${cand.reading}"`}
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
