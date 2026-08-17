// The Vitest setup file. jsdom does not implement `URL.createObjectURL` or
// `URL.revokeObjectURL`, and the blob-URL audio path needs both. This file
// polyfills them with a trackable fake, so a test can assert that a
// component revoked the URL it made.

import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Unmounts every rendered component after each test. Without this, a state
// pill or an audio player from one test can leak into the next test's DOM
// query and produce a false match or a false multiple-match failure.
afterEach(() => {
  cleanup();
});

let counter = 0;
const active = new Set<string>();

// `URL.createObjectURL` is read-only on the Node/jsdom URL class in this
// environment, so a plain assignment throws. `defineProperty` replaces it.
Object.defineProperty(URL, "createObjectURL", {
  value: (_blob: Blob) => {
    const url = `blob:mock-${counter++}`;
    active.add(url);
    return url;
  },
  writable: true,
  configurable: true,
});

Object.defineProperty(URL, "revokeObjectURL", {
  value: (url: string) => {
    active.delete(url);
  },
  writable: true,
  configurable: true,
});

export function activeBlobUrlCount(): number {
  return active.size;
}

/** Test-only: clears the tracked set between tests, so one test's leftover
 * blob URL never confuses the next test's count assertion. */
export function resetBlobUrlTracking(): void {
  active.clear();
}
