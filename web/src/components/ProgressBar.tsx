// The progress bar.
//
// Warning: `progress_total` of 0 means unknown. This component renders an
// indeterminate bar in that case, and never a false percentage. Refer to
// APP-CONTRACT.md section 4.2 and the frontend brief.
//
// This component never shows an ETA. A book takes between one night and one
// day on the target hardware, and no formula here can shorten that honestly.
// It shows elapsed time only.

export interface ProgressBarProps {
  done: number;
  total: number;
  /** ISO-ish UTC stamp of when the stage started, for the elapsed-time label. */
  startedAt?: string | null;
}

function parseStamp(stamp: string): Date | null {
  const m = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/.exec(stamp);
  if (!m) return null;
  const [, y, mo, d, h, mi, s] = m;
  return new Date(Date.UTC(Number(y), Number(mo) - 1, Number(d), Number(h), Number(mi), Number(s)));
}

export function formatElapsed(startedAt: string, now: Date = new Date()): string {
  const start = parseStamp(startedAt);
  if (!start) return "";
  const totalSeconds = Math.max(0, Math.floor((now.getTime() - start.getTime()) / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  if (hours > 0) return `${hours}h ${minutes}m elapsed`;
  return `${minutes}m elapsed`;
}

export function ProgressBar({ done, total, startedAt }: ProgressBarProps) {
  const indeterminate = total === 0;
  const pct = indeterminate ? 0 : Math.min(100, Math.round((done / total) * 100));

  return (
    <div className="progress">
      <div
        className={indeterminate ? "progress__track progress__track--indeterminate" : "progress__track"}
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={indeterminate ? undefined : 100}
        aria-valuenow={indeterminate ? undefined : pct}
        aria-label={indeterminate ? "Progress unknown" : `${pct} percent complete`}
      >
        {!indeterminate && <div className="progress__fill" style={{ width: `${pct}%` }} />}
      </div>
      <div className="progress__caption">
        {indeterminate ? <span>Progress unknown</span> : <span>{`${done} / ${total} (${pct}%)`}</span>}
        {startedAt && <span className="progress__elapsed">{formatElapsed(startedAt)}</span>}
      </div>
    </div>
  );
}
