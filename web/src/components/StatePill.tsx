// The job state pill. A gate state (a book that waits for a person) is the
// single most important thing on the Library screen, so a gate pill carries
// its own icon and a "waiting" label, not colour alone. Refer to
// APP-CONTRACT.md section 5 and section 9.

import type { JobState } from "../api/types";
import { GATE_STATES } from "../api/types";

const LABELS: Record<JobState, string> = {
  queued: "Queued",
  running: "Running",
  awaiting_sample_approval: "Waiting: sample",
  awaiting_homograph_review: "Waiting: homograph",
  awaiting_qc_review: "Waiting: QC",
  delivering: "Delivering",
  done: "Done",
  failed: "Failed",
  cancelled: "Cancelled",
  paused: "Paused",
};

const CLASS: Record<JobState, string> = {
  queued: "pill pill--neutral",
  running: "pill pill--active",
  awaiting_sample_approval: "pill pill--gate",
  awaiting_homograph_review: "pill pill--gate",
  awaiting_qc_review: "pill pill--gate",
  delivering: "pill pill--active",
  done: "pill pill--ok",
  failed: "pill pill--error",
  cancelled: "pill pill--neutral",
  paused: "pill pill--warn",
};

export interface StatePillProps {
  state: JobState;
}

export function StatePill({ state }: StatePillProps) {
  const isGate = (GATE_STATES as JobState[]).includes(state);
  return (
    <span className={CLASS[state]} data-state={state}>
      {isGate && (
        <span className="pill__icon" aria-hidden="true">
          ●
        </span>
      )}
      {LABELS[state]}
    </span>
  );
}
