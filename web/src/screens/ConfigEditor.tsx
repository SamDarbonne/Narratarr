// Screen 4: Configuration editor. Edits a job's book config and QC config.
// Refer to APP-CONTRACT.md section 13.2 and vendor/abpipe/CONTRACT.md
// sections 4.1 and 9.2.
//
// Warning: never let the UI widen a QC threshold as a way to clear a red
// gate. Pipeline contract section 9.2 forbids it outright: "a threshold is
// never widened to turn a red gate green." The warning sits beside every
// threshold field below.

import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { getApiClient } from "../api";
import type { ApiClientError } from "../api/types";

interface QcFields {
  wer_max: number;
  coverage_min: number;
  token_similarity_min: number;
  duration_outlier_factor: number;
  min_tokens_for_wer: number;
  min_chars_for_duration_test: number;
  max_token_repeat: number;
  whisper_model: string;
  condition_on_previous_text: boolean;
  equivalences: string;
}

const QC_DEFAULTS: QcFields = {
  wer_max: 0.15,
  coverage_min: 0.9,
  token_similarity_min: 0.85,
  duration_outlier_factor: 3.0,
  min_tokens_for_wer: 8,
  min_chars_for_duration_test: 15,
  max_token_repeat: 2,
  whisper_model: "",
  condition_on_previous_text: false,
  equivalences: "{}",
};

function toQcFields(qcConfig: Record<string, unknown>): QcFields {
  const g = <T,>(key: string, fallback: T): T => (qcConfig[key] as T) ?? fallback;
  return {
    wer_max: g("wer_max", QC_DEFAULTS.wer_max),
    coverage_min: g("coverage_min", QC_DEFAULTS.coverage_min),
    token_similarity_min: g("token_similarity_min", QC_DEFAULTS.token_similarity_min),
    duration_outlier_factor: g("duration_outlier_factor", QC_DEFAULTS.duration_outlier_factor),
    min_tokens_for_wer: g("min_tokens_for_wer", QC_DEFAULTS.min_tokens_for_wer),
    min_chars_for_duration_test: g("min_chars_for_duration_test", QC_DEFAULTS.min_chars_for_duration_test),
    max_token_repeat: g("max_token_repeat", QC_DEFAULTS.max_token_repeat),
    whisper_model: g("whisper_model", QC_DEFAULTS.whisper_model),
    condition_on_previous_text: g("condition_on_previous_text", QC_DEFAULTS.condition_on_previous_text),
    equivalences: JSON.stringify(g("equivalences", {}), null, 2),
  };
}

export function ConfigEditor() {
  const { id } = useParams<{ id: string }>();
  const client = useMemo(() => getApiClient(), []);
  const navigate = useNavigate();

  const [bookConfigText, setBookConfigText] = useState("{}");
  const [qc, setQc] = useState<QcFields>(QC_DEFAULTS);
  const [loading, setLoading] = useState(true);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    client.getJobConfig(id).then((cfg) => {
      if (cancelled) return;
      setBookConfigText(JSON.stringify(cfg.book_config, null, 2));
      setQc(toQcFields(cfg.qc_config));
      setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [client, id]);

  let bookConfigError: string | null = null;
  let parsedBookConfig: Record<string, unknown> | null = null;
  try {
    parsedBookConfig = JSON.parse(bookConfigText) as Record<string, unknown>;
  } catch {
    bookConfigError = "This is not valid JSON. Fix it before you save.";
  }

  let equivalencesError: string | null = null;
  let parsedEquivalences: Record<string, unknown> | null = null;
  try {
    parsedEquivalences = JSON.parse(qc.equivalences) as Record<string, unknown>;
  } catch {
    equivalencesError = "This is not valid JSON. Fix it before you save.";
  }

  const canSave = !bookConfigError && !equivalencesError && !saving;

  async function save() {
    if (!id || !parsedBookConfig || !parsedEquivalences) return;
    setSaving(true);
    setSaveError(null);
    try {
      await client.putJobConfig(id, {
        book_config: parsedBookConfig,
        qc_config: {
          wer_max: qc.wer_max,
          coverage_min: qc.coverage_min,
          token_similarity_min: qc.token_similarity_min,
          duration_outlier_factor: qc.duration_outlier_factor,
          min_tokens_for_wer: qc.min_tokens_for_wer,
          min_chars_for_duration_test: qc.min_chars_for_duration_test,
          max_token_repeat: qc.max_token_repeat,
          whisper_model: qc.whisper_model,
          condition_on_previous_text: qc.condition_on_previous_text,
          equivalences: parsedEquivalences,
        },
      });
      navigate(`/jobs/${id}`);
    } catch (err) {
      setSaveError((err as ApiClientError).message ?? "The save failed.");
    } finally {
      setSaving(false);
    }
  }

  if (!id) return <p>No job id.</p>;
  if (loading) return <p className="muted">Loading the configuration…</p>;

  return (
    <div className="stack">
      <h1>Configuration</h1>

      <div className="panel">
        <h2 style={{ fontSize: 15 }}>Book config</h2>
        <p className="muted">Refer to the pipeline contract, section 4.1, for every field.</p>
        <textarea
          aria-label="Book config JSON"
          value={bookConfigText}
          onChange={(e) => setBookConfigText(e.target.value)}
          rows={16}
          style={{ fontFamily: "ui-monospace, monospace" }}
        />
        {bookConfigError && (
          <p className="callout callout--warn" role="alert">
            {bookConfigError}
          </p>
        )}
      </div>

      <div className="panel">
        <h2 style={{ fontSize: 15 }}>QC config</h2>
        <div className="callout callout--warn">
          <p>
            Never widen a threshold to clear a red gate. The pipeline contract forbids it
            outright: a wider threshold hides a real fault instead of fixing it. Use{" "}
            <code>equivalences</code> for a foreign term or a name that whisper spells its
            own way, and use <strong>accept</strong> or <strong>re-render</strong> on the
            review queue for one chunk. Do not change these numbers to make a book pass.
          </p>
        </div>

        <div className="grid-2">
          <NumberField
            label="wer_max"
            value={qc.wer_max}
            step={0.01}
            onChange={(v) => setQc((s) => ({ ...s, wer_max: v }))}
          />
          <NumberField
            label="coverage_min"
            value={qc.coverage_min}
            step={0.01}
            onChange={(v) => setQc((s) => ({ ...s, coverage_min: v }))}
          />
          <NumberField
            label="token_similarity_min"
            value={qc.token_similarity_min}
            step={0.01}
            onChange={(v) => setQc((s) => ({ ...s, token_similarity_min: v }))}
          />
          <NumberField
            label="duration_outlier_factor"
            value={qc.duration_outlier_factor}
            step={0.1}
            onChange={(v) => setQc((s) => ({ ...s, duration_outlier_factor: v }))}
          />
          <NumberField
            label="min_tokens_for_wer"
            value={qc.min_tokens_for_wer}
            step={1}
            onChange={(v) => setQc((s) => ({ ...s, min_tokens_for_wer: v }))}
          />
          <NumberField
            label="min_chars_for_duration_test"
            value={qc.min_chars_for_duration_test}
            step={1}
            onChange={(v) => setQc((s) => ({ ...s, min_chars_for_duration_test: v }))}
          />
          <NumberField
            label="max_token_repeat"
            value={qc.max_token_repeat}
            step={1}
            onChange={(v) => setQc((s) => ({ ...s, max_token_repeat: v }))}
          />
          <div className="field">
            <label htmlFor="whisper-model">whisper_model</label>
            <input
              id="whisper-model"
              type="text"
              value={qc.whisper_model}
              onChange={(e) => setQc((s) => ({ ...s, whisper_model: e.target.value }))}
            />
          </div>
        </div>

        <div className="field">
          <label htmlFor="condition-on-previous">
            <input
              id="condition-on-previous"
              type="checkbox"
              style={{ width: "auto", marginRight: 6 }}
              checked={qc.condition_on_previous_text}
              onChange={(e) => setQc((s) => ({ ...s, condition_on_previous_text: e.target.checked }))}
            />
            condition_on_previous_text
          </label>
        </div>

        <div className="field" style={{ maxWidth: "none" }}>
          <label htmlFor="equivalences">equivalences (JSON)</label>
          <textarea
            id="equivalences"
            value={qc.equivalences}
            onChange={(e) => setQc((s) => ({ ...s, equivalences: e.target.value }))}
            rows={6}
            style={{ fontFamily: "ui-monospace, monospace" }}
          />
          {equivalencesError && (
            <p className="callout callout--warn" role="alert">
              {equivalencesError}
            </p>
          )}
        </div>
      </div>

      {saveError && (
        <p className="callout callout--warn" role="alert">
          {saveError}
        </p>
      )}

      <div className="btn-row">
        <button className="btn btn--primary" disabled={!canSave} onClick={save}>
          {saving ? "Saving…" : "Save"}
        </button>
        <button className="btn" onClick={() => navigate(`/jobs/${id}`)}>
          Cancel
        </button>
      </div>
    </div>
  );
}

function NumberField({
  label,
  value,
  step,
  onChange,
}: {
  label: string;
  value: number;
  step: number;
  onChange: (v: number) => void;
}) {
  return (
    <div className="field">
      <label htmlFor={`field-${label}`}>{label}</label>
      <input
        id={`field-${label}`}
        type="number"
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </div>
  );
}
