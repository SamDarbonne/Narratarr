// Screen 5: Targets. List, add, edit, test.
//
// Warning: a target's secret is never shown. The UI shows only the
// environment variable name and whether the secret is present. Refer to
// APP-CONTRACT.md section 8.2 and section 10.2.

import { useMemo, useState } from "react";
import { getApiClient } from "../api";
import type { DeliveryResult, Target, TargetKind } from "../api/types";
import { usePolling } from "../hooks/usePolling";

interface TargetFormState {
  name: string;
  kind: TargetKind;
  enabled: boolean;
  // folder
  root: string;
  layout: string;
  copy_cover: boolean;
  // audiobookshelf
  base_url: string;
  library_id: string;
  token_env: string;
  folder_target: string;
}

const EMPTY_FORM: TargetFormState = {
  name: "",
  kind: "folder",
  enabled: true,
  root: "/output",
  layout: "{author}/{title}/{title}.m4b",
  copy_cover: true,
  base_url: "http://audiobookshelf:13378",
  library_id: "",
  token_env: "NARRATARR_ABS_TOKEN",
  folder_target: "",
};

function toConfig(form: TargetFormState): Record<string, unknown> {
  if (form.kind === "folder") {
    return { root: form.root, layout: form.layout, copy_cover: form.copy_cover };
  }
  return {
    base_url: form.base_url,
    library_id: form.library_id,
    token_env: form.token_env,
    folder_target: form.folder_target,
  };
}

function SecretPresence({
  envName,
  secrets,
}: {
  envName: string;
  secrets: Record<string, { present: boolean }>;
}) {
  // The API keys secrets by their environment variable name. It reports
  // only whether each one is present, never its value.
  const secret = secrets[envName];
  const present = secret?.present ?? false;
  return (
    <span className={"pill " + (present ? "pill--ok" : "pill--warn")}>
      {envName}: {present ? "present" : "missing"}
    </span>
  );
}

export function Targets() {
  const client = useMemo(() => getApiClient(), []);
  const { data, refresh } = usePolling(() => client.listTargets(), 8000);
  const { data: status } = usePolling(() => client.systemStatus(), 8000);
  const [form, setForm] = useState<TargetFormState>(EMPTY_FORM);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<Record<string, DeliveryResult>>({});
  const [saving, setSaving] = useState(false);

  const targets = data?.items ?? [];
  const secrets = status?.secrets ?? {};

  function startEdit(target: Target) {
    setEditingId(target.id);
    if (target.kind === "folder") {
      setForm({
        ...EMPTY_FORM,
        name: target.name,
        kind: "folder",
        enabled: target.enabled,
        root: String(target.config.root ?? "/output"),
        layout: String(target.config.layout ?? EMPTY_FORM.layout),
        copy_cover: Boolean(target.config.copy_cover ?? true),
      });
    } else {
      setForm({
        ...EMPTY_FORM,
        name: target.name,
        kind: "audiobookshelf",
        enabled: target.enabled,
        base_url: String(target.config.base_url ?? EMPTY_FORM.base_url),
        library_id: String(target.config.library_id ?? ""),
        token_env: String(target.config.token_env ?? EMPTY_FORM.token_env),
        folder_target: String(target.config.folder_target ?? ""),
      });
    }
  }

  async function save() {
    setSaving(true);
    try {
      const body = { name: form.name, kind: form.kind, enabled: form.enabled, config: toConfig(form) };
      if (editingId) {
        await client.updateTarget(editingId, body);
      } else {
        await client.createTarget(body);
      }
      setForm(EMPTY_FORM);
      setEditingId(null);
      refresh();
    } finally {
      setSaving(false);
    }
  }

  async function remove(id: string) {
    await client.deleteTarget(id);
    refresh();
  }

  async function test(id: string) {
    const result = await client.testTarget(id);
    setTestResult((prev) => ({ ...prev, [id]: result }));
  }

  return (
    <div className="stack">
      <h1>Targets</h1>

      <table className="data-table">
        <thead>
          <tr>
            <th scope="col">Name</th>
            <th scope="col">Kind</th>
            <th scope="col">Enabled</th>
            <th scope="col">Secret</th>
            <th scope="col">Test</th>
            <th scope="col"></th>
          </tr>
        </thead>
        <tbody>
          {targets.map((target) => (
            <tr key={target.id}>
              <td>{target.name}</td>
              <td>{target.kind}</td>
              <td>{target.enabled ? "yes" : "no"}</td>
              <td>
                {target.kind === "audiobookshelf" ? (
                  <SecretPresence envName={String(target.config.token_env)} secrets={secrets} />
                ) : (
                  <span className="muted">none needed</span>
                )}
              </td>
              <td>
                <button className="btn" onClick={() => test(target.id)}>
                  Test
                </button>
                {testResult[target.id] && (
                  <span className={"pill " + (testResult[target.id].ok ? "pill--ok" : "pill--error")} style={{ marginLeft: 8 }}>
                    {testResult[target.id].ok ? "reachable" : "unreachable"}
                  </span>
                )}
              </td>
              <td>
                <div className="btn-row">
                  <button className="btn" onClick={() => startEdit(target)}>
                    Edit
                  </button>
                  <button className="btn btn--danger" onClick={() => remove(target.id)}>
                    Delete
                  </button>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="panel">
        <h2 style={{ fontSize: 15 }}>{editingId ? "Edit target" : "Add target"}</h2>
        <div className="grid-2">
          <div className="field">
            <label htmlFor="target-name">Name</label>
            <input
              id="target-name"
              type="text"
              value={form.name}
              onChange={(e) => setForm((s) => ({ ...s, name: e.target.value }))}
            />
          </div>
          <div className="field">
            <label htmlFor="target-kind">Kind</label>
            <select
              id="target-kind"
              value={form.kind}
              onChange={(e) => setForm((s) => ({ ...s, kind: e.target.value as TargetKind }))}
            >
              <option value="folder">folder</option>
              <option value="audiobookshelf">audiobookshelf</option>
            </select>
          </div>
        </div>

        {form.kind === "folder" ? (
          <div className="grid-2">
            <div className="field">
              <label htmlFor="target-root">root</label>
              <input
                id="target-root"
                type="text"
                value={form.root}
                onChange={(e) => setForm((s) => ({ ...s, root: e.target.value }))}
              />
            </div>
            <div className="field">
              <label htmlFor="target-layout">layout</label>
              <input
                id="target-layout"
                type="text"
                value={form.layout}
                onChange={(e) => setForm((s) => ({ ...s, layout: e.target.value }))}
              />
            </div>
          </div>
        ) : (
          <div className="grid-2">
            <div className="field">
              <label htmlFor="target-base-url">base_url</label>
              <input
                id="target-base-url"
                type="text"
                value={form.base_url}
                onChange={(e) => setForm((s) => ({ ...s, base_url: e.target.value }))}
              />
            </div>
            <div className="field">
              <label htmlFor="target-library-id">library_id</label>
              <input
                id="target-library-id"
                type="text"
                value={form.library_id}
                onChange={(e) => setForm((s) => ({ ...s, library_id: e.target.value }))}
              />
            </div>
            <div className="field">
              <label htmlFor="target-token-env">
                token_env — the environment variable name only. The token itself is never
                entered here.
              </label>
              <input
                id="target-token-env"
                type="text"
                value={form.token_env}
                onChange={(e) => setForm((s) => ({ ...s, token_env: e.target.value }))}
              />
            </div>
            <div className="field">
              <label htmlFor="target-folder-target">folder_target id</label>
              <input
                id="target-folder-target"
                type="text"
                value={form.folder_target}
                onChange={(e) => setForm((s) => ({ ...s, folder_target: e.target.value }))}
              />
            </div>
          </div>
        )}

        <div className="btn-row">
          <button className="btn btn--primary" disabled={saving || !form.name} onClick={save}>
            {saving ? "Saving…" : editingId ? "Save changes" : "Add target"}
          </button>
          {editingId && (
            <button
              className="btn"
              onClick={() => {
                setEditingId(null);
                setForm(EMPTY_FORM);
              }}
            >
              Cancel edit
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
