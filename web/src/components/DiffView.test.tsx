import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DiffView } from "./DiffView";

describe("DiffView", () => {
  it("renders a deleted word with strikethrough styling and an insert with underline styling", () => {
    render(<DiffView source="the quick brown fox" transcript="the quick red fox" />);
    const deleted = screen.getByTitle('Removed from the transcript: "brown"');
    const inserted = screen.getByTitle('Added by the transcript: "red"');
    expect(deleted).toHaveClass("diff__delete");
    expect(inserted).toHaveClass("diff__insert");
  });

  it("conveys the diff by more than colour: each op gets its own class and its own title text", () => {
    // index.css gives .diff__delete a strikethrough and .diff__insert an
    // underline, on top of colour. jsdom does not load that stylesheet in a
    // component test, so this test locks the two signals it can see here:
    // a distinct class per op, and a distinct, readable title per op.
    render(<DiffView source="Gyko!" transcript="jayu" />);
    const deleted = screen.getByTitle('Removed from the transcript: "Gyko!"');
    const inserted = screen.getByTitle('Added by the transcript: "jayu"');
    expect(deleted.className).not.toBe(inserted.className);
    expect(deleted).toHaveClass("diff__delete");
    expect(inserted).toHaveClass("diff__insert");
    expect(deleted.getAttribute("title")).toMatch(/^Removed/);
    expect(inserted.getAttribute("title")).toMatch(/^Added/);
  });

  it("renders an identical pair with no delete or insert element", () => {
    render(<DiffView source="chapter one" transcript="chapter one" />);
    expect(screen.queryByTitle(/Removed from the transcript/)).toBeNull();
    expect(screen.queryByTitle(/Added by the transcript/)).toBeNull();
  });
});
