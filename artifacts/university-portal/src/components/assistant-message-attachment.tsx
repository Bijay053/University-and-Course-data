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
          <span className="mt-1 block break-all text-xs opacity-80">{filename}</span>
        </a>
      )}
    </div>
  );
}

export type MessageAttachment = { index: number; filename: string; contentType: string; size: number };

export function AssistantMessageAttachments({ jobId, turnId, attachments }: {
  jobId: string; turnId: string; attachments: MessageAttachment[];
}) {
  return <div className="mb-2 flex flex-wrap gap-3" data-testid="sent-attachments">
    {attachments.map(a => {
      const src = `/api/scrape/jobs/${encodeURIComponent(jobId)}/assistant/messages/${encodeURIComponent(turnId)}/attachments/${a.index}`;
      return a.contentType.startsWith("image/")
        ? <AssistantMessageAttachment key={a.index} src={src} filename={a.filename} />
        : <a key={a.index} href={src} target="_blank" rel="noopener noreferrer"
            className="rounded-lg border border-teal-700/30 px-3 py-2 text-sm underline"
            data-testid="sent-file-attachment">{a.filename} · PDF</a>;
    })}
  </div>;
}
