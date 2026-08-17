// The accept-reason control.
//
// Warning: `accept` requires a reason. The API returns 422 on an empty
// reason, but this control must never let a person reach that error. The
// button stays disabled until the reason box holds real text. Refer to
// APP-CONTRACT.md section 9.3 and section 13.3.

import { useId, useState } from "react";

export interface AcceptWithReasonProps {
  onAccept: (reason: string) => void | Promise<void>;
  busy?: boolean;
  acceptLabel?: string;
}

export function AcceptWithReason({ onAccept, busy = false, acceptLabel = "Accept" }: AcceptWithReasonProps) {
  const [reason, setReason] = useState("");
  const reasonId = useId();
  const hasReason = reason.trim().length > 0;

  return (
    <div className="accept-with-reason">
      <label htmlFor={reasonId}>Reason (required)</label>
      <textarea
        id={reasonId}
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        placeholder="Why is this audio correct? A later reader needs to know."
        rows={3}
      />
      <button
        type="button"
        className="btn btn--primary"
        disabled={!hasReason || busy}
        onClick={() => {
          if (!hasReason) return;
          void onAccept(reason.trim());
        }}
      >
        {busy ? "Accepting…" : acceptLabel}
      </button>
    </div>
  );
}
