// Refer to APP-CONTRACT.md section 9.3: the QC gate shows the source text
// against the transcript as a word-level diff.

import { describe, expect, it } from "vitest";
import { wordDiff } from "./diff";

describe("wordDiff", () => {
  it("marks every token equal for an identical pair", () => {
    const tokens = wordDiff("the quick brown fox", "the quick brown fox");
    expect(tokens.every((t) => t.op === "equal")).toBe(true);
    expect(tokens.map((t) => t.text)).toEqual(["the", "quick", "brown", "fox"]);
  });

  it("finds a single-word substitution as a delete and an insert", () => {
    const tokens = wordDiff("the quick brown fox", "the quick red fox");
    expect(tokens).toEqual([
      { op: "equal", text: "the" },
      { op: "equal", text: "quick" },
      { op: "delete", text: "brown" },
      { op: "insert", text: "red" },
      { op: "equal", text: "fox" },
    ]);
  });

  it("finds the real fault: 'You Connor' misheard as 'Ucona'", () => {
    // Refer to vendor/abpipe/CONTRACT.md section 9.7's own example.
    const tokens = wordDiff(
      "Whisper garbles the proper noun You Connor as its own guess.",
      "Whisper garbles the proper noun Ucona as its own guess.",
    );
    const deletes = tokens.filter((t) => t.op === "delete").map((t) => t.text);
    const inserts = tokens.filter((t) => t.op === "insert").map((t) => t.text);
    expect(deletes).toEqual(["You", "Connor"]);
    expect(inserts).toEqual(["Ucona"]);
  });

  it("marks a whole extra word as an insert with no source counterpart", () => {
    const tokens = wordDiff("chapter one", "chapter one stir stir stir");
    expect(tokens.filter((t) => t.op === "insert").map((t) => t.text)).toEqual(["stir", "stir", "stir"]);
  });

  it("marks a dropped word as a delete with no transcript counterpart", () => {
    const tokens = wordDiff("Gyko!", "");
    expect(tokens).toEqual([{ op: "delete", text: "Gyko!" }]);
  });

  it("handles two empty strings with no tokens", () => {
    expect(wordDiff("", "")).toEqual([]);
  });
});
