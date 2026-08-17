// A word-level diff, by longest common subsequence. Refer to
// APP-CONTRACT.md section 9.3: the QC gate shows the source text against the
// transcript as a word-level diff. This module holds the algorithm; the
// DiffView component holds the rendering.

export type DiffOp = "equal" | "delete" | "insert";

export interface DiffToken {
  op: DiffOp;
  text: string;
}

function tokenize(text: string): string[] {
  return text.trim().length === 0 ? [] : text.trim().split(/\s+/);
}

/**
 * Returns the word-level diff of `source` against `transcript`. A `delete`
 * token is a word the source held and the transcript dropped. An `insert`
 * token is a word the transcript added.
 */
export function wordDiff(source: string, transcript: string): DiffToken[] {
  const a = tokenize(source);
  const b = tokenize(transcript);
  const n = a.length;
  const m = b.length;

  // The LCS table. dp[i][j] is the LCS length of a[i:] and b[j:].
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }

  const tokens: DiffToken[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      tokens.push({ op: "equal", text: a[i] });
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      tokens.push({ op: "delete", text: a[i] });
      i++;
    } else {
      tokens.push({ op: "insert", text: b[j] });
      j++;
    }
  }
  while (i < n) {
    tokens.push({ op: "delete", text: a[i] });
    i++;
  }
  while (j < m) {
    tokens.push({ op: "insert", text: b[j] });
    j++;
  }
  return tokens;
}
