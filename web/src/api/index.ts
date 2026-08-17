// Picks the mock client or the real client. A screen imports `getApiClient`
// from this file only, and never imports httpClient.ts or mockClient.ts
// directly. Refer to APP-CONTRACT.md section 13.
//
// The flag is `VITE_USE_MOCK`. Vite reads it from `.env`, from the shell, or
// from `--mode`. It defaults to real backend usage in a production build and
// to the mock in local development, so `npm run dev` runs with no backend by
// default. Set `VITE_USE_MOCK=false` to develop against a live backend.

import type { ApiClient } from "./client";
import { createHttpApiClient } from "./httpClient";
import { createMockApiClient } from "./mockClient";

export type { ApiClient } from "./client";
export * from "./types";

function useMock(): boolean {
  const raw = import.meta.env.VITE_USE_MOCK;
  if (raw === undefined) return import.meta.env.DEV;
  return raw !== "false";
}

let cached: ApiClient | null = null;

export function getApiClient(): ApiClient {
  if (!cached) {
    cached = useMock() ? createMockApiClient() : createHttpApiClient();
  }
  return cached;
}

/** Test-only: forces a fresh client on the next getApiClient() call. */
export function resetApiClient(): void {
  cached = null;
}
