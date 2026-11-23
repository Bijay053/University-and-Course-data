import { ExternalLink, GraduationCap } from "lucide-react";
import { AssistantMarkdown } from "./assistant-markdown";
import { AssistantMessageAttachment, AssistantMessageAttachments } from "./assistant-message-attachment";
import { safeSourceUrl } from "./assistant-safe-url";
import { clockTime, type SavedMessage } from "./assistant-timeline";

const STATE_TEXT: Record<string, string> = {
  proposed: "Not run", starting: "Started", running: "Was running", completed: "Completed",
  needs_review: "Needs review", error: "Failed", superseded: "Outdated",
};

/** A saved message from earlier in the conversation. Read-only: its actions cannot execute here. */
export function AssistantSavedMessage({ message: m, at, currentJobId, onReviewJob }: {
  message: SavedMessage; at: number | null; currentJobId: string; onReviewJob?: (jobId: string) => void;
}) {
  const otherReview = m.jobId !== currentJobId;
  const isImage = !!m.attachment && !!m.turnId && ["image/png", "image/jpeg"].includes(m.attachment.contentType);
  const image = m.attachments?.length && m.turnId
    ? <AssistantMessageAttachments jobId={m.jobId} turnId={m.turnId} attachments={m.attachments} />
    : isImage && (
    <AssistantMessageAttachment filename={m.attachment!.filename}
      src={`/api/scrape/jobs/${encodeURIComponent(m.jobId)}/assistant/messages/${encodeURIComponent(m.turnId!)}/image`} />
  );
  const time = clockTime(at);

  if (m.role === "user") return (
    <li className="flex flex-col items-end" data-testid="saved-message" data-role="user" data-job={m.jobId}>
      <div className="max-w-[85%] rounded-2xl rounded-br-md bg-[#e6ece9] px-3.5 py-2.5 text-[13.5px] leading-relaxed text-slate-900">
        <span className="sr-only">You said: </span>
        {image}
        <p className="whitespace-pre-wrap break-words">{m.content}</p>
      </div>
      {time && <time dateTime={m.createdAt} className="mt-1 pr-1 text-[10.5px] text-slate-400">{time}</time>}
    </li>
  );

  return (
    <li className="flex gap-3" data-testid="saved-message" data-role="assistant" data-job={m.jobId}>
      <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-teal-800 text-teal-50" aria-hidden>
        <GraduationCap className="h-3.5 w-3.5" />
      </span>
      <div className="min-w-0 flex-1 text-[13.5px] text-slate-800">
        <span className="sr-only">Assistant: </span>
        {image}
        <AssistantMarkdown content={m.content} />
        {!!m.actions?.length && (
          <ul className="mt-2.5 space-y-1.5">
            {m.actions.map((a, i) => {
              const failed = a.state === "error";
              const attention = a.state === "needs_review";
              return (
                <li key={i} className={`rounded-lg border px-3 py-2 text-[12px] ${failed ? "border-red-200 bg-red-50/70 text-red-800" : attention ? "border-amber-200 bg-amber-50/70 text-amber-950" : "border-slate-200 bg-white/70 text-slate-700"}`}
                  data-testid={`saved-action-${i}`} role={failed ? "alert" : undefined}>
                  <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                    <span className="font-medium">{a.label || "Earlier action"}</span>
                    {a.state && <span className="rounded-full bg-black/[0.05] px-1.5 py-px text-[10.5px]">{STATE_TEXT[a.state] ?? a.state}</span>}
                  </div>
                  {(a.resultSummary || a.error) && <p className="mt-0.5 break-words">{a.error || a.resultSummary}</p>}
                </li>
              );
            })}
          </ul>
        )}
        {!!m.sources?.length && (
          <details className="group mt-2 text-[12px]">
            <summary className="cursor-pointer select-none text-teal-800 hover:text-teal-950">Evidence ({m.sources.length})</summary>
            <div className="mt-1.5 space-y-1.5">
              {m.sources.map((s, si) => {
                const href = safeSourceUrl(s.url ?? "");
                return (
                  <blockquote key={si} className="break-words rounded-lg border border-slate-200 bg-white px-3 py-2">
                    {href ? <a className="font-medium text-teal-800 underline underline-offset-2" href={href} target="_blank" rel="noopener noreferrer nofollow">{s.title || href}</a>
                      : <span className="text-slate-700">{s.title || "Source"} (link unavailable)</span>}
                    {typeof s.page === "number" && s.page > 0 && <span className="text-slate-500"> · Page {s.page}</span>}
                    {s.excerpt && <p className="mt-1 whitespace-pre-wrap text-slate-600">{s.excerpt}</p>}
                  </blockquote>
                );
              })}
            </div>
          </details>
        )}
        <div className="mt-1 flex flex-wrap items-center gap-x-2 text-[10.5px] text-slate-400">
          {time && <time dateTime={m.createdAt}>{time}</time>}
          {otherReview && onReviewJob && (
            <button type="button" onClick={() => onReviewJob(m.jobId)}
              className="inline-flex items-center gap-1 text-teal-700 hover:text-teal-900 hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-teal-600"
              data-testid={`button-open-original-review-${m.threadId}-${m.position}`}>
              From an earlier review <ExternalLink className="h-2.5 w-2.5" aria-hidden />
            </button>
          )}
        </div>
      </div>
    </li>
  );
}
