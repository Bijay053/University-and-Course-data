// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const auth = vi.hoisted(() => ({ user: { id: 1, email: "a@uni.test", name: "A", role: "staff" } as null | { id: number; email: string; name: string; role: string } }));
vi.mock("@/context/auth", () => ({ useAuth: () => ({ user: auth.user }) }));
vi.mock("./assistant-import-status", () => ({ AssistantImportStatus: () => null }));

import { buildTimeline, mergeSaved, projectLive, type SavedMessage } from "./assistant-timeline";
import { useAssistantHistory } from "./use-assistant-history";
import { ScrapePromptAssistant, type AssistantMessage, type AssistantThread } from "./scrape-prompt-assistant";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const saved = (position: number, extra: Partial<SavedMessage> = {}): SavedMessage => ({
  threadId: "t-old", jobId: "old-job", position, role: position % 2 ? "assistant" : "user",
  content: `Saved ${position}`, createdAt: `2026-01-0${1 + Math.floor(position / 4)}T0${position % 4}:00:00Z`, ...extra,
});

afterEach(() => { cleanup(); vi.restoreAllMocks(); auth.user = { id: 1, email: "a@uni.test", name: "A", role: "staff" }; });

describe("timeline merging", () => {
  it("dedupes saved pages by threadId+position and sorts oldest first", () => {
    const merged = mergeSaved([saved(2), saved(3)], [saved(0), saved(1), saved(2, { content: "newer copy" })]);
    expect(merged.map(m => m.position)).toEqual([0, 1, 2, 3]);
    expect(merged[2].content).toBe("newer copy");
  });

  it("prefers live messages over saved copies, with a turnId+role fallback", () => {
    const live = [
      { threadId: "t-live", jobId: "j1", position: 0, role: "user" as const, content: "Live question", createdAt: "2026-02-01T00:00:00Z" },
      { role: "assistant" as const, content: "Legacy live reply", createdAt: "2026-02-01T00:01:00Z", turnId: "turn-9" },
    ];
    const history = [
      saved(0),
      { ...saved(1), threadId: "t-live", jobId: "j1", position: 0, role: "user" as const, content: "stale" },
      { ...saved(3), threadId: "t-live", jobId: "j1", position: 1, role: "assistant" as const, turnId: "turn-9", content: "stale reply" },
    ];
    const t = buildTimeline(history, live);
    expect(t.map(e => e.kind === "live" ? e.message.content : `saved:${e.message.content}`))
      .toEqual(["saved:Saved 0", "Live question", "Legacy live reply"]);
  });
});

it("offers a jump to latest while reading older messages even without a new reply", async () => {
  vi.spyOn(globalThis, "fetch").mockImplementation(async url =>
    String(url).includes("/history")
      ? json({ messages: [], nextCursor: null })
      : json({ threadId: "current", status: "ready", messages: [
        { role: "assistant", content: "Newest response", createdAt: "2026-11-17T01:00:00Z" },
      ] }));
  render(<ScrapePromptAssistant jobId="j1" />);
  await screen.findByText("Newest response");
  const scroll = screen.getByTestId("assistant-scroll");
  Object.defineProperties(scroll, {
    scrollHeight: { value: 2000, configurable: true },
    clientHeight: { value: 400, configurable: true },
    scrollTop: { value: 0, writable: true, configurable: true },
  });
  fireEvent.scroll(scroll);
  fireEvent.click(await screen.findByTestId("button-jump-latest"));
  expect(screen.queryByTestId("button-jump-latest")).toBeNull();
});

it("keeps saved university messages available when the current conversation request fails", async () => {
  vi.spyOn(globalThis, "fetch").mockImplementation(async url =>
    String(url).includes("/history")
      ? json({ messages: [saved(1, { content: "Earlier conversation remains available" })], nextCursor: null })
      : json({ detail: "Current conversation is temporarily unavailable" }, 503));
  render(<ScrapePromptAssistant jobId="j1" />);
  await screen.findByTestId("assistant-load-error");
  await screen.findByText("Earlier conversation remains available");
  expect(screen.getByTestId("assistant-scroll")).toBeTruthy();
  expect(screen.queryByTestId("button-run-action-undefined")).toBeNull();
});

describe("stable identities", () => {
  it("keeps multiple assistant messages of one turn when identities are stable", () => {
    const live: AssistantMessage[] = [
      { threadId: "t", jobId: "j1", position: 4, role: "assistant", turnId: "turn-1", content: "Image observation", createdAt: "2026-02-01T00:00:00Z" },
      { threadId: "t", jobId: "j1", position: 5, role: "assistant", turnId: "turn-1", content: "Final reply", createdAt: "2026-02-01T00:00:01Z" },
    ];
    const history = [
      { ...saved(0), threadId: "t", jobId: "j1", position: 2, role: "assistant" as const, turnId: "turn-1", content: "Earlier saved same-turn note" },
    ];
    const contents = buildTimeline(history, live).map(e => e.message.content);
    expect(contents).toEqual(["Earlier saved same-turn note", "Image observation", "Final reply"]);
  });

  it("never retains executable action ids in projections", () => {
    const p = projectLive({ threadId: "t", jobId: "j1", position: 1, role: "assistant", content: "x", createdAt: "2026-02-01T00:00:00Z",
      actions: [{ id: "exec-123", kind: "fix_fields", label: "Fix", state: "proposed", courseCount: 1, fields: [], requiresConfirmation: true }] }, "j1");
    expect(JSON.stringify(p)).not.toContain("exec-123");
    expect(p?.actions?.[0]).toEqual({ kind: "fix_fields", label: "Fix", state: "proposed", courseCount: 1 });
  });
});

describe("useAssistantHistory", () => {
  it("retains every observed live window across a 45-message progression without gaps", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(json({ messages: [], nextCursor: null, retentionNotice: "" }));
    const msg = (p: number): AssistantMessage => ({ threadId: "t", jobId: "j1", position: p, role: p % 2 ? "assistant" : "user",
      content: `m${p}`, createdAt: new Date(Date.UTC(2026, 1, 1, 0, p)).toISOString(),
      actions: p % 2 ? [{ id: `exec-${p}`, kind: "fix_fields", label: "Fix", state: "completed", courseCount: 1, fields: [] }] : undefined });
    const win = (from: number, to: number) => Array.from({ length: to - from + 1 }, (_, i) => msg(from + i));
    const { result, rerender } = renderHook(({ k }) => useAssistantHistory("j1", k), { initialProps: { k: "1:a\u0000j1" } });
    await waitFor(() => expect(result.current.loaded).toBe(true));
    let last: AssistantMessage[] = [];
    for (let end = 0; end <= 44; end += 3) {
      last = win(Math.max(0, end - 19), end);
      act(() => result.current.retainLive("1:a\u0000j1", last));
    }
    last = win(25, 44);
    act(() => result.current.retainLive("1:a\u0000j1", last));
    const t = buildTimeline(result.current.messages, last);
    expect(t.map(e => e.message.content)).toEqual(Array.from({ length: 45 }, (_, i) => `m${i}`));
    expect(JSON.stringify(result.current.messages)).not.toContain("exec-");
    expect(result.current.hasMore).toBe(false);
    // Another owner's view never sees retained messages, and stale keys are ignored.
    rerender({ k: "2:b\u0000j1" });
    expect(result.current.messages).toHaveLength(0);
    act(() => result.current.retainLive("1:a\u0000j1", win(0, 3)));
    expect(result.current.messages).toHaveLength(0);
  });


  it("loads latest-first pages, appends older pages, retains them on failure and retries the same cursor", async () => {
    const fetcher = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(json({ messages: [saved(3), saved(2)], nextCursor: "c1", retentionNotice: "", universityName: "Aston University" }))
      .mockResolvedValueOnce(json({ error: "boom" }, 500))
      .mockResolvedValueOnce(json({ messages: [saved(1), saved(0)], nextCursor: null, retentionNotice: "" }));
    const { result } = renderHook(() => useAssistantHistory("job/1", "1:a\u0000job/1"));
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(String(fetcher.mock.calls[0][0])).toBe("/api/scrape/jobs/job%2F1/assistant/history?limit=20");
    expect(result.current.universityName).toBe("Aston University");
    expect(result.current.hasMore).toBe(true);
    act(() => result.current.loadOlder());
    await waitFor(() => expect(result.current.error).toBeTruthy());
    expect(result.current.messages.map(m => m.position)).toEqual([2, 3]);
    act(() => result.current.retry());
    await waitFor(() => expect(result.current.messages).toHaveLength(4));
    expect(String(fetcher.mock.calls[2][0])).toContain("cursor=c1");
    expect(result.current.hasMore).toBe(false);
    expect(fetcher.mock.calls.every(([, init]) => !init?.method || init.method === "GET")).toBe(true);
  });

  it("aborts and hides another identity's pages on switch", async () => {
    let first: AbortSignal | undefined;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (_u, init) => {
      if (!first) { first = init?.signal ?? undefined; return new Promise<Response>(() => {}); }
      return json({ messages: [saved(0, { content: "Mine" })], nextCursor: null, retentionNotice: "" });
    });
    const { result, rerender } = renderHook(({ k }) => useAssistantHistory("j1", k), { initialProps: { k: "1:a\u0000j1" } });
    rerender({ k: "2:b\u0000j1" });
    expect(first?.aborted).toBe(true);
    await waitFor(() => expect(result.current.messages.map(m => m.content)).toEqual(["Mine"]));
  });
});

describe("ScrapePromptAssistant continuous transcript", () => {
  it("renders saved and live messages in one scroll area without executable historical actions", async () => {
    const live: AssistantThread = { threadId: "t-live", status: "ready", phase: "", error: null, universityName: "Aston University",
      messages: [{ threadId: "t-live", jobId: "j1", position: 0, role: "user", content: "Why are IELTS rows missing?", createdAt: "2026-02-01T00:00:00Z" }] };
    vi.spyOn(globalThis, "fetch").mockImplementation(async url => String(url).includes("/history")
      ? json({ messages: [
          saved(1, { content: "I checked 149 records.", actions: [{ label: "IELTS repair", state: "completed", resultSummary: "149 unchanged." }] }),
          saved(0, { content: "Check English requirements.", turnId: "turn-a", attachment: { filename: "a.png", contentType: "image/png" } }),
        ], nextCursor: null, retentionNotice: "", universityName: "Aston University" })
      : json(live));
    const review = vi.fn();
    render(<ScrapePromptAssistant jobId="j1" onReviewJob={review} />);
    const scroll = await screen.findByTestId("assistant-scroll");
    await within(scroll).findByText("I checked 149 records.");
    expect(within(scroll).getByText("Why are IELTS rows missing?")).toBeTruthy();
    expect(within(scroll).getByText("149 unchanged.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Run now/ })).toBeNull();
    expect(screen.getByTestId("text-assistant-title").textContent).toBe("Aston University");
    expect(screen.getAllByTestId("assistant-scroll")).toHaveLength(1);
    const img = scroll.querySelector("img");
    if (img) expect(img.getAttribute("src")).toContain("/api/scrape/jobs/old-job/assistant/messages/turn-a/image");
    fireEvent.click(screen.getByTestId("button-open-original-review-t-old-1"));
    expect(review).toHaveBeenCalledWith("old-job");
    await screen.findByTestId("text-history-start");
  });

  it("shows a small retry when the saved conversation fails without hiding the live thread", async () => {
    const live: AssistantThread = { threadId: "t", status: "ready", phase: "", error: null,
      messages: [{ role: "assistant", content: "Live reply", createdAt: "2026-02-01T00:00:00Z" }] };
    let failures = 1;
    vi.spyOn(globalThis, "fetch").mockImplementation(async url => {
      if (!String(url).includes("/history")) return json(live);
      if (failures-- > 0) return json({ error: "down" }, 503);
      return json({ messages: [saved(0, { content: "Recovered earlier" })], nextCursor: null, retentionNotice: "" });
    });
    render(<ScrapePromptAssistant jobId="j1" />);
    await screen.findByText("Live reply");
    fireEvent.click(await screen.findByTestId("button-history-retry"));
    await screen.findByText("Recovered earlier");
  });
});
