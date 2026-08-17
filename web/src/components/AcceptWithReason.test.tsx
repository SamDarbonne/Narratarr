// Refer to APP-CONTRACT.md section 9.3: "accept requires a reason. The
// button stays disabled until the reason box holds real text." This test
// suite is the guard against ever weakening that gate.

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AcceptWithReason } from "./AcceptWithReason";

describe("AcceptWithReason", () => {
  it("disables the accept button when the reason box is empty", () => {
    render(<AcceptWithReason onAccept={vi.fn()} />);
    expect(screen.getByRole("button", { name: /accept/i })).toBeDisabled();
  });

  it("stays disabled when the reason box holds only whitespace", () => {
    render(<AcceptWithReason onAccept={vi.fn()} />);
    const textarea = screen.getByLabelText(/reason/i);
    fireEvent.change(textarea, { target: { value: "   \n  " } });
    expect(screen.getByRole("button", { name: /accept/i })).toBeDisabled();
  });

  it("enables the accept button once the reason box holds real text", () => {
    render(<AcceptWithReason onAccept={vi.fn()} />);
    const textarea = screen.getByLabelText(/reason/i);
    fireEvent.change(textarea, { target: { value: "The audio is correct." } });
    expect(screen.getByRole("button", { name: /accept/i })).toBeEnabled();
  });

  it("disables again after the reason is cleared", () => {
    render(<AcceptWithReason onAccept={vi.fn()} />);
    const textarea = screen.getByLabelText(/reason/i);
    fireEvent.change(textarea, { target: { value: "A real reason." } });
    expect(screen.getByRole("button", { name: /accept/i })).toBeEnabled();
    fireEvent.change(textarea, { target: { value: "" } });
    expect(screen.getByRole("button", { name: /accept/i })).toBeDisabled();
  });

  it("calls onAccept with the trimmed reason, and only on a click", () => {
    const onAccept = vi.fn();
    render(<AcceptWithReason onAccept={onAccept} />);
    const textarea = screen.getByLabelText(/reason/i);
    fireEvent.change(textarea, { target: { value: "  Whisper garbles the name.  " } });
    expect(onAccept).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /accept/i }));
    expect(onAccept).toHaveBeenCalledWith("Whisper garbles the name.");
  });

  it("never calls onAccept from a disabled click attempt", () => {
    const onAccept = vi.fn();
    render(<AcceptWithReason onAccept={onAccept} />);
    fireEvent.click(screen.getByRole("button", { name: /accept/i }));
    expect(onAccept).not.toHaveBeenCalled();
  });

  it("disables the button while busy, even with a valid reason", () => {
    render(<AcceptWithReason onAccept={vi.fn()} busy />);
    const textarea = screen.getByLabelText(/reason/i);
    fireEvent.change(textarea, { target: { value: "A real reason." } });
    expect(screen.getByRole("button", { name: /accepting/i })).toBeDisabled();
  });
});
