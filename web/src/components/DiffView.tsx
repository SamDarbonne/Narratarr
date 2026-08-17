// Renders the word-level diff of the source text against the transcript.
// Refer to APP-CONTRACT.md section 9.3. Colour alone never carries the
// meaning: a deleted word also gets strikethrough and the word "removed" in
// its title, an inserted word also gets underline and the word "added".

import { wordDiff } from "../utils/diff";

export interface DiffViewProps {
  source: string;
  transcript: string;
}

export function DiffView({ source, transcript }: DiffViewProps) {
  const tokens = wordDiff(source, transcript);
  return (
    <p className="diff" aria-label="Word-level diff of the source text against the transcript">
      {tokens.map((token, idx) => {
        if (token.op === "equal") {
          return <span key={idx}>{token.text} </span>;
        }
        if (token.op === "delete") {
          return (
            <span key={idx} className="diff__delete" title={`Removed from the transcript: "${token.text}"`}>
              {token.text}{" "}
            </span>
          );
        }
        return (
          <span key={idx} className="diff__insert" title={`Added by the transcript: "${token.text}"`}>
            {token.text}{" "}
          </span>
        );
      })}
    </p>
  );
}
