// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const auth = vi.hoisted(() => ({ user: { id: 1, email: "a@uni.test", name: "A", role: "staff" } as null | { id: number; email: string; name: string; role: string } }));
vi.mock("@/context/auth", () => ({ useAuth: () => ({ user: auth.user }) }));

import { ScrapePromptAssistant, type AssistantThread } from "./scrape-prompt-assistant";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const empty: AssistantThread = { threadId: null, status: "idle", phase: "", error: null, messages: [] };

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  auth.user = { id: 1, email: "a@uni.test", name: "A", role: "staff" };
});

describe("ScrapePromptAssistant", () => {
  it("submits a prompt to the job assistant endpoint", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(json(empty))
      .mockResolvedValueOnce(json({ ...empty, threadId: "t1", status: "ready", messages: [
        { role: "user", content: "Why are fees missing?", createdAt: "2024-01-01T00:00:00Z" },
      ] }));
    render(<ScrapePromptAssistant jobId="job/1" />);
    const input = await screen.findByTestId("input-assistant-prompt");
    fireEvent.change(input, { target: { value: "Why are fees missing?" } });
    fireEvent.click(screen.getByTestId("button-assistant-submit"));
    await screen.findByText("Why are fees missing?", { selector: "p" });
    const [url, init] = fetchMock.mock.calls[1];
    expect(url).toBe("/api/scrape/jobs/job%2F1/assistant");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({ prompt: "Why are fees missing?" });
  });

  it("rehydrates history with safe source links", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(json({ ...empty, status: "ready", messages: [
      { role: "assistant", content: "IELTS 6.5 found.", createdAt: "2024-01-01T00:00:00Z",
        sources: [
          { url: "https://uni.edu/english", title: "English reqs", excerpt: "IELTS 6.5 overall" },
          { url: "javascript:alert(1)", title: "Bad", excerpt: "" },
          { url: "http://uni.edu/plain", title: "Plain", excerpt: "" },
        ] },
    ] }));
    render(<ScrapePromptAssistant jobId="j1" />);
    expect(await screen.findByText("IELTS 6.5 found.")).toBeTruthy();
    expect(screen.getByTestId("link-source-0-0").getAttribute("href")).toBe("https://uni.edu/english");
    expect(screen.getByText(/Bad \(link unavailable\)/)).toBeTruthy();
    expect(screen.getByText(/Plain \(link unavailable\)/)).toBeTruthy();
  });

  it("renders model output as plain text, not HTML", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(json({ ...empty, status: "ready", messages: [
      { role: "assistant", content: "<img src=x onerror=alert(1)><b>bold</b>", createdAt: "2024-01-01T00:00:00Z" },
    ] }));
    const { container } = render(<ScrapePromptAssistant jobId="j1" />);
    expect(await screen.findByText("<img src=x onerror=alert(1)><b>bold</b>")).toBeTruthy();
    expect(container.querySelector("img, b")).toBeNull();
  });

  it("calls onChanged when an action errors after a batch started", async () => {
    const base = { id: "a2", kind: "fix_fields" as const, label: "Fix", courseCount: 2, fields: ["duration"] };
    const running: AssistantThread = { ...empty, status: "ready", messages: [
      { role: "assistant", content: "x", createdAt: "2024-01-01T00:00:00Z", actions: [{ ...base, state: "running", jobId: "b1" }] }] };
    const failed: AssistantThread = { ...running, messages: [{ ...running.messages[0], actions: [{ ...base, state: "error", jobId: "b1", error: "boom" }] }] };
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(json(running)).mockResolvedValueOnce(json(failed));
    const onChanged = vi.fn();
    render(<ScrapePromptAssistant jobId="j1" onChanged={onChanged} />);
    await screen.findByText(/Some courses may already have been updated/, {}, { timeout: 4000 });
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it("shows a permission error", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(json({ error: "forbidden" }, 403));
    render(<ScrapePromptAssistant jobId="j1" />);
    expect((await screen.findByTestId("assistant-load-error")).textContent).toMatch(/permission/i);
  });

  it("requires explicit confirmation before running an action", async () => {
    const thread: AssistantThread = { ...empty, status: "ready", messages: [
      { role: "assistant", content: "Fees can be refetched.", createdAt: "2024-01-01T00:00:00Z",
        actions: [{ id: "a1", kind: "fix_fields", label: "Refetch fees", courseCount: 4, fields: ["internationalFee"], state: "proposed" }] },
    ] };
    const done: AssistantThread = { ...thread, messages: [{ ...thread.messages[0], actions: [{ ...thread.messages[0].actions![0], state: "completed" }] }] };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(json(thread)).mockResolvedValueOnce(json(done));
    const onChanged = vi.fn();
    render(<ScrapePromptAssistant jobId="j1" onChanged={onChanged} />);
    fireEvent.click(await screen.findByTestId("button-run-action-a1"));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(await screen.findByTestId("dialog-confirm-action")).toBeTruthy();
    fireEvent.click(screen.getByTestId("button-confirm-action"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    const [url, init] = fetchMock.mock.calls[1];
    expect(url).toBe("/api/scrape/jobs/j1/assistant/actions/a1");
    expect(JSON.parse(String(init?.body))).toEqual({ confirm: true });
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
  });

  it("clears the prior user's history immediately and refetches when identity changes", async () => {
    let resolveSecond: (r: Response) => void = () => {};
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(json({ ...empty, status: "ready", messages: [
        { role: "user", content: "Private question from A", createdAt: "2024-01-01T00:00:00Z" },
      ] }))
      .mockImplementationOnce(() => new Promise<Response>(r => { resolveSecond = r; }));
    const { rerender } = render(<ScrapePromptAssistant jobId="j1" />);
    expect(await screen.findByText("Private question from A")).toBeTruthy();
    fireEvent.change(screen.getByTestId("input-assistant-prompt"), { target: { value: "draft by A" } });

    auth.user = { id: 2, email: "b@uni.test", name: "B", role: "staff" };
    rerender(<ScrapePromptAssistant jobId="j1" />);
    // Gone synchronously on the very next render, before B's history arrives.
    expect(screen.queryByText("Private question from A")).toBeNull();
    expect(screen.queryByDisplayValue("draft by A")).toBeNull();
    expect(screen.getByTestId("assistant-loading")).toBeTruthy();
    const firstSignal = fetchMock.mock.calls[0][1]?.signal;
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(firstSignal?.aborted).toBe(true);

    resolveSecond(json(empty));
    expect(await screen.findByTestId("assistant-empty")).toBeTruthy();
    expect(screen.queryByText("Private question from A")).toBeNull();
  });

  it("shows nothing and does not fetch when signed out", () => {
    auth.user = null;
    const fetchMock = vi.spyOn(globalThis, "fetch");
    render(<ScrapePromptAssistant jobId="j1" />);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.queryByTestId("assistant-history")).toBeNull();
  });

  it("offers an explicit review button for terminal actions with a jobId and never auto-navigates", async () => {
    const thread: AssistantThread = { ...empty, status: "ready", messages: [
      { role: "assistant", content: "Repair done.", createdAt: "2024-01-01T00:00:00Z", actions: [
        { id: "r1", kind: "repair_catalogue", label: "Repair", courseCount: 3, fields: [], state: "completed", jobId: "verify-child" },
        { id: "f1", kind: "fix_fields", label: "Fix", courseCount: 1, fields: ["ielts_overall"], state: "error", jobId: "fix-job", error: "partial" },
        { id: "p1", kind: "fix_fields", label: "Pending", courseCount: 1, fields: ["duration"], state: "proposed" },
      ] },
    ] };
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(json(thread));
    const onReviewJob = vi.fn();
    render(<ScrapePromptAssistant jobId="j1" onReviewJob={onReviewJob} />);
    const btn = await screen.findByTestId("button-open-review-r1");
    expect(btn.textContent).toMatch(/verification review/i);
    expect(screen.getByTestId("button-open-review-f1")).toBeTruthy();
    expect(screen.queryByTestId("button-open-review-p1")).toBeNull();
    expect(onReviewJob).not.toHaveBeenCalled();
    expect(screen.getByText(/1 course · IELTS overall and bands/)).toBeTruthy();
    fireEvent.click(btn);
    expect(onReviewJob).toHaveBeenCalledWith("verify-child");
  });

  it("hides the review button when no onReviewJob handler is provided", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(json({ ...empty, status: "ready", messages: [
      { role: "assistant", content: "x", createdAt: "2024-01-01T00:00:00Z", actions: [
        { id: "r1", kind: "repair_catalogue", label: "Repair", courseCount: 3, fields: [], state: "completed", jobId: "c" }] },
    ] }));
    render(<ScrapePromptAssistant jobId="j1" />);
    await screen.findByTestId("assistant-action-r1");
    expect(screen.queryByTestId("button-open-review-r1")).toBeNull();
  });
});
