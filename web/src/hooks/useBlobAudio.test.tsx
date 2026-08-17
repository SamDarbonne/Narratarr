// Refer to APP-CONTRACT.md section 10.1: a long review session that never
// revokes a blob URL leaks memory, one clip at a time. This suite checks the
// revoke-on-unmount and revoke-on-refetch behaviour of useBlobAudio.

import { act, render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useBlobAudio } from "./useBlobAudio";
import { activeBlobUrlCount, resetBlobUrlTracking } from "../test/setup";

beforeEach(() => {
  resetBlobUrlTracking();
});

function Probe({ fetcher }: { fetcher: (() => Promise<Blob>) | null }) {
  const { url, loading, error } = useBlobAudio(fetcher);
  return (
    <div>
      <span data-testid="url">{url ?? ""}</span>
      <span data-testid="loading">{String(loading)}</span>
      <span data-testid="error">{error ?? ""}</span>
    </div>
  );
}

describe("useBlobAudio", () => {
  it("makes a blob URL once the fetch resolves", async () => {
    const fetcher = vi.fn(async () => new Blob(["audio"], { type: "audio/wav" }));
    const { getByTestId } = render(<Probe fetcher={fetcher} />);

    expect(getByTestId("loading").textContent).toBe("true");
    await waitFor(() => expect(getByTestId("loading").textContent).toBe("false"));
    expect(getByTestId("url").textContent).toMatch(/^blob:/);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("revokes the blob URL when the component unmounts", async () => {
    const fetcher = vi.fn(async () => new Blob(["audio"], { type: "audio/wav" }));
    const { getByTestId, unmount } = render(<Probe fetcher={fetcher} />);
    await waitFor(() => expect(getByTestId("url").textContent).toMatch(/^blob:/));

    expect(activeBlobUrlCount()).toBe(1);
    act(() => unmount());
    expect(activeBlobUrlCount()).toBe(0);
  });

  it("revokes the old URL before making a new one when the fetcher changes", async () => {
    const fetcherA = vi.fn(async () => new Blob(["a"], { type: "audio/wav" }));
    const fetcherB = vi.fn(async () => new Blob(["b"], { type: "audio/wav" }));
    const { getByTestId, rerender } = render(<Probe fetcher={fetcherA} />);
    await waitFor(() => expect(getByTestId("url").textContent).toMatch(/^blob:/));
    const firstUrl = getByTestId("url").textContent;

    rerender(<Probe fetcher={fetcherB} />);
    await waitFor(() => {
      const current = getByTestId("url").textContent;
      expect(current).toMatch(/^blob:/);
      expect(current).not.toBe(firstUrl);
    });
    expect(activeBlobUrlCount()).toBe(1);
  });

  it("reports an error and no URL when the fetch rejects", async () => {
    const fetcher = vi.fn(async () => {
      throw new Error("network down");
    });
    const { getByTestId } = render(<Probe fetcher={fetcher} />);
    await waitFor(() => expect(getByTestId("error").textContent).toBe("network down"));
    expect(getByTestId("url").textContent).toBe("");
  });

  it("does nothing when the fetcher is null", () => {
    const { getByTestId } = render(<Probe fetcher={null} />);
    expect(getByTestId("loading").textContent).toBe("false");
    expect(getByTestId("url").textContent).toBe("");
  });
});
