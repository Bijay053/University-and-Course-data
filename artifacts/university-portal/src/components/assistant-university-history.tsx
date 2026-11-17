import React, { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { readResponseJson, getFetchErrorMessage } from "@/lib/readResponseJson";
import { AssistantMarkdown } from "./assistant-markdown";
import { AssistantMessageAttachment } from "./assistant-message-attachment";
import { safeSourceUrl } from "./assistant-safe-url";

type HistoryMessage = {
  threadId: string; jobId: string; position: number;
  role: "user" | "assistant"; content: string; createdAt?: string; turnId?: string;
  attachment?: { filename: string; contentType: string };
  sources?: { url?: string; title?: string; excerpt?: string; page?: number }[];
  actions?: { label?: string; state?: string; resultSummary?: string; error?: string }[];
};
type HistoryPage = {
  messages: HistoryMessage[]; nextCursor: string | null; retentionNotice: string;
};

/** Kept separate from the live conversation: historical actions cannot execute here. */
export function AssistantUniversityHistory({ jobId, onReviewJob }: {
  jobId: string; onReviewJob?: (jobId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [page, setPage] = useState<HistoryPage | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const panelId = useId();
  const retryOlder = useRef(false);
  const request = useRef<AbortController | null>(null);
  const scroll = useRef<HTMLDivElement | null>(null);
  const anchor = useRef<{ height: number; top: number } | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  useLayoutEffect(() => {
    if (!scroll.current || !anchor.current) return;
    scroll.current.scrollTop = scroll.current.scrollHeight - anchor.current.height + anchor.current.top;
    anchor.current = null;
  }, [page, open]);

  async function load(older = false) {
    if (request.current) return;
    if (older && !page?.nextCursor) return;
    retryOlder.current = older;
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setError(null);
    try {
      const query = new URLSearchParams({ limit: "20" });
      if (older && page?.nextCursor) query.set("cursor", page.nextCursor);
      const response = await fetch(`/api/scrape/jobs/${encodeURIComponent(jobId)}/assistant/history?${query}`, {
        credentials: "include", signal: controller.signal,
      });
      if (!response.ok) throw new Error(await getFetchErrorMessage(response));
      const next = await readResponseJson<HistoryPage>(response);
      if (!next || !Array.isArray(next.messages)) throw new Error("University history returned an invalid response.");
      if (controller.signal.aborted) return;
      anchor.current = older && scroll.current
        ? { height: scroll.current.scrollHeight, top: scroll.current.scrollTop }
        : { height: 0, top: 0 };
      setPage(current => {
        const entries = older ? [...(current?.messages ?? []), ...next.messages] : next.messages;
        return { ...next, messages: [...new Map(entries.map(m => [`${m.threadId}:${m.position}`, m])).values()] };
      });
    } catch (failure) {
      if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : "University history could not be loaded.");
    } finally {
      if (request.current === controller) request.current = null;
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  return (
    <section className="border-b border-slate-200 bg-white px-4 py-3" aria-label="University chat history">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="text-sm font-semibold text-slate-800">University chat history</p>
          <p className="text-xs text-slate-500">Your saved conversations across this university’s reviews. History is read-only.</p>
        </div>
        <Button variant="outline" size="sm" aria-expanded={open} aria-controls={panelId}
          onClick={() => {
            if (open && scroll.current) anchor.current = { height: scroll.current.scrollHeight, top: scroll.current.scrollTop };
            setOpen(!open);
            if (!open && !page) void load();
          }}>
          {open ? "Hide history" : "View university history"}
        </Button>
      </div>
      {open && (
        <div id={panelId} className="mt-3 space-y-2" data-testid="university-chat-history">
          <p className="text-xs text-slate-500">{page?.retentionNotice ?? "Loading retained messages. Older conversations stay separate from the current repair."}</p>
          <div className="flex flex-wrap items-center gap-2">
            {page?.nextCursor && <Button size="sm" variant="outline" disabled={busy} onClick={() => void load(true)}>Load older messages</Button>}
            {page && <Button size="sm" variant="ghost" disabled={busy} onClick={() => void load()}>Refresh history</Button>}
            {busy && <span role="status" className="text-xs text-slate-500">Loading history…</span>}
          </div>
          {error && <div role="alert" className="text-sm text-red-700">{error}
            <Button size="sm" variant="outline" disabled={busy} onClick={() => void load(retryOlder.current)}>Retry history</Button>
          </div>}
          {page && <>
            <div ref={scroll} className="max-h-96 space-y-3 overflow-y-auto rounded-lg border border-slate-200 p-3"
              style={{ overflowAnchor: "none" }} data-testid="university-history-scroll">
              {!page.messages.length && <p className="text-sm text-slate-500">No saved messages were found for your account at this university.</p>}
              {[...page.messages].reverse().map(message => (
                <article key={`${message.threadId}:${message.position}`} className="min-w-0 rounded-lg border border-slate-100 bg-slate-50 p-3 text-sm"
                  data-testid="university-history-message">
                  <div className="mb-2 flex flex-wrap items-center gap-2 text-xs text-slate-500">
                    <strong>{message.role === "user" ? "You" : "Assistant"}</strong>
                    {message.createdAt && <time dateTime={message.createdAt}>{new Date(message.createdAt).toLocaleString()}</time>}
                    <span>{message.jobId === jobId ? "Current review" : "Earlier review"}</span>
                    {onReviewJob && message.jobId !== jobId && (
                      <button className="text-teal-800 underline" onClick={() => onReviewJob(message.jobId)}>Open original review</button>
                    )}
                  </div>
                  {message.attachment && message.turnId && (
                    <AssistantMessageAttachment filename={message.attachment.filename}
                      src={`/api/scrape/jobs/${encodeURIComponent(message.jobId)}/assistant/messages/${encodeURIComponent(message.turnId)}/image`} />
                  )}
                  <div className="break-words"><AssistantMarkdown content={message.content} /></div>
                  {!!message.sources?.length && <details className="mt-2 text-xs">
                    <summary className="cursor-pointer text-teal-800">Saved evidence</summary>
                    {message.sources.map((source, index) => {
                      const href = safeSourceUrl(source.url ?? "");
                      return <blockquote key={index} className="mt-2 break-words border-l-2 border-slate-200 pl-2">
                        {href ? <a className="text-teal-800 underline" href={href} target="_blank" rel="noopener noreferrer nofollow">{source.title || href}</a> : <span>{source.title || "Source link unavailable"}</span>}
                        {source.page && <span> · Page {source.page}</span>}
                        <p className="whitespace-pre-wrap">{source.excerpt}</p>
                      </blockquote>;
                    })}
                  </details>}
                  {message.actions?.map((action, index) => (
                    <p key={index} className="mt-2 break-words text-xs text-slate-600">
                      Previous action: {action.label} · {action.state}. {action.resultSummary || action.error} <span>Read-only.</span>
                    </p>
                  ))}
                </article>
              ))}
            </div>
            {!!page.messages.length && !page.nextCursor && <p className="text-xs text-slate-500">End of retained university history.</p>}
          </>}
        </div>
      )}
    </section>
  );
}
