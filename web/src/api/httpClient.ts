// The real API client. It talks to the FastAPI backend at /api/v1.
//
// Warning: the API key goes in the X-Api-Key header, and nowhere else. A URL
// enters a log, a browser history, and a referrer header. Refer to
// APP-CONTRACT.md section 10.1. This file must never build a `?apikey=` URL.

import type {
  ApiClient,
  EventStreamHandler,
  TargetWriteBody,
} from "./client";
import type {
  ApiErrorBody,
  ApiKeyCreated,
  ApiKeyInfo,
  Artifact,
  CreateJobBody,
  DeliveryResult,
  FixItem,
  Gate,
  GateDetail,
  GateListParams,
  Job,
  JobConfig,
  JobDetail,
  JobEvent,
  JobListParams,
  JobStatus,
  LibraryFile,
  ListResponse,
  ModelInfo,
  ReviewItem,
  ReviewItemListParams,
  SystemHealth,
  SystemStatus,
  Target,
} from "./types";
import { ApiClientError } from "./types";

const BASE = "/api/v1";

function query(params: object): string {
  const usp = new URLSearchParams();
  for (const [key, value] of Object.entries(params as Record<string, unknown>)) {
    if (value !== undefined && value !== "") usp.set(key, String(value));
  }
  const s = usp.toString();
  return s ? `?${s}` : "";
}

/** Reads a stored key. The key never rides in a URL, only in this header. */
function apiKeyHeader(): Record<string, string> {
  const key = getStoredApiKey();
  return key ? { "X-Api-Key": key } : {};
}

const API_KEY_STORAGE = "narratarr.apiKey";

// A module-level fallback. Some test environments run with no
// `window.localStorage` at all. A real browser always has it, so the
// fallback only ever activates under test.
let memoryFallback: string | null = null;

function hasLocalStorage(): boolean {
  try {
    return typeof window !== "undefined" && Boolean(window.localStorage);
  } catch {
    return false;
  }
}

export function getStoredApiKey(): string | null {
  if (!hasLocalStorage()) return memoryFallback;
  try {
    return window.localStorage.getItem(API_KEY_STORAGE);
  } catch {
    return memoryFallback;
  }
}

export function setStoredApiKey(key: string): void {
  memoryFallback = key;
  if (hasLocalStorage()) {
    try {
      window.localStorage.setItem(API_KEY_STORAGE, key);
    } catch {
      // The fallback above already holds the key.
    }
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      ...(init?.body && !(init.body instanceof FormData)
        ? { "Content-Type": "application/json" }
        : {}),
      ...apiKeyHeader(),
      ...init?.headers,
    },
  });
  if (!res.ok) {
    let body: ApiErrorBody | null = null;
    try {
      body = (await res.json()) as ApiErrorBody;
    } catch {
      // The body was not JSON. The status code still carries the fault.
    }
    throw new ApiClientError(
      res.status,
      body?.error.code ?? "unknown",
      body?.error.message ?? res.statusText,
      body?.error.detail,
    );
  }
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  return text ? (JSON.parse(text) as T) : (undefined as T);
}

async function requestBlob(path: string): Promise<Blob> {
  const res = await fetch(`${BASE}${path}`, { headers: { ...apiKeyHeader() } });
  if (!res.ok) {
    throw new ApiClientError(res.status, "audio_fetch_failed", res.statusText);
  }
  return res.blob();
}

export function createHttpApiClient(): ApiClient {
  return {
    health: () => request<SystemHealth>("/system/health"),
    systemStatus: () => request<SystemStatus>("/system/status"),
    systemModels: () => request<ListResponse<ModelInfo>>("/system/models"),
    fetchModels: () => request<void>("/system/models/fetch", { method: "POST" }),

    listJobs: (params: JobListParams = {}) =>
      request<ListResponse<Job>>(`/jobs${query(params)}`),
    createJob: (body: CreateJobBody) => {
      if (body.file) {
        const form = new FormData();
        form.append("file", body.file);
        if (body.allow_duplicate) form.append("allow_duplicate", "true");
        return request<Job>("/jobs", { method: "POST", body: form });
      }
      return request<Job>("/jobs", {
        method: "POST",
        body: JSON.stringify({
          source_path: body.source_path,
          allow_duplicate: body.allow_duplicate,
        }),
      });
    },
    getJob: (id: string) => request<JobDetail>(`/jobs/${id}`),
    deleteJob: (id: string, purge = false) =>
      request<void>(`/jobs/${id}${query({ purge })}`, { method: "DELETE" }),
    startJob: (id: string) => request<void>(`/jobs/${id}/start`, { method: "POST" }),
    pauseJob: (id: string) => request<void>(`/jobs/${id}/pause`, { method: "POST" }),
    cancelJob: (id: string) => request<void>(`/jobs/${id}/cancel`, { method: "POST" }),
    retryJob: (id: string) => request<void>(`/jobs/${id}/retry`, { method: "POST" }),
    getJobConfig: (id: string) => request<JobConfig>(`/jobs/${id}/config`),
    putJobConfig: (id: string, config: JobConfig) =>
      request<void>(`/jobs/${id}/config`, { method: "PUT", body: JSON.stringify(config) }),
    getJobEvents: (id: string, since?: number, level?: string) =>
      request<ListResponse<JobEvent>>(`/jobs/${id}/events${query({ since, level })}`),
    subscribeJobEvents: (id: string, onEvent: EventStreamHandler) => {
      const source = new EventSource(`${BASE}/jobs/${id}/events/stream`);
      source.onmessage = (msg) => {
        try {
          onEvent(JSON.parse(msg.data) as JobEvent);
        } catch {
          // A malformed event is dropped. The log view keeps its last state.
        }
      };
      return () => source.close();
    },
    getJobArtifacts: (id: string) => request<ListResponse<Artifact>>(`/jobs/${id}/artifacts`),
    getJobStatus: (id: string) => request<JobStatus>(`/jobs/${id}/status`),
    deliverJob: (id: string) => request<void>(`/jobs/${id}/deliver`, { method: "POST" }),
    fixJob: (id: string, items: FixItem[]) =>
      request<void>(`/jobs/${id}/fix`, { method: "POST", body: JSON.stringify({ items }) }),

    listGates: (params: GateListParams = {}) => request<ListResponse<Gate>>(`/gates${query(params)}`),
    getGate: (id: string) => request<GateDetail>(`/gates/${id}`),
    resolveGate: (id: string, resolution: string, reason?: string) =>
      request<void>(`/gates/${id}/resolve`, {
        method: "POST",
        body: JSON.stringify({ resolution, reason }),
      }),
    // ASSUMPTION: refer to the docstring on ApiClient.getGateAudio in client.ts.
    getGateAudio: (id: string) => requestBlob(`/gates/${id}/audio`),
    listReviewItems: (params: ReviewItemListParams = {}) =>
      request<ListResponse<ReviewItem>>(`/review/items${query(params)}`),
    getReviewItem: (id: string) => request<ReviewItem>(`/review/items/${id}`),
    acceptReviewItem: (id: string, reason: string) =>
      request<void>(`/review/items/${id}/accept`, {
        method: "POST",
        body: JSON.stringify({ reason }),
      }),
    rerenderReviewItem: (id: string) =>
      request<void>(`/review/items/${id}/rerender`, { method: "POST" }),
    resolveHomograph: (id: string, reading: string) =>
      request<void>(`/review/items/${id}/resolve`, {
        method: "POST",
        body: JSON.stringify({ reading }),
      }),
    getReviewItemAudio: (id: string) => requestBlob(`/review/items/${id}/audio`),
    getReviewItemCandidateAudio: (id: string, n: number) =>
      requestBlob(`/review/items/${id}/audio/${n}`),

    listTargets: () => request<ListResponse<Target>>("/targets"),
    createTarget: (body: TargetWriteBody) =>
      request<Target>("/targets", { method: "POST", body: JSON.stringify(body) }),
    getTarget: (id: string) => request<Target>(`/targets/${id}`),
    updateTarget: (id: string, body: TargetWriteBody) =>
      request<Target>(`/targets/${id}`, { method: "PUT", body: JSON.stringify(body) }),
    deleteTarget: (id: string) => request<void>(`/targets/${id}`, { method: "DELETE" }),
    testTarget: (id: string) => request<DeliveryResult>(`/targets/${id}/test`, { method: "POST" }),
    getSettings: () => request<Record<string, unknown>>("/settings"),
    putSettings: (settings: Record<string, unknown>) =>
      request<void>("/settings", { method: "PUT", body: JSON.stringify(settings) }),
    listLibrary: () => request<ListResponse<LibraryFile>>("/library"),
    scanLibrary: () => request<void>("/library/scan", { method: "POST" }),
    listKeys: () => request<ListResponse<ApiKeyInfo>>("/keys"),
    createKey: (name: string) =>
      request<ApiKeyCreated>("/keys", { method: "POST", body: JSON.stringify({ name }) }),
    deleteKey: (id: string) => request<void>(`/keys/${id}`, { method: "DELETE" }),
    deliveries: async (jobId: string) => {
      const job = await request<JobDetail>(`/jobs/${jobId}`);
      return job.deliveries;
    },
  };
}
