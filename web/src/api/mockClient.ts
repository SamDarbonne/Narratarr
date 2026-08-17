// The mock API client. It runs the whole application with no backend, using
// the fixtures of fixtures.ts as an in-memory store. This is the client the
// component tests exercise, and the client the screenshot walkthrough runs
// against. Refer to APP-CONTRACT.md section 13 for the shape it mirrors.

import type { ApiClient, EventStreamHandler, TargetWriteBody } from "./client";
import {
  FIXTURE_EVENTS,
  FIXTURE_GATES,
  FIXTURE_JOB_STATUS,
  FIXTURE_JOBS,
  FIXTURE_KEYS,
  FIXTURE_LIBRARY,
  FIXTURE_REVIEW_ITEMS,
  FIXTURE_SYSTEM_STATUS,
  FIXTURE_TARGETS,
  silentWavBlob,
} from "./fixtures";
import type {
  ApiKeyCreated,
  ApiKeyInfo,
  Artifact,
  CreateJobBody,
  Delivery,
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
  JobState,
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

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function paginate<T>(items: T[], limit = 50, offset = 0): ListResponse<T> {
  return { items: items.slice(offset, offset + limit), total: items.length, limit, offset };
}

function nowStamp(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${d.getUTCFullYear()}${pad(d.getUTCMonth() + 1)}${pad(d.getUTCDate())}T` +
    `${pad(d.getUTCHours())}${pad(d.getUTCMinutes())}${pad(d.getUTCSeconds())}Z`
  );
}

/**
 * Makes a fresh, isolated mock client. Each call clones the fixtures, so one
 * test's mutation (an accept, a resolve, a start) never leaks into another.
 */
export function createMockApiClient(): ApiClient {
  const jobs: JobDetail[] = clone(FIXTURE_JOBS).map((job: Job) => ({
    ...job,
    gates: [],
    deliveries: [],
  }));
  const gates: Gate[] = clone(FIXTURE_GATES);
  const reviewItems: ReviewItem[] = clone(FIXTURE_REVIEW_ITEMS);
  const targets: Target[] = clone(FIXTURE_TARGETS);
  const library: LibraryFile[] = clone(FIXTURE_LIBRARY);
  const keys: ApiKeyInfo[] = clone(FIXTURE_KEYS);
  const events: JobEvent[] = clone(FIXTURE_EVENTS);
  const deliveries: Delivery[] = [
    {
      id: "delivery-1",
      job_id: "job-fifth-season",
      target_id: "target-folder",
      state: "delivered",
      remote_ref: null,
      url: "/output/N. K. Jemisin/The Fifth Season/The Fifth Season.m4b",
      bytes: 812_400_000,
      error: null,
      created_at: "20260813T035000Z",
      delivered_at: "20260813T040000Z",
    },
  ];

  for (const gate of gates) {
    const job = jobs.find((j) => j.id === gate.job_id);
    if (job) job.gates.push(gate);
  }
  for (const d of deliveries) {
    const job = jobs.find((j) => j.id === d.job_id);
    if (job) job.deliveries.push(d);
  }

  function findJob(id: string): JobDetail {
    const job = jobs.find((j) => j.id === id);
    if (!job) throw new ApiClientError(404, "not_found", `No job ${id}.`);
    return job;
  }

  function findGate(id: string): Gate {
    const gate = gates.find((g) => g.id === id);
    if (!gate) throw new ApiClientError(404, "not_found", `No gate ${id}.`);
    return gate;
  }

  function findReviewItem(id: string): ReviewItem {
    const item = reviewItems.find((r) => r.id === id);
    if (!item) throw new ApiClientError(404, "not_found", `No review item ${id}.`);
    return item;
  }

  function addEvent(job_id: string, level: JobEvent["level"], stage: string | null, message: string) {
    events.push({ id: events.length + 1, job_id, level, stage, message, data: null, created_at: nowStamp() });
  }

  function setJobState(job: JobDetail, state: JobState) {
    job.state = state;
    job.updated_at = nowStamp();
  }

  function maybeCloseGate(gateId: string) {
    const gate = gates.find((g) => g.id === gateId);
    if (!gate) return;
    const open = reviewItems.filter((r) => r.gate_id === gateId && r.state === "open");
    gate.open_items = open.length;
    if (open.length === 0) {
      gate.state = "resolved";
      gate.resolved_at = nowStamp();
      const job = jobs.find((j) => j.id === gate.job_id);
      if (job) setJobState(job, "queued");
    }
  }

  return {
    health: async (): Promise<SystemHealth> => ({ status: "ok", version: "0.1.0-mock" }),
    systemStatus: async (): Promise<SystemStatus> => clone(FIXTURE_SYSTEM_STATUS),
    systemModels: async (): Promise<ListResponse<ModelInfo>> =>
      paginate(
        Object.entries(FIXTURE_SYSTEM_STATUS.models).map(([name, m]) => ({ name, ...m })),
      ),
    fetchModels: async () => undefined,

    listJobs: async (params: JobListParams = {}): Promise<ListResponse<Job>> => {
      let list = jobs as Job[];
      if (params.state) list = list.filter((j) => j.state === params.state);
      if (params.q) {
        const q = params.q.toLowerCase();
        list = list.filter(
          (j) => j.title?.toLowerCase().includes(q) || j.author?.toLowerCase().includes(q),
        );
      }
      return paginate(clone(list), params.limit, params.offset);
    },
    createJob: async (body: CreateJobBody): Promise<Job> => {
      const id = `job-${Math.random().toString(36).slice(2, 9)}`;
      const job: JobDetail = {
        id,
        slug: `new-book-${jobs.length + 1}`,
        title: body.file?.name.replace(/\.epub$/i, "") ?? body.source_path ?? "Untitled",
        author: null,
        year: null,
        genre: null,
        language: "en",
        source_path: body.source_path ?? `/config/library/${body.file?.name ?? "upload.epub"}`,
        source_sha256: "pending",
        cover_path: null,
        state: "queued",
        stage: null,
        worker: "local",
        priority: 0,
        progress_done: 0,
        progress_total: 0,
        error: null,
        book_config: {},
        qc_config: {},
        created_at: nowStamp(),
        updated_at: nowStamp(),
        started_at: null,
        finished_at: null,
        gates: [],
        deliveries: [],
      };
      jobs.unshift(job);
      return clone(job);
    },
    getJob: async (id: string): Promise<JobDetail> => {
      const job = findJob(id);
      // Simulate live progress so the screenshot and the demo look alive.
      if (job.state === "running" && job.progress_total > 0 && job.progress_done < job.progress_total) {
        job.progress_done = Math.min(job.progress_total, job.progress_done + 1);
        job.updated_at = nowStamp();
      }
      return clone(job);
    },
    deleteJob: async (id: string) => {
      const idx = jobs.findIndex((j) => j.id === id);
      if (idx >= 0) jobs.splice(idx, 1);
    },
    startJob: async (id: string) => {
      const job = findJob(id);
      setJobState(job, "running");
      job.stage = "extract";
      job.started_at = job.started_at ?? nowStamp();
      addEvent(id, "info", "extract", "The runner started the job.");
    },
    pauseJob: async (id: string) => {
      const job = findJob(id);
      setJobState(job, "paused");
      addEvent(id, "info", job.stage, "A person paused the job.");
    },
    cancelJob: async (id: string) => {
      const job = findJob(id);
      setJobState(job, "cancelled");
      addEvent(id, "info", job.stage, "A person cancelled the job.");
    },
    retryJob: async (id: string) => {
      const job = findJob(id);
      job.error = null;
      setJobState(job, "queued");
      addEvent(id, "info", null, "The error cleared. The job is queued again.");
    },
    getJobConfig: async (id: string): Promise<JobConfig> => {
      const job = findJob(id);
      return { book_config: clone(job.book_config), qc_config: clone(job.qc_config) };
    },
    putJobConfig: async (id: string, config: JobConfig) => {
      const job = findJob(id);
      if (job.state === "running") {
        throw new ApiClientError(409, "job_running", "The job is running. Stop it before editing its config.");
      }
      job.book_config = clone(config.book_config);
      job.qc_config = clone(config.qc_config);
      job.updated_at = nowStamp();
    },
    getJobEvents: async (id: string, since?: number, level?: string): Promise<ListResponse<JobEvent>> => {
      let list = events.filter((e) => e.job_id === id);
      if (since !== undefined) list = list.filter((e) => e.id > since);
      if (level) list = list.filter((e) => e.level === level);
      return paginate(clone(list), 200, 0);
    },
    subscribeJobEvents: (id: string, onEvent: EventStreamHandler) => {
      for (const e of events.filter((ev) => ev.job_id === id)) onEvent(clone(e));
      const job = jobs.find((j) => j.id === id);
      if (!job || job.state !== "running") return () => undefined;
      const timer = window.setInterval(() => {
        const current = jobs.find((j) => j.id === id);
        if (!current || current.state !== "running") return;
        addEvent(id, "info", current.stage, `Rendering chunk ${current.progress_done} of ${current.progress_total}.`);
        onEvent(clone(events[events.length - 1]));
      }, 4000);
      return () => window.clearInterval(timer);
    },
    getJobArtifacts: async (id: string): Promise<ListResponse<Artifact>> => {
      const job = findJob(id);
      const items: Artifact[] = [
        { name: "book.json", path: `work/${job.slug}/book.json`, size_bytes: 4096, exists: true },
        {
          name: "cover",
          path: `work/${job.slug}/cover.jpg`,
          size_bytes: job.cover_path ? 180_000 : null,
          exists: Boolean(job.cover_path),
        },
        {
          name: "m4b",
          path: `work/${job.slug}/07-book/${job.title}.m4b`,
          size_bytes: job.state === "done" ? 812_000_000 : null,
          exists: job.state === "done",
        },
      ];
      return paginate(items);
    },
    getJobStatus: async (): Promise<JobStatus> => clone(FIXTURE_JOB_STATUS),
    deliverJob: async (id: string) => {
      const job = findJob(id);
      setJobState(job, "delivering");
      addEvent(id, "info", "deliver", "Delivery started to every enabled target.");
    },
    fixJob: async (id: string, items: FixItem[]) => {
      const job = findJob(id);
      addEvent(id, "info", null, `Fix flow applied to ${items.length} chunk(s).`);
      setJobState(job, "queued");
    },

    listGates: async (params: GateListParams = {}): Promise<ListResponse<Gate>> =>
      paginate(clone(gates.filter((g) => g.state === "open")), params.limit, params.offset),
    getGate: async (id: string): Promise<GateDetail> => {
      const gate = findGate(id);
      const items = reviewItems.filter((r) => r.gate_id === id);
      return clone({ ...gate, review_items: items });
    },
    resolveGate: async (id: string, resolution: string, reason?: string) => {
      const gate = findGate(id);
      gate.state = "resolved";
      gate.resolution = resolution as Gate["resolution"];
      gate.reason = reason ?? null;
      gate.resolved_at = nowStamp();
      gate.resolved_by = "mock-user";
      const job = jobs.find((j) => j.id === gate.job_id);
      if (job) {
        setJobState(job, resolution === "rejected" ? "running" : "queued");
        addEvent(job.id, "info", job.stage, `Sample gate resolved: ${resolution}.`);
      }
    },
    getGateAudio: async (): Promise<Blob> => silentWavBlob(),

    listReviewItems: async (params: ReviewItemListParams = {}): Promise<ListResponse<ReviewItem>> => {
      let list = reviewItems;
      if (params.job_id) list = list.filter((r) => r.job_id === params.job_id);
      if (params.gate_id) list = list.filter((r) => r.gate_id === params.gate_id);
      if (params.state) list = list.filter((r) => r.state === params.state);
      if (params.kind) list = list.filter((r) => r.kind === params.kind);
      return paginate(clone(list), params.limit, params.offset);
    },
    getReviewItem: async (id: string): Promise<ReviewItem> => clone(findReviewItem(id)),
    acceptReviewItem: async (id: string, reason: string) => {
      if (!reason.trim()) {
        throw new ApiClientError(422, "empty_reason", "The reason must not be empty.");
      }
      const item = findReviewItem(id);
      item.state = "accepted";
      item.resolution = "accepted";
      item.reason = reason;
      item.resolved_at = nowStamp();
      addEvent(item.job_id, "info", "qc", `Chunk ${item.chapter}/${item.chunk} accepted: ${reason}`);
      maybeCloseGate(item.gate_id);
    },
    rerenderReviewItem: async (id: string) => {
      const item = findReviewItem(id);
      item.state = "rerendered";
      item.wav_sha256 = `${item.wav_sha256 ?? ""}-rerendered`;
      addEvent(item.job_id, "info", "qc", `Chunk ${item.chapter}/${item.chunk} queued for a re-render.`);
      maybeCloseGate(item.gate_id);
    },
    resolveHomograph: async (id: string, reading: string) => {
      const item = findReviewItem(id);
      item.state = "resolved";
      item.resolution = reading;
      item.resolved_at = nowStamp();
      addEvent(item.job_id, "info", "homographs", `'${item.word}' occurrence ${item.occurrence} resolved: ${reading}.`);
      maybeCloseGate(item.gate_id);
    },
    getReviewItemAudio: async (): Promise<Blob> => silentWavBlob(),
    getReviewItemCandidateAudio: async (): Promise<Blob> => silentWavBlob(),

    listTargets: async (): Promise<ListResponse<Target>> => paginate(clone(targets)),
    createTarget: async (body: TargetWriteBody): Promise<Target> => {
      const target: Target = {
        id: `target-${Math.random().toString(36).slice(2, 9)}`,
        name: body.name,
        kind: body.kind,
        enabled: body.enabled ?? true,
        config: body.config,
        created_at: nowStamp(),
        updated_at: nowStamp(),
      };
      targets.push(target);
      return clone(target);
    },
    getTarget: async (id: string): Promise<Target> => {
      const target = targets.find((t) => t.id === id);
      if (!target) throw new ApiClientError(404, "not_found", `No target ${id}.`);
      return clone(target);
    },
    updateTarget: async (id: string, body: TargetWriteBody): Promise<Target> => {
      const target = targets.find((t) => t.id === id);
      if (!target) throw new ApiClientError(404, "not_found", `No target ${id}.`);
      target.name = body.name;
      target.kind = body.kind;
      target.enabled = body.enabled ?? target.enabled;
      target.config = body.config;
      target.updated_at = nowStamp();
      return clone(target);
    },
    deleteTarget: async (id: string) => {
      const idx = targets.findIndex((t) => t.id === id);
      if (idx >= 0) targets.splice(idx, 1);
    },
    testTarget: async (id: string): Promise<DeliveryResult> => {
      const target = targets.find((t) => t.id === id);
      if (!target) throw new ApiClientError(404, "not_found", `No target ${id}.`);
      return { ok: true, remote_ref: null, url: null, bytes: 0, message: "Reachable." };
    },
    getSettings: async (): Promise<Record<string, unknown>> => ({
      sample_gate: true,
      prune: false,
      watch_delete_after_ingest: false,
      watch_interval_s: 60,
      events_per_job_max: 5000,
    }),
    putSettings: async () => undefined,
    listLibrary: async (): Promise<ListResponse<LibraryFile>> => paginate(clone(library)),
    scanLibrary: async () => undefined,
    listKeys: async (): Promise<ListResponse<ApiKeyInfo>> => paginate(clone(keys)),
    createKey: async (name: string): Promise<ApiKeyCreated> => {
      const info: ApiKeyInfo = { id: `key-${keys.length + 1}`, name, created_at: nowStamp(), last_used_at: null };
      keys.push(info);
      return { ...info, key: `narratarr_mock_${Math.random().toString(36).slice(2, 18)}` };
    },
    deleteKey: async (id: string) => {
      const idx = keys.findIndex((k) => k.id === id);
      if (idx >= 0) keys.splice(idx, 1);
    },
    deliveries: async (jobId: string): Promise<Delivery[]> => clone(deliveries.filter((d) => d.job_id === jobId)),
  };
}
