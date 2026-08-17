// The QC gate item. Refer to APP-CONTRACT.md section 9.3 and section 4.4.
//
// Warning: `accept` requires a reason. The button stays disabled until the
// reason box holds real text. Refer to the AcceptWithReason component.

import { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import type { ApiClient } from "../../api/client";
import type { ReviewItem } from "../../api/types";
import { AudioPlayer } from "../../components/AudioPlayer";
import { DiffView } from "../../components/DiffView";
import { AcceptWithReason } from "../../components/AcceptWithReason";
import { VoidedPinNotice } from "../../components/VoidedPinNotice";

export interface QcGateItemProps {
  client: ApiClient;
  item: ReviewItem;
  jobId: string;
  onResolved: () => void;
}

export function QcGateItemRow({ client, item, jobId, onResolved }: QcGateItemProps) {
  const [busy, setBusy] = useState(false);
  const fetcher = useCallback(() => client.getReviewItemAudio(item.id), [client, item.id]);

  async function accept(reason: string) {
    setBusy(true);
    try {
      await client.acceptReviewItem(item.id, reason);
      onResolved();
    } finally {
      setBusy(false);
    }
  }

  async function rerender() {
    setBusy(true);
    try {
      await client.rerenderReviewItem(item.id);
      onResolved();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="panel">
      <div className="panel__header">
        <h3 style={{ margin: 0 }}>
          {item.chapter} / {item.chunk}
        </h3>
        <Link to={`/jobs/${jobId}/config`} className="btn">
          Edit config
        </Link>
      </div>

      {item.state === "voided" && <VoidedPinNotice priorReason={item.reason} />}

      <div className="badge-row" style={{ margin: "8px 0" }}>
        <span>
          WER: <strong>{item.wer !== null ? item.wer.toFixed(2) : "—"}</strong>
        </span>
        <span>
          Coverage: <strong>{item.coverage !== null ? item.coverage.toFixed(2) : "—"}</strong>
        </span>
        <span>
          Duration: <strong>{item.duration_s !== null ? `${item.duration_s.toFixed(1)}s` : "—"}</strong>
        </span>
      </div>

      {item.flags.length > 0 && (
        <div style={{ marginBottom: 8 }}>
          {item.flags.map((f) => (
            <span key={f} className="flag">
              {f}
            </span>
          ))}
        </div>
      )}

      {item.source_text !== null && item.transcript !== null && (
        <DiffView source={item.source_text} transcript={item.transcript} />
      )}

      <div style={{ marginTop: 12 }}>
        <AudioPlayer label="Rendered chunk" fetcher={fetcher} />
      </div>

      {(item.state === "open" || item.state === "voided") && (
        <div className="grid-2" style={{ marginTop: 12 }}>
          <AcceptWithReason onAccept={accept} busy={busy} />
          <div className="btn-row" style={{ alignItems: "flex-start" }}>
            <button type="button" className="btn" disabled={busy} onClick={rerender}>
              {busy ? "Working…" : "Re-render"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
