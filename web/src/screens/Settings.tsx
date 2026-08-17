// Screen 6a: Settings. The operational switches of APP-CONTRACT.md section
// 10, the API keys of section 4.7, and the watch folder of section 7.

import { useEffect, useMemo, useState } from "react";
import { getApiClient } from "../api";
import { usePolling } from "../hooks/usePolling";
import { formatBytes, formatStamp } from "../utils/time";

interface SettingsForm {
  sample_gate: boolean;
  prune: boolean;
  watch_delete_after_ingest: boolean;
  watch_interval_s: number;
  events_per_job_max: number;
}

export function Settings() {
  const client = useMemo(() => getApiClient(), []);
  const [form, setForm] = useState<SettingsForm | null>(null);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(null);

  const { data: keys, refresh: refreshKeys } = usePolling(() => client.listKeys(), 15000);
  const { data: library, refresh: refreshLibrary } = usePolling(() => client.listLibrary(), 15000);
  const [newKeyName, setNewKeyName] = useState("");
  const [createdKey, setCreatedKey] = useState<string | null>(null);
  const [scanning, setScanning] = useState(false);

  useEffect(() => {
    client.getSettings().then((s) => {
      setForm({
        sample_gate: Boolean(s.sample_gate ?? true),
        prune: Boolean(s.prune ?? false),
        watch_delete_after_ingest: Boolean(s.watch_delete_after_ingest ?? false),
        watch_interval_s: Number(s.watch_interval_s ?? 60),
        events_per_job_max: Number(s.events_per_job_max ?? 5000),
      });
    });
  }, [client]);

  async function save() {
    if (!form) return;
    setSaving(true);
    try {
      await client.putSettings({ ...form });
      setSavedAt(Date.now());
    } finally {
      setSaving(false);
    }
  }

  async function createKey() {
    if (!newKeyName.trim()) return;
    const created = await client.createKey(newKeyName.trim());
    setCreatedKey(created.key);
    setNewKeyName("");
    refreshKeys();
  }

  async function deleteKey(id: string) {
    await client.deleteKey(id);
    refreshKeys();
  }

  async function scan() {
    setScanning(true);
    try {
      await client.scanLibrary();
      refreshLibrary();
    } finally {
      setScanning(false);
    }
  }

  return (
    <div className="stack">
      <h1>Settings</h1>

      {form && (
        <div className="panel">
          <h2 style={{ fontSize: 15 }}>Runner</h2>
          <div className="field">
            <label htmlFor="sample-gate">
              <input
                id="sample-gate"
                type="checkbox"
                style={{ width: "auto", marginRight: 6 }}
                checked={form.sample_gate}
                onChange={(e) => setForm((s) => (s ? { ...s, sample_gate: e.target.checked } : s))}
              />
              Sample gate — stop and wait for a person to approve the sample passage.
              On by default. Refer to APP-CONTRACT section 9.1.
            </label>
          </div>
          <div className="field">
            <label htmlFor="prune">
              <input
                id="prune"
                type="checkbox"
                style={{ width: "auto", marginRight: 6 }}
                checked={form.prune}
                onChange={(e) => setForm((s) => (s ? { ...s, prune: e.target.checked } : s))}
              />
              Prune chunk audio after delivery. Off by default. A pruned chapter defeats
              the Fix flow — a one-chunk fix then costs a whole chapter.
            </label>
          </div>
          <div className="field">
            <label htmlFor="watch-delete">
              <input
                id="watch-delete"
                type="checkbox"
                style={{ width: "auto", marginRight: 6 }}
                checked={form.watch_delete_after_ingest}
                onChange={(e) =>
                  setForm((s) => (s ? { ...s, watch_delete_after_ingest: e.target.checked } : s))
                }
              />
              Delete a file from the watch folder after ingest.
            </label>
          </div>
          <div className="grid-2">
            <div className="field">
              <label htmlFor="watch-interval">Watch interval, seconds</label>
              <input
                id="watch-interval"
                type="number"
                value={form.watch_interval_s}
                onChange={(e) =>
                  setForm((s) => (s ? { ...s, watch_interval_s: Number(e.target.value) } : s))
                }
              />
            </div>
            <div className="field">
              <label htmlFor="events-max">Events per job, maximum</label>
              <input
                id="events-max"
                type="number"
                value={form.events_per_job_max}
                onChange={(e) =>
                  setForm((s) => (s ? { ...s, events_per_job_max: Number(e.target.value) } : s))
                }
              />
            </div>
          </div>
          <div className="btn-row">
            <button className="btn btn--primary" disabled={saving} onClick={save}>
              {saving ? "Saving…" : "Save"}
            </button>
            {savedAt && <span className="muted">Saved.</span>}
          </div>
        </div>
      )}

      <div className="panel">
        <h2 style={{ fontSize: 15 }}>API keys</h2>
        <p className="muted">A key's value shows once, at the moment it is made, and never again.</p>
        {createdKey && (
          <div className="callout callout--warn" role="alert">
            <p>
              New key, shown once — copy it now: <code>{createdKey}</code>
            </p>
          </div>
        )}
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col">Created</th>
              <th scope="col">Last used</th>
              <th scope="col"></th>
            </tr>
          </thead>
          <tbody>
            {(keys?.items ?? []).map((k) => (
              <tr key={k.id}>
                <td>{k.name}</td>
                <td>{formatStamp(k.created_at)}</td>
                <td>{formatStamp(k.last_used_at)}</td>
                <td>
                  <button className="btn btn--danger" onClick={() => deleteKey(k.id)}>
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="btn-row" style={{ marginTop: 12 }}>
          <input
            type="text"
            placeholder="Key name"
            value={newKeyName}
            onChange={(e) => setNewKeyName(e.target.value)}
            style={{ maxWidth: 220 }}
          />
          <button className="btn" disabled={!newKeyName.trim()} onClick={createKey}>
            Create key
          </button>
        </div>
      </div>

      <div className="panel">
        <div className="panel__header">
          <h2 style={{ fontSize: 15, margin: 0 }}>Library</h2>
          <button className="btn" disabled={scanning} onClick={scan}>
            {scanning ? "Scanning…" : "Scan watch folder now"}
          </button>
        </div>
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">Path</th>
              <th scope="col">Size</th>
              <th scope="col">Ingested</th>
            </tr>
          </thead>
          <tbody>
            {(library?.items ?? []).map((f) => (
              <tr key={f.path}>
                <td className="muted">{f.path}</td>
                <td>{formatBytes(f.size_bytes)}</td>
                <td>{f.ingested ? "yes" : "no"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
