// The voided-pin notice.
//
// Warning: this is the most important text in the application. Refer to
// APP-CONTRACT.md section 9.3 and vendor/abpipe/CONTRACT.md section 9.7.
// Kokoro is not deterministic. A re-render always changes the bytes, even
// when the words, the length, and the reading are the same. A voided pin is
// therefore NOT evidence that the audio changed. It means only that a
// person judged this text's audio acceptable once. A reader who takes the
// wrong lesson here will distrust the whole tool, so this copy is exact and
// never abbreviated elsewhere in the application.

export interface VoidedPinNoticeProps {
  priorReason: string | null;
}

export function VoidedPinNotice({ priorReason }: VoidedPinNoticeProps) {
  return (
    <div className="callout callout--warn" role="note" aria-label="Voided acceptance notice">
      <p>
        <strong>This acceptance is voided.</strong> A person accepted this chunk once, but the
        chunk was re-rendered since. Every acceptance voids on every re-render, always.
      </p>
      <p>
        Kokoro is not deterministic. A re-render always changes the bytes, even when the
        words, the length, and the reading stay exactly the same.
      </p>
      <p>
        A voided pin is not evidence that the audio changed. It means only that a person
        judged this text&apos;s audio acceptable once, on a version of the bytes that no
        longer exists. Listen to the current audio and decide again.
      </p>
      {priorReason && (
        <p className="muted">
          Prior reason: <em>&ldquo;{priorReason}&rdquo;</em>
        </p>
      )}
    </div>
  );
}
