// Screen 6b: System status. Disk free, model presence, secret presence,
// queue depth, runner state. Refer to APP-CONTRACT.md section 13.1.

import { useMemo, useState } from "react";
import { getApiClient } from "../api";
import { usePolling } from "../hooks/usePolling";
import { formatBytes } from "../utils/time";

export function SystemStatus() {
  const client = useMemo(() => getApiClient(), []);
  const { data: status, refresh } = usePolling(() => client.systemStatus(), 5000);
  const [fetching, setFetching] = useState(false);

  async function fetchModels() {
    setFetching(true);
    try {
      await client.fetchModels();
      refresh();
    } finally {
      setFetching(false);
    }
  }

  if (!status) return <p className="muted">Loading system status…</p>;

  return (
    <div className="stack">
      <h1>System status</h1>

      <div className="grid-2">
        <div className="panel">
          <h2 style={{ fontSize: 15 }}>Runner</h2>
          <div className="badge-row">
            <span>
              State: <strong>{status.runner_state}</strong>
            </span>
            <span>
              Queue depth: <strong>{status.queue_depth}</strong>
            </span>
          </div>
        </div>
        <div className="panel">
          <h2 style={{ fontSize: 15 }}>Disk</h2>
          <p>
            Free: <strong>{formatBytes(status.disk_free_bytes)}</strong>
          </p>
        </div>
      </div>

      <div className="panel">
        <div className="panel__header">
          <h2 style={{ fontSize: 15, margin: 0 }}>Models</h2>
          <button className="btn" disabled={fetching} onClick={fetchModels}>
            {fetching ? "Starting…" : "Fetch missing models"}
          </button>
        </div>
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">Model</th>
              <th scope="col">Present</th>
              <th scope="col">Size</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(status.models ?? {}).map(([name, m]) => (
              <tr key={name}>
                <td>{name}</td>
                <td>
                  <span className={"pill " + (m.present ? "pill--ok" : "pill--warn")}>
                    {m.present ? "present" : "missing"}
                  </span>
                </td>
                <td>{m.size_bytes == null ? "—" : formatBytes(m.size_bytes)}</td>
              </tr>
            ))}
            {Object.keys(status.models ?? {}).length === 0 && (
              <tr>
                <td colSpan={3} className="muted">
                  No model is downloaded yet. The first render fetches them.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>


      <div className="panel">
        <h2 style={{ fontSize: 15 }}>Engine preflight</h2>
        <p className="muted">
          Narratarr checks the espeak fallback before it renders. When the fallback is
          absent, the text front end is built with an empty unknown-word symbol, and every
          word outside its lexicon is deleted from the audio in silence. Quality control
          cannot see that loss, because the transcript and the source lose the same word.
          Narratarr reads the engine object directly, because the library disables its own
          log.
        </p>
        {status.engine_preflight ? (
          <div className="badge-row">
            <span className={"pill " + (status.engine_preflight.espeak_fallback ? "pill--ok" : "pill--warn")}>
              espeak fallback: {status.engine_preflight.espeak_fallback ? "present" : "ABSENT"}
            </span>
            <span className={"pill " + (status.engine_preflight.oov_probe_nonempty ? "pill--ok" : "pill--warn")}>
              out-of-lexicon probe: {status.engine_preflight.oov_probe_nonempty ? "spoken" : "SILENT"}
            </span>
            <span>
              probe word: <strong>{status.engine_preflight.oov_probe_word}</strong>
            </span>
            <span>
              checked: <strong>{status.engine_preflight.checked_at}</strong>
            </span>
          </div>
        ) : (
          <p className="muted">No preflight has run yet.</p>
        )}
      </div>

      <div className="panel">
        <h2 style={{ fontSize: 15 }}>Secrets</h2>
        <p className="muted">A secret's value never appears here, only whether it is present.</p>
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">Environment variable</th>
              <th scope="col">Present</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(status.secrets ?? {}).map(([name, info]) => (
              <tr key={name}>
                <td>{name}</td>
                <td>
                  <span className={"pill " + (info.present ? "pill--ok" : "pill--warn")}>
                    {info.present ? "present" : "missing"}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
