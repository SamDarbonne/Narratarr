// The blob-URL audio hook.
//
// Warning: the API key goes in the X-Api-Key header, never in a URL. Refer to
// APP-CONTRACT.md section 10.1. This hook fetches audio through a function
// that the caller supplies (an ApiClient method, which sets the header). The
// hook then wraps the returned Blob in a `blob:` URL for the <audio> element.
// A blob URL never carries a key, because it names browser memory, not a
// network resource.
//
// The hook revokes the blob URL when the component unmounts, or when the
// fetch function changes. A long review session that never revokes a blob
// URL leaks memory, one clip at a time.

import { useEffect, useRef, useState } from "react";

export type BlobFetcher = () => Promise<Blob>;

export interface UseBlobAudioResult {
  url: string | null;
  loading: boolean;
  error: string | null;
}

export function useBlobAudio(fetcher: BlobFetcher | null): UseBlobAudioResult {
  const [url, setUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(Boolean(fetcher));
  const [error, setError] = useState<string | null>(null);
  const urlRef = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    // Revoke the previous URL before making a new one. A stale blob URL from
    // an earlier fetch call must not survive past its own effect.
    if (urlRef.current) {
      URL.revokeObjectURL(urlRef.current);
      urlRef.current = null;
    }
    setUrl(null);

    if (!fetcher) {
      setLoading(false);
      setError(null);
      return;
    }

    setLoading(true);
    setError(null);

    fetcher()
      .then((blob) => {
        if (cancelled) return;
        const objectUrl = URL.createObjectURL(blob);
        urlRef.current = objectUrl;
        setUrl(objectUrl);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "The audio could not load.");
        setLoading(false);
      });

    return () => {
      cancelled = true;
      if (urlRef.current) {
        URL.revokeObjectURL(urlRef.current);
        urlRef.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fetcher]);

  return { url, loading, error };
}
