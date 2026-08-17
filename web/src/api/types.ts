// The types of this file mirror APP-CONTRACT.md. Section 4 gives the database
// shape. Section 13 gives the API shape. Keep this file matched to the contract.
// A worker never invents a field the contract does not name. Refer to
// APP-CONTRACT.md section 13 for the frozen route list.

export type JobState =
  | "queued"
  | "running"
  | "awaiting_sample_approval"
  | "awaiting_homograph_review"
  | "awaiting_qc_review"
  | "delivering"
  | "done"
  | "failed"
  | "cancelled"
  | "paused";

/** The three states where a person, not the runner, holds the next action. */
export const GATE_STATES: readonly JobState[] = [
  "awaiting_sample_approval",
  "awaiting_homograph_review",
  "awaiting_qc_review",
];

export type JobStage =
  | "extract"
  | "normalize"
  | "chunk"
  | "sample"
  | "homographs"
  | "render"
  | "qc"
  | "assemble"
  | "bind"
  | "deliver"
  | null;

export interface Job {
  id: string;
  slug: string;
  title: string | null;
  author: string | null;
  year: string | null;
  genre: string | null;
  language: string;
  source_path: string;
  source_sha256: string;
  cover_path: string | null;
  state: JobState;
  stage: JobStage;
  worker: string;
  priority: number;
  progress_done: number;
  /** `0` means unknown. Render an indeterminate bar, never a false percentage. */
  progress_total: number;
  error: string | null;
  book_config: Record<string, unknown>;
  qc_config: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface JobDetail extends Job {
  gates: Gate[];
  deliveries: Delivery[];
}

export type GateKind = "sample" | "homograph" | "qc";
export type GateState = "open" | "resolved" | "voided";
export type GateResolution = "approved" | "rejected" | "rerender" | "edited" | null;

export interface Gate {
  // The API returns the job's title and slug flat on the gate row, so the
  // review queue needs no second request per gate.
  job_title?: string | null;
  job_slug?: string | null;
  id: string;
  job_id: string;
  kind: GateKind;
  state: GateState;
  payload: Record<string, unknown>;
  open_items: number;
  created_at: string;
  resolved_at: string | null;
  resolved_by: string | null;
  resolution: GateResolution;
  reason: string | null;
  // Present on GET /gates so the review queue can show the book without a
  // second round trip per row. The contract does not name this field
  // explicitly; refer to the gap note in the worker report.
  job?: Pick<Job, "id" | "slug" | "title" | "author" | "cover_path">;
}

export interface GateDetail extends Gate {
  review_items: ReviewItem[];
}

export type ReviewItemKind = "qc_chunk" | "homograph_occurrence";
export type ReviewItemState = "open" | "accepted" | "rerendered" | "resolved" | "voided";

export interface HomographCandidate {
  reading: string;
  phonemes: string;
  audio: string;
}

export interface ReviewItem {
  id: string;
  job_id: string;
  gate_id: string;
  kind: ReviewItemKind;
  chapter: string;
  chunk: string | null;
  word: string | null;
  occurrence: number | null;
  source_text: string | null;
  transcript: string | null;
  context: string | null;
  wer: number | null;
  coverage: number | null;
  duration_s: number | null;
  flags: string[];
  wav_sha256: string | null;
  candidates: HomographCandidate[] | null;
  state: ReviewItemState;
  resolution: string | null;
  reason: string | null;
  resolved_at: string | null;
  created_at: string;
}

export type TargetKind = "folder" | "audiobookshelf";

export interface Target {
  id: string;
  name: string;
  kind: TargetKind;
  enabled: boolean;
  /** A secret is never present here. Refer to APP-CONTRACT section 10.2. */
  config: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export type DeliveryState = "pending" | "delivering" | "delivered" | "failed";

export interface Delivery {
  id: string;
  job_id: string;
  target_id: string;
  state: DeliveryState;
  remote_ref: string | null;
  url: string | null;
  bytes: number | null;
  error: string | null;
  created_at: string;
  delivered_at: string | null;
}

export interface DeliveryResult {
  ok: boolean;
  remote_ref: string | null;
  url: string | null;
  bytes: number;
  message: string;
}

export interface ApiKeyInfo {
  id: string;
  name: string;
  created_at: string;
  last_used_at: string | null;
}

/** The key value returns once, on create, and never again. */
export interface ApiKeyCreated extends ApiKeyInfo {
  key: string;
}

export interface SystemHealth {
  status: "ok";
  version: string;
}

export interface ModelInfo {
  name: string;
  present: boolean;
  size_bytes: number | null;
}

export interface SecretInfo {
  name: string;
  present: boolean;
}

// Warning: this shape is what `GET /api/v1/system/status` really returns.
// An earlier version of this file guessed a nested `runner: {state}` and
// arrays for `models` and `secrets`. The server returns a flat
// `runner_state` and two objects keyed by name. TopBar reads this on every
// screen, so the guess blanked the whole application, on every page, with
// one TypeError. Refer to APP-CONTRACT.md section 13.1, which now writes
// the shape down.
export interface EnginePreflight {
  espeak_fallback: boolean;
  warmup_samples: number;
  warmup_sample_rate: number;
  oov_probe_word: string;
  oov_probe_nonempty: boolean;
  job_slug: string | null;
  checked_at: string;
}

export interface SystemStatus {
  runner_state: string;
  queue_depth: number;
  disk_free_bytes: number;
  models: Record<string, { present: boolean; size_bytes: number | null }>;
  secrets: Record<string, { present: boolean }>;
  engine_preflight: EnginePreflight | null;
}

export interface StageStatusEntry {
  fresh: number;
  stale: number;
  absent: number;
}

export type JobStatus = Partial<Record<NonNullable<JobStage>, StageStatusEntry>>;

export interface Artifact {
  name: string;
  path: string;
  size_bytes: number | null;
  exists: boolean;
}

export interface JobEvent {
  id: number;
  job_id: string;
  level: "debug" | "info" | "warning" | "error";
  stage: string | null;
  message: string;
  data: Record<string, unknown> | null;
  created_at: string;
}

export interface LibraryFile {
  path: string;
  size_bytes: number;
  ingested: boolean;
  job_id: string | null;
}

export interface FixItem {
  chapter: string;
  chunk: string;
  action: "rerender" | "pronunciation" | "homograph";
  value: Record<string, unknown>;
  reason: string;
}

export interface JobConfig {
  book_config: Record<string, unknown>;
  qc_config: Record<string, unknown>;
}

export interface ListResponse<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    detail?: Record<string, unknown>;
  };
}

/** Thrown by the API client on any non-2xx response. */
export class ApiClientError extends Error {
  code: string;
  status: number;
  detail?: Record<string, unknown>;

  constructor(status: number, code: string, message: string, detail?: Record<string, unknown>) {
    super(message);
    this.name = "ApiClientError";
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

export interface JobListParams {
  state?: JobState;
  q?: string;
  limit?: number;
  offset?: number;
}

export interface GateListParams {
  limit?: number;
  offset?: number;
}

export interface ReviewItemListParams {
  job_id?: string;
  gate_id?: string;
  state?: ReviewItemState;
  kind?: ReviewItemKind;
  limit?: number;
  offset?: number;
}

export interface CreateJobBody {
  source_path?: string;
  file?: File;
  allow_duplicate?: boolean;
}
