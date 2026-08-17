// The sample gate item. Refer to APP-CONTRACT.md section 9.1.
//
// Note on the audio route: refer to the ASSUMPTION docstring on
// ApiClient.getGateAudio in web/src/api/client.ts. Section 13.3 names no
// route for this audio.

import { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import type { ApiClient } from "../../api/client";
import type { Gate } from "../../api/types";
import { AudioPlayer } from "../../components/AudioPlayer";

export interface SampleGateItemProps {
  client: ApiClient;
  gate: Gate;
  onResolved: () => void;
}

export function SampleGateItem({ client, gate, onResolved }: SampleGateItemProps) {
  const [busy, setBusy] = useState<string | null>(null);
  const fetcher = useCallback(() => client.getGateAudio(gate.id), [client, gate.id]);

  async function resolve(resolution: "approved" | "rejected") {
    setBusy(resolution);
    try {
      await client.resolveGate(gate.id, resolution);
      onResolved();
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="panel panel--gate">
      <div className="panel__header">
        <div>
          <h3 style={{ margin: 0 }}>{gate.job_title ?? gate.job_slug ?? gate.job_id}</h3>
          <p className="muted" style={{ margin: 0 }}>
            Sample gate
            {gate.payload.chapter ? ` · chapter ${String(gate.payload.chapter)}` : ""}
          </p>
        </div>
      </div>
      <p className="muted">
        This passage was picked for its hazards: the worst proper noun, a foreign term, a
        number, and a caps run. Listen for a wrong reading, not for the prose.
      </p>
      <AudioPlayer label="Sample passage" fetcher={fetcher} />
      <div className="btn-row" style={{ marginTop: 12 }}>
        <button className="btn btn--primary" disabled={busy !== null} onClick={() => resolve("approved")}>
          {busy === "approved" ? "Approving…" : "Approve"}
        </button>
        <button className="btn btn--danger" disabled={busy !== null} onClick={() => resolve("rejected")}>
          {busy === "rejected" ? "Rejecting…" : "Reject"}
        </button>
        <Link to={`/jobs/${gate.job_id}/config`} className="btn">
          Edit config
        </Link>
      </div>
    </div>
  );
}
