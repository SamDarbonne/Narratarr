// Refer to APP-CONTRACT.md section 4.2: "progress_total of 0 means unknown.
// The user interface then shows an indeterminate bar, never a false
// percentage." This suite is the guard against that regression, and against
// a fake ETA ever appearing.

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ProgressBar, formatElapsed } from "./ProgressBar";

describe("ProgressBar", () => {
  it("renders an indeterminate bar and no percentage when total is 0", () => {
    render(<ProgressBar done={0} total={0} />);
    const bar = screen.getByRole("progressbar");
    expect(bar).toHaveClass("progress__track--indeterminate");
    expect(bar).not.toHaveAttribute("aria-valuenow");
    expect(screen.getByText(/progress unknown/i)).toBeInTheDocument();
    expect(screen.queryByText(/%/)).toBeNull();
  });

  it("never reports 100% just because total is 0 and done is also 0", () => {
    render(<ProgressBar done={0} total={0} />);
    expect(screen.queryByText("100%")).toBeNull();
  });

  it("renders a real percentage when total is greater than 0", () => {
    render(<ProgressBar done={340} total={812} />);
    const bar = screen.getByRole("progressbar");
    expect(bar).not.toHaveClass("progress__track--indeterminate");
    expect(bar).toHaveAttribute("aria-valuenow", "42");
    expect(screen.getByText("340 / 812 (42%)")).toBeInTheDocument();
  });

  it("caps the shown percentage at 100 even if done somehow exceeds total", () => {
    render(<ProgressBar done={900} total={812} />);
    expect(screen.getByText(/\(100%\)/)).toBeInTheDocument();
  });

  it("shows elapsed time, not a percentage-derived ETA", () => {
    render(<ProgressBar done={1} total={0} startedAt="20260816T100000Z" />);
    expect(screen.getByText(/elapsed/i)).toBeInTheDocument();
    expect(screen.queryByText(/eta/i)).toBeNull();
    expect(screen.queryByText(/remaining/i)).toBeNull();
  });
});

describe("formatElapsed", () => {
  it("formats minutes only under an hour", () => {
    const started = "20260816T100000Z";
    const now = new Date(Date.UTC(2026, 7, 16, 10, 32, 0));
    expect(formatElapsed(started, now)).toBe("32m elapsed");
  });

  it("formats hours and minutes at an hour or more", () => {
    const started = "20260816T020000Z";
    const now = new Date(Date.UTC(2026, 7, 16, 11, 30, 0));
    expect(formatElapsed(started, now)).toBe("9h 30m elapsed");
  });
});
