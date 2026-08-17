// Refer to APP-CONTRACT.md section 10.1: "The key never goes into a URL."
// This suite is the guard against a `?apikey=` fallback ever creeping back
// in. It asserts the header on every audio call, and asserts the fetched
// URL string never carries the key.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createHttpApiClient, setStoredApiKey } from "./httpClient";

const SECRET_KEY = "narratarr_super_secret_key_do_not_leak";

describe("createHttpApiClient — the audio routes never put the key in a URL", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    setStoredApiKey(SECRET_KEY);
    fetchMock = vi.fn(async () => new Response(new Blob(["fake audio"]), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    setStoredApiKey("");
  });

  it("fetches the QC chunk audio with the X-Api-Key header, and no key in the URL", async () => {
    const client = createHttpApiClient();
    await client.getReviewItemAudio("item-1");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];

    expect(url).not.toContain(SECRET_KEY);
    expect(url).not.toMatch(/apikey/i);
    expect((init.headers as Record<string, string>)["X-Api-Key"]).toBe(SECRET_KEY);
  });

  it("fetches a homograph candidate's audio with the header, and no key in the URL", async () => {
    const client = createHttpApiClient();
    await client.getReviewItemCandidateAudio("item-2", 2);

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/review/items/item-2/audio/2");
    expect(url).not.toContain(SECRET_KEY);
    expect((init.headers as Record<string, string>)["X-Api-Key"]).toBe(SECRET_KEY);
  });

  it("fetches the sample gate audio with the header, and no key in the URL", async () => {
    const client = createHttpApiClient();
    await client.getGateAudio("gate-1");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).not.toContain(SECRET_KEY);
    expect((init.headers as Record<string, string>)["X-Api-Key"]).toBe(SECRET_KEY);
  });

  it("puts the key in the header on an ordinary JSON route too, never in the query string", async () => {
    const client = createHttpApiClient();
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ items: [], total: 0, limit: 50, offset: 0 }), { status: 200 }),
    );
    await client.listJobs({ q: "trout" });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).not.toContain(SECRET_KEY);
    expect((init.headers as Record<string, string>)["X-Api-Key"]).toBe(SECRET_KEY);
  });
});
