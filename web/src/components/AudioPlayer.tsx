// A labelled audio player over a blob URL.
//
// Warning: the source is always a blob URL, made from a Blob this component
// receives. This component never receives a plain HTTP URL for audio, and it
// never appends a key to one. Refer to APP-CONTRACT.md section 10.1.

import type { BlobFetcher } from "../hooks/useBlobAudio";
import { useBlobAudio } from "../hooks/useBlobAudio";

export interface AudioPlayerProps {
  label: string;
  fetcher: BlobFetcher | null;
}

export function AudioPlayer({ label, fetcher }: AudioPlayerProps) {
  const { url, loading, error } = useBlobAudio(fetcher);

  return (
    <div className="audio-player">
      <div className="audio-player__label" id={`audio-label-${label.replace(/\s+/g, "-")}`}>
        {label}
      </div>
      {loading && <p className="audio-player__status">Loading audio…</p>}
      {error && (
        <p className="audio-player__status audio-player__status--error" role="alert">
          {error}
        </p>
      )}
      {url && (
        // eslint-disable-next-line jsx-a11y/media-has-caption
        <audio
          controls
          src={url}
          aria-label={label}
          className="audio-player__el"
        />
      )}
    </div>
  );
}
