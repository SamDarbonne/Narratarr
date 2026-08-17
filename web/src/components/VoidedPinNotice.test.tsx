// Refer to APP-CONTRACT.md section 9.3 and vendor/abpipe/CONTRACT.md
// section 9.7: the voided-pin copy is the most important text in this
// application. This test locks the load-bearing claims down so a future
// edit cannot soften them by accident.

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { VoidedPinNotice } from "./VoidedPinNotice";

describe("VoidedPinNotice", () => {
  it("states plainly that Kokoro is not deterministic", () => {
    render(<VoidedPinNotice priorReason={null} />);
    expect(screen.getByText(/Kokoro is not deterministic/i)).toBeInTheDocument();
  });

  it("states that a re-render always changes the bytes", () => {
    render(<VoidedPinNotice priorReason={null} />);
    expect(screen.getByText(/re-render always changes the bytes/i)).toBeInTheDocument();
  });

  it("states that a voided pin is NOT evidence the audio changed", () => {
    render(<VoidedPinNotice priorReason={null} />);
    expect(screen.getByText(/not evidence that the audio changed/i)).toBeInTheDocument();
  });

  it("states that a voided pin means only that a person judged the audio acceptable once", () => {
    render(<VoidedPinNotice priorReason={null} />);
    expect(
      screen.getByText(/means only that a person\s+judged this text.s audio acceptable once/i),
    ).toBeInTheDocument();
  });

  it("shows the prior reason when one is given, for the record's own value", () => {
    render(<VoidedPinNotice priorReason="Whisper garbles the proper noun." />);
    expect(screen.getByText(/Whisper garbles the proper noun\./)).toBeInTheDocument();
  });

  it("shows no prior-reason line when none is given", () => {
    render(<VoidedPinNotice priorReason={null} />);
    expect(screen.queryByText(/Prior reason/)).toBeNull();
  });
});
