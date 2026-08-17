// Time formatting helpers over the contract's UTC stamp form,
// `YYYYMMDDThhmmssZ`. Refer to APP-CONTRACT.md section 4.2.

export function parseStamp(stamp: string | null | undefined): Date | null {
  if (!stamp) return null;
  const m = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/.exec(stamp);
  if (!m) return null;
  const [, y, mo, d, h, mi, s] = m;
  return new Date(Date.UTC(Number(y), Number(mo) - 1, Number(d), Number(h), Number(mi), Number(s)));
}

/** The age of a job, in a short human form: "2h", "3d", "just now". */
export function formatAge(stamp: string | null | undefined, now: Date = new Date()): string {
  const then = parseStamp(stamp);
  if (!then) return "—";
  const seconds = Math.max(0, Math.floor((now.getTime() - then.getTime()) / 1000));
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.floor(hours / 24);
  return `${days}d`;
}

export function formatStamp(stamp: string | null | undefined): string {
  const d = parseStamp(stamp);
  if (!d) return "—";
  return d.toISOString().replace("T", " ").replace(/\.\d+Z$/, "Z");
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit++;
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}
