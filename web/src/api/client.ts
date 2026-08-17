// The typed client interface. narratarr/api/httpClient.ts and
// narratarr/api/mockClient.ts each implement this interface. A screen imports
// only this file's types and the `getApiClient()` factory of index.ts. A
// screen never imports httpClient.ts or mockClient.ts directly, so the mock
// and the real client stay interchangeable.

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
  JobStatus,
  LibraryFile,
  ListResponse,
  ReviewItem,
  ReviewItemListParams,
  SystemHealth,
  SystemStatus,
  Target,
  TargetKind,
} from "./types";

export interface TargetWriteBody {
  name: string;
  kind: TargetKind;
  enabled?: boolean;
  config: Record<string, unknown>;
}

/** One line of the live event stream. The subscriber gets one call per event. */
export type EventStreamHandler = (event: JobEvent) => void;

export interface ApiClient {
  // 13.1 System
  health(): Promise<SystemHealth>;
  systemStatus(): Promise<SystemStatus>;
  systemModels(): Promise<ListResponse<import("./types").ModelInfo>>;
  fetchModels(): Promise<void>;

  // 13.2 Jobs
  listJobs(params?: JobListParams): Promise<ListResponse<Job>>;
  createJob(body: CreateJobBody): Promise<Job>;
  getJob(id: string): Promise<JobDetail>;
  deleteJob(id: string, purge?: boolean): Promise<void>;
  startJob(id: string): Promise<void>;
  pauseJob(id: string): Promise<void>;
  cancelJob(id: string): Promise<void>;
  retryJob(id: string): Promise<void>;
  getJobConfig(id: string): Promise<JobConfig>;
  putJobConfig(id: string, config: JobConfig): Promise<void>;
  getJobEvents(id: string, since?: number, level?: string): Promise<ListResponse<JobEvent>>;
  /** Returns an unsubscribe function. Refer to GET /jobs/{id}/events/stream. */
  subscribeJobEvents(id: string, onEvent: EventStreamHandler): () => void;
  getJobArtifacts(id: string): Promise<ListResponse<Artifact>>;
  getJobStatus(id: string): Promise<JobStatus>;
  deliverJob(id: string): Promise<void>;
  fixJob(id: string, items: FixItem[]): Promise<void>;

  // 13.3 Gates and review
  listGates(params?: GateListParams): Promise<ListResponse<Gate>>;
  getGate(id: string): Promise<GateDetail>;
  resolveGate(id: string, resolution: string, reason?: string): Promise<void>;
  /**
   * Fetches the sample gate's audio with the auth header. Returns a Blob, never a URL.
   *
   * ASSUMPTION, not in the frozen spec: APP-CONTRACT.md section 13.3 names no route for
   * the sample gate's audio. Section 9.1 requires an audio player at this gate. This
   * client calls `GET /gates/{id}/audio`, named after the documented
   * `GET /review/items/{id}/audio`. Flagged for the overlord.
   */
  getGateAudio(id: string): Promise<Blob>;
  listReviewItems(params?: ReviewItemListParams): Promise<ListResponse<ReviewItem>>;
  getReviewItem(id: string): Promise<ReviewItem>;
  acceptReviewItem(id: string, reason: string): Promise<void>;
  rerenderReviewItem(id: string): Promise<void>;
  resolveHomograph(id: string, reading: string): Promise<void>;
  /** Fetches the chunk audio with the auth header. Returns a Blob, never a URL. */
  getReviewItemAudio(id: string): Promise<Blob>;
  /** Fetches candidate `n`'s audio with the auth header. Returns a Blob, never a URL. */
  getReviewItemCandidateAudio(id: string, n: number): Promise<Blob>;

  // 13.4 Targets and settings
  listTargets(): Promise<ListResponse<Target>>;
  createTarget(body: TargetWriteBody): Promise<Target>;
  getTarget(id: string): Promise<Target>;
  updateTarget(id: string, body: TargetWriteBody): Promise<Target>;
  deleteTarget(id: string): Promise<void>;
  testTarget(id: string): Promise<DeliveryResult>;
  getSettings(): Promise<Record<string, unknown>>;
  putSettings(settings: Record<string, unknown>): Promise<void>;
  listLibrary(): Promise<ListResponse<LibraryFile>>;
  scanLibrary(): Promise<void>;
  listKeys(): Promise<ListResponse<ApiKeyInfo>>;
  createKey(name: string): Promise<ApiKeyCreated>;
  deleteKey(id: string): Promise<void>;
  deliveries(jobId: string): Promise<Delivery[]>;
}
