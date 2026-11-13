import { useEffect, useState } from "react";

/** Authenticated image URL, never a public bucket URL or a persisted data URI. */
export function AssistantMessageAttachment({ src, filename }: { src: string; filename: string }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [src]);
  return (
    <div className="mb-2" data-testid="sent-image-attachment">
      {failed ? (
        <p role="status" className="rounded-lg border border-teal-700 p-2 text-xs">
          {filename} — Image unavailable. It may have expired or been removed.
        </p>
      ) : (
        <a href={src} target="_blank" rel="noopener noreferrer" aria-label={`Open image ${filename}`}>
          <img src={src} alt={`Attached image: ${filename}`} loading="lazy"
            onError={() => setFailed(true)}
            className="max-h-72 max-w-full rounded-lg border border-teal-700 object-contain" />
          <span className="mt-1 block break-all text-xs text-teal-100">{filename}</span>
        </a>
      )}
    </div>
  );
}
