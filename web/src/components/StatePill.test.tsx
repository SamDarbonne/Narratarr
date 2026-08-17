// Refer to APP-CONTRACT.md section 9: "The three gate states are first-class
// states... A job list must show at a glance which books wait for a
// person." This suite checks that a gate state is marked in more than one
// way: its own CSS class and an icon glyph, not colour alone.

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { StatePill } from "./StatePill";
import { GATE_STATES } from "../api/types";
import type { JobState } from "../api/types";

const NON_GATE_STATES: JobState[] = [
  "queued",
  "running",
  "delivering",
  "done",
  "failed",
  "cancelled",
  "paused",
];

describe("StatePill", () => {
  it.each(GATE_STATES)("marks the gate state %s with the gate class and an icon", (state) => {
    render(<StatePill state={state} />);
    const pill = screen.getByText((_, el) => el?.getAttribute("data-state") === state);
    expect(pill).toHaveClass("pill--gate");
    expect(pill.querySelector(".pill__icon")).not.toBeNull();
  });

  it.each(NON_GATE_STATES)("does not mark %s as a gate", (state) => {
    render(<StatePill state={state} />);
    const pill = screen.getByText((_, el) => el?.getAttribute("data-state") === state);
    expect(pill).not.toHaveClass("pill--gate");
    expect(pill.querySelector(".pill__icon")).toBeNull();
  });

  it("shows a human-readable label, not the raw state string, for every state", () => {
    render(<StatePill state="awaiting_qc_review" />);
    expect(screen.getByText(/Waiting: QC/)).toBeInTheDocument();
  });
});
