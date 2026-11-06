// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const auth = vi.hoisted(() => ({ user: { id: 1, email: "a@uni.test", name: "A", role: "staff" } as null | { id: number; email: string; name: string; role: string } }));
const importHold = vi.hoisted(() => ({paused: false}));
vi.mock("@/context/auth", () => ({ useAuth: () => ({ user: auth.user }) }));
// Status transport has its own integration tests; keep these chat fixtures scoped
// to conversation requests rather than consuming them with background polling.
vi.mock("./assistant-import-status", async () => {
  const { useEffect } = await import("react");
  return { AssistantImportStatus: ({onStatus}: {onStatus: (paused: boolean) => void}) => {
    useEffect(() => onStatus(importHold.paused), [onStatus]);
    return null;
  }};
});

import { ScrapePromptAssistant, type AssistantThread } from "./scrape-prompt-assistant";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const empty: AssistantThread = { threadId: null, status: "idle", phase: "", error: null, messages: [] };

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  auth.user = { id: 1, email: "a@uni.test", name: "A", role: "staff" };
  importHold.paused = false;
});

describe("ScrapePromptAssistant", () => {
  it("confirms a large selected-failure retry and shows exact limits without claiming repair", async () => {
    const thread: AssistantThread = {...empty, status: "ready", messages: [
      {role: "assistant", content: "Selected failures only.", createdAt: "2024-01-01T00:00:00Z", actions: [
        {id: "retry", label: "Retry selected failures", kind: "retry_failed", state: "proposed", courseCount: 11, fields: [],
         requiresConfirmation: true, courseResults: [{url: "https://uni.edu/failed", status: "selected",
           message: "At most 2 remaining extraction attempts; backoff at least 7s."}]}]}]};
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(json(thread)).mockResolvedValueOnce(json(thread));
    render(<ScrapePromptAssistant jobId="j1" />);
    fireEvent.click(await screen.findByTestId("button-run-action-retry"));
    expect(screen.getByTestId("dialog-confirm-action").textContent).toContain("Successful URLs are not replayed");
    expect(screen.getByText(/At most 2 remaining/)).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByTestId("button-confirm-action"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({confirm: true});
  });
  it("requires a separate confirmation for job-only tuning and waits for acknowledgement", async () => {
    const proposed = {
      ...empty, threadId: "runtime", status: "ready",
      messages: [{ role: "assistant", content: "Confirm this job-only preview", createdAt: new Date().toISOString(),
        actions: [{ id: "rt", kind: "runtime_tuning", state: "proposed",
          label: "Concurrency 4 to 2", courseCount: 0, fields: [], requiresConfirmation: true }] }],
    };
    const fetcher = vi.spyOn(globalThis, "fetch").mockImplementation(async (_url, options) => {
      if (options?.method === "POST") return json({
        ...proposed, messages: [{ ...proposed.messages[0], actions: [{
          ...proposed.messages[0].actions[0], state: "running", resultSummary: "Waiting for the owning worker acknowledgement.",
        }] }],
      });
      return json(proposed);
    });
    render(<ScrapePromptAssistant jobId="job-only" />);
    fireEvent.click(await screen.findByTestId("button-run-action-rt"));
    expect(await screen.findByTestId("dialog-confirm-action")).toBeTruthy();
    expect(screen.getByText(/Active requests finish normally/)).toBeTruthy();
    expect(fetcher.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(0);
    fireEvent.click(screen.getByTestId("button-confirm-action"));
    await waitFor(() => expect(screen.getByTestId("status-action-rt").textContent).toBe("running"));
    const post = fetcher.mock.calls.find(([, options]) => options?.method === "POST")!;
    expect(post[0]).toBe("/api/scrape/jobs/job-only/assistant/actions/rt");
    expect(JSON.parse(String(post[1]?.body))).toEqual({ confirm: true });
    expect(screen.queryByText(/Worker acknowledged/)).toBeNull();
  });
  it("disables imports during a hold without disabling pending field repairs", async () => {
    importHold.paused = true;
    vi.spyOn(globalThis, "fetch").mockResolvedValue(json({
      ...empty, status: "ready", messages: [{role: "assistant", content: "Ready", createdAt: new Date().toISOString(), actions: [
        {id: "held", kind: "scan_catalogue", state: "proposed", courseCount: 1, fields: []},
        {id: "repair", kind: "fix_fields", state: "proposed", courseCount: 1, fields: ["duration"]},
      ]}],
    }));
    render(<ScrapePromptAssistant jobId="j1" />);
    expect((await screen.findByTestId("button-run-action-held") as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByTestId("button-run-action-repair") as HTMLButtonElement).disabled).toBe(false);
  });
  it("restores a saved location undo through confirmation and refreshes review data", async () => {
    const saved: AssistantThread = { ...empty, threadId: "saved", status: "ready",
      messages: [{ role: "assistant", content: "Locations saved.", createdAt: "2024-01-01T00:00:00Z",
        actions: [{ id: "undo-location", kind: "undo_staff_location", label: "Undo staff-provided locations",
          state: "proposed", courseCount: 2, publishedCount: 1, fields: ["course_location"], requiresConfirmation: true }] }] };
    const completed = structuredClone(saved);
    completed.messages[0].actions![0].state = "completed";
    completed.messages[0].actions![0].resultSummary = "Restored 2 prior locations and provenance, with audit records.";
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (_url, init) =>
      json(init?.method === "POST" ? completed : saved));
    const changed = vi.fn();
    render(<ScrapePromptAssistant jobId="source" onChanged={changed} />);
    fireEvent.click(await screen.findByRole("button", { name: "Undo locations" }));
    expect(await screen.findByTestId("dialog-confirm-action")).toBeTruthy();
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Confirm and start" }));
    await screen.findByText("Restored 2 prior locations and provenance, with audit records.");
    expect(changed).toHaveBeenCalled();
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
    expect(String(post?.[0])).toContain("/actions/undo-location");
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({ confirm: true });
    expect(screen.queryByRole("button", { name: "Undo locations" })).toBeNull();
  });
  it("shows partial recovery and each exclusion inline with the resulting review link", async () => {
    const messages = [
      "Verified in pending review.",
      "Excluded by the international-student eligibility check.",
      "Excluded because the course is online-only.",
      "The source page could not be fetched; eligibility was not verified.",
      "Extraction did not complete; no pending course was verified.",
      "No pending course or conclusive failure reason was found; eligibility is unknown.",
    ];
    const saved: AssistantThread = { ...empty, status: "ready", threadId: "saved",
      messages: [{ role: "assistant", content: "Check finished.", createdAt: "2024-01-01T00:00:00Z",
        actions: [{ id: "recovery", kind: "review_courses", label: "Check requested courses",
          state: "error", courseCount: 6, fields: [], jobId: "result-job",
          error: "Partial recovery: 1 of 6 requested courses verified in pending review. Nothing was published.",
          courseResults: messages.map((message, index) => ({
            url: `https://example.edu/course-${index}`, status: index ? "unverified" : "recovered", message,
          })) }] }] };
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => json(saved));
    const openReview = vi.fn();
    const first = render(<ScrapePromptAssistant jobId="source" onReviewJob={openReview} />);
    await screen.findByTestId("course-results-recovery");
    for (const message of messages) expect(screen.getByText(message)).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toContain("Partial recovery: 1 of 6");
    expect(screen.queryByText(/Some courses may already have been updated/)).toBeNull();
    fireEvent.click(screen.getByTestId("button-open-review-recovery"));
    expect(openReview).toHaveBeenCalledWith("result-job");
    first.unmount();
    render(<ScrapePromptAssistant jobId="source" onReviewJob={openReview} />);
    await screen.findByTestId("course-results-recovery");
    for (const message of messages) expect(screen.getByText(message)).toBeTruthy();
  });

  it("restores PDF page links and aligned table excerpts after reloading", async () => {
    const excerpt = "Course           Annual       Full Course\nBachelor Arts    GBP 18,900    GBP 56,700";
    const saved: AssistantThread = { ...empty, threadId: "pdf-thread", status: "ready",
      messages: [{ role: "assistant", content: "Read the official tuition schedule.", createdAt: "2024-01-01T00:00:00Z",
        sources: [{ url: "https://uni.edu/fees.pdf", title: "Tuition schedule", page: 2,
          quoteId: "pdf-immutable-p2-q0", excerpt }],
        activity: [{ id: "pdf-check", at: "2024-01-01T00:00:00Z", label: "Official PDF checked",
          status: "completed", detail: "Read 2/2 PDF pages." }] }] };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async () => json(saved));
    const first = render(<ScrapePromptAssistant jobId="j1" />);
    expect((await screen.findByTestId("link-source-0-0")).getAttribute("href")).toBe("https://uni.edu/fees.pdf#page=2");
    expect(screen.getByText("· Page 2")).toBeTruthy();
    first.unmount();
    render(<ScrapePromptAssistant jobId="j1" />);
    const restored = await screen.findByTestId("link-source-0-0");
    expect(restored.textContent).toContain("Page 2");
    expect(restored.getAttribute("href")).toBe("https://uni.edu/fees.pdf#page=2");
    const quote = restored.closest("blockquote")?.querySelector("p");
    expect(quote?.textContent).toBe(excerpt);
    expect(quote?.className).toContain("whitespace-pre");
    expect(quote?.className).toContain("font-mono");
    expect(await screen.findByTestId("activity-item-pdf-check")).toBeTruthy();
    expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it("never turns an unsafe PDF URL or invalid page into a page link", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(json({ ...empty, status: "ready", messages: [
      { role: "assistant", content: "PDF guidance.", createdAt: "2024-01-01T00:00:00Z", sources: [
        { url: "javascript:alert(1)", title: "Unsafe PDF", excerpt: "Untrusted", page: 2 },
        { url: "https://uni.edu/fees.pdf", title: "No page", excerpt: "Text", page: -1 },
        { url: "https://uni.edu/fees.pdf", title: "Injected page", excerpt: "Text", page: "2&x=bad" },
      ] },
    ] }));
    render(<ScrapePromptAssistant jobId="j1" />);
    expect((await screen.findByTestId("link-source-0-1")).getAttribute("href")).toBe("https://uni.edu/fees.pdf");
    expect(screen.queryByTestId("link-source-0-0")).toBeNull();
    expect(screen.getByTestId("link-source-0-2").getAttribute("href")).toBe("https://uni.edu/fees.pdf");
    expect(screen.queryByText("Page -1")).toBeNull();
  });

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

  it("refreshes the review and renders saved domestic exclusions without a run button", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const saved: AssistantThread = { ...empty, status: "ready", messages: [{
      role: "assistant", createdAt: "2024-01-01T00:00:00Z",
      content: "Excluded 1 confirmed domestic-only record.",
      actions: [{ id: "exclude-1", kind: "exclude_domestic", label: "Pending eligibility check",
        state: "completed", courseCount: 2, fields: [],
        resultSummary: "One excluded; one retained.",
        courseResults: [
          { url: "https://uni.edu/a", status: "excluded", message: "Course A — explicit domestic-only evidence." },
          { url: "https://uni.edu/b", status: "retained", message: "Course B — source unavailable." },
        ],
      }],
    }] };
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(json({ ...empty, threadId: "eligibility-thread", status: "researching",
        activity: [{ id: "eligibility-running", at: "2024-01-01T00:00:00Z",
          label: "Checking eligibility", status: "running" }], messages: [] }))
      .mockResolvedValue(json(saved));
    const onChanged = vi.fn();
    render(<ScrapePromptAssistant jobId="j1" onChanged={onChanged} />);
    expect(await screen.findByTestId("activity-item-eligibility-running")).toBeTruthy();
    await vi.advanceTimersByTimeAsync(1500);
    expect(await screen.findByText("One excluded; one retained.")).toBeTruthy();
    expect(screen.getByText("Course B — source unavailable.")).toBeTruthy();
    expect(screen.queryByTestId("button-run-action-exclude-1")).toBeNull();
    expect(onChanged).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
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

  it.each(["fix_fields", "review_courses"] as const)("runs legacy %s without another confirmation", async (kind) => {
    const thread: AssistantThread = { ...empty, status: "ready", messages: [
      { role: "assistant", content: "Fees can be refetched.", createdAt: "2024-01-01T00:00:00Z",
        actions: [{ id: "a1", kind, label: kind === "review_courses" ? "Fetch requested courses" : "Refetch fees", courseCount: 4, fields: kind === "review_courses" ? [] : ["internationalFee"], state: "proposed" }] },
    ] };
    const done: AssistantThread = { ...thread, messages: [{ ...thread.messages[0], actions: [{ ...thread.messages[0].actions![0], state: "completed" }] }] };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(json(thread)).mockResolvedValueOnce(json(done));
    const onChanged = vi.fn();
    render(<ScrapePromptAssistant jobId="j1" onChanged={onChanged} />);
    fireEvent.click(await screen.findByTestId("button-run-action-a1"));
    expect(screen.queryByTestId("dialog-confirm-action")).toBeNull();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    const [url, init] = fetchMock.mock.calls[1];
    expect(url).toBe("/api/scrape/jobs/j1/assistant/actions/a1");
    expect(JSON.parse(String(init?.body))).toEqual({ confirm: true });
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
  });

  it("explains staff provenance and published records before confirmation", async () => {
    const thread: AssistantThread = { ...empty, status: "ready", messages: [
      { role: "assistant", content: "Tuition preview", createdAt: "2024-01-01T00:00:00Z",
        actions: [{ id: "staff1", kind: "staff_edit", label: "Apply staff values", courseCount: 2,
          publishedCount: 1, fields: ["international_fee"], state: "proposed" }] },
    ] };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(json(thread));
    render(<ScrapePromptAssistant jobId="j1" />);
    fireEvent.click(await screen.findByTestId("button-run-action-staff1"));
    const dialog = await screen.findByTestId("dialog-confirm-action");
    expect(dialog.textContent).toContain("staff-provided provenance");
    expect(dialog.textContent).toContain("including 1 published records");
    expect(dialog.textContent).toContain("does not independently verify");
    expect(fetchMock).toHaveBeenCalledTimes(1);
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

  it("renders archived events inline between the prompt and the reply, with no separate activity panel", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(json({ ...empty, status: "ready", turnId: "t1",
      activity: [
        { id: "x1", at: "2024-01-01T00:00:01Z", label: "Fetched fees page", url: "https://uni.edu/fees", status: "completed", durationMs: 1500 },
        { id: "x3", at: "2024-01-01T00:00:03Z", label: "Answer ready", status: "completed" },
      ],
      messages: [
        { role: "user", content: "Why fees?", createdAt: "2024-01-01T00:00:00Z", turnId: "t1" },
        { role: "assistant", content: "Found.", createdAt: "2024-01-01T00:00:04Z", turnId: "t1", activity: [
          { id: "x1", at: "2024-01-01T00:00:01Z", label: "Fetched fees page", url: "https://uni.edu/fees", status: "completed", durationMs: 1500 },
          { id: "x2", at: "2024-01-01T00:00:02Z", label: "Fetch failed", detail: "timeout", url: "javascript:alert(1)", status: "error", durationMs: 30000 },
          { id: "x3", at: "2024-01-01T00:00:03Z", label: "Answer ready", status: "completed" },
        ] },
      ] }));
    const { container } = render(<ScrapePromptAssistant jobId="j1" />);
    await screen.findByText("Found.");
    const order = Array.from(container.querySelectorAll("[data-testid^='assistant-message-'],[data-testid^='activity-item-']")).map(e => e.getAttribute("data-testid"));
    expect(order).toEqual(["assistant-message-0", "activity-item-x1", "activity-item-x2", "activity-item-x3", "assistant-message-1"]);
    expect(screen.getAllByText("Answer ready")).toHaveLength(1);
    expect(screen.queryByTestId("button-toggle-activity")).toBeNull();
    expect(screen.queryByTestId("assistant-activity")).toBeNull();
    expect(screen.getByTestId("link-activity-x1").getAttribute("href")).toBe("https://uni.edu/fees");
    expect(screen.queryByTestId("link-activity-x2")).toBeNull();
    expect(screen.getByText("(link unavailable)")).toBeTruthy();
    expect(screen.getByTestId("text-activity-duration-x1").textContent).toBe("1.5s");
    expect(screen.getByText("timeout")).toBeTruthy();
  });

  it("handles legacy threads without activity or turn ids", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(json({ ...empty, status: "ready", messages: [
      { role: "assistant", content: "old", createdAt: "2024-01-01T00:00:00Z" }] }));
    render(<ScrapePromptAssistant jobId="j1" />);
    await screen.findByText("old");
    expect(screen.queryByTestId(/^activity-item-/)).toBeNull();
  });

  it("renders fenced code and markdown safely as text", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(json({ ...empty, status: "ready", messages: [
      { role: "assistant", createdAt: "2024-01-01T00:00:00Z",
        content: "**Note** see [page](https://uni.edu/a) and [bad](javascript:alert(1))\n```html\n<script>alert(1)</script>\n```" }] }));
    const { container } = render(<ScrapePromptAssistant jobId="j1" />);
    const code = await screen.findByTestId("assistant-code-block");
    expect(code.textContent).toContain("<script>alert(1)</script>");
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("a[href^='javascript']")).toBeNull();
    expect(screen.getByText("page").getAttribute("href")).toBe("https://uni.edu/a");
    expect(screen.getByText("Note").tagName).toBe("STRONG");
  });

  it("shows public operation details JSON without private fields", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(json({ ...empty, status: "ready", messages: [
      { role: "assistant", content: "x", createdAt: "2024-01-01T00:00:00Z", actions: [
        { id: "a1", kind: "fix_fields", label: "Fix", courseCount: 2, fields: ["duration"], state: "proposed", _secret: "s" }] }] }));
    render(<ScrapePromptAssistant jobId="j1" />);
    const code = await screen.findByTestId("code-operation-a1");
    const parsed = JSON.parse(code.querySelector("pre")!.textContent!);
    expect(parsed).toEqual({ kind: "fix_fields", state: "proposed", label: "Fix", fields: ["duration"], courseCount: 2 });
    expect(screen.getByText("Operation details")).toBeTruthy();
  });

  it("sends long prompts unchanged, Enter sends, Shift+Enter and IME do not", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(json(empty))
      .mockImplementation(async () => json({ ...empty, status: "ready" }));
    render(<ScrapePromptAssistant jobId="j1" />);
    const input = await screen.findByTestId("input-assistant-prompt");
    expect(input.getAttribute("maxLength")).toBeNull();
    const long = "a".repeat(2600);
    fireEvent.change(input, { target: { value: long } });
    fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
    fireEvent.keyDown(input, { key: "Enter", isComposing: true });
    fireEvent.keyDown(input, { key: "Enter", keyCode: 229 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({ prompt: long });
  });

  it("keeps the draft and shows an error when sending fails", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(json(empty)).mockResolvedValueOnce(json({ error: "nope" }, 500));
    render(<ScrapePromptAssistant jobId="j1" />);
    const input = await screen.findByTestId("input-assistant-prompt");
    fireEvent.change(input, { target: { value: "yes" } });
    fireEvent.click(screen.getByTestId("button-assistant-submit"));
    await screen.findByTestId("assistant-submit-error");
    expect((input as HTMLTextAreaElement).value).toBe("yes");
  });

  it("shows live events inline with elapsed time, polls at 1s and stops on unmount", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date("2024-01-01T00:00:12Z"));
    const running = { ...empty, threadId: "t", status: "researching", phase: "Reading", turnId: "t2", startedAt: "2024-01-01T00:00:00Z",
      activity: [{ id: "s1", at: "2024-01-01T00:00:01Z", label: "Searching official site", status: "running" }],
      messages: [{ role: "user", content: "Check", createdAt: "2024-01-01T00:00:00Z", turnId: "t2" }] };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async () => json(running));
    const { unmount } = render(<ScrapePromptAssistant jobId="j1" />);
    expect(await screen.findByTestId("activity-item-s1")).toBeTruthy();
    expect(screen.getByTestId("text-activity-current").textContent).toBe("Searching official site");
    expect(screen.getByTestId("text-activity-elapsed").textContent).toMatch(/^Researching 1[2-3]s$/);
    const before = fetchMock.mock.calls.length;
    await vi.advanceTimersByTimeAsync(1100);
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(before));
    unmount();
    const after = fetchMock.mock.calls.length;
    await vi.advanceTimersByTimeAsync(3000);
    expect(fetchMock.mock.calls.length).toBe(after);
    vi.useRealTimers();
  });

  it("offers related pending reviews via onReviewJob, ignoring current and duplicate jobs", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(json({ ...empty, status: "ready",
      relatedReviews: [
        { jobId: "other", courseName: "Bachelor of Nursing", status: "pending" },
        { jobId: "other", courseName: "Dup", status: "pending" },
        { jobId: "j1", courseName: "Self", status: "pending" },
      ],
      messages: [{ role: "assistant", content: "Missing.", createdAt: "2024-01-01T00:00:00Z", actions: [
        { id: "f1", kind: "fix_fields", label: "Fix", courseCount: 1, fields: [], state: "error", error: "not found" }] }] }));
    const onReviewJob = vi.fn();
    render(<ScrapePromptAssistant jobId="j1" onReviewJob={onReviewJob} />);
    const btn = await screen.findByTestId("button-open-related-review-other");
    expect(screen.queryByTestId("button-open-related-review-j1")).toBeNull();
    expect(screen.getAllByTestId(/^related-review-/)).toHaveLength(1);
    expect(screen.getByTestId("status-action-f1").textContent).toBe("error");
    expect(onReviewJob).not.toHaveBeenCalled();
    fireEvent.click(btn);
    expect(onReviewJob).toHaveBeenCalledWith("other");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("omits related reviews for old servers or without a navigation handler", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(json({ ...empty, status: "ready",
      relatedReviews: [{ jobId: "other", courseName: "X", status: "pending" }], messages: [] }));
    render(<ScrapePromptAssistant jobId="j1" />);
    await screen.findByTestId("assistant-empty");
    expect(screen.queryByTestId("assistant-related-reviews")).toBeNull();
  });

  it("renders a partial catalogue comparison with counts, rows and safe links", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(json({ ...empty, status: "ready", messages: [
      { role: "assistant", content: "Compared.", createdAt: "2024-01-01T00:00:00Z", comparison: {
        complete: false, note: "Page 3 timed out.", observed: 3,
        counts: { published: 1, pending: 1, recorded: 0, missing: 1 },
        sources: [{ url: "https://uni.edu/courses", status: "ok" }, { url: "javascript:alert(1)", status: "error" }],
        courses: [
          { url: "https://uni.edu/c/a", title: "BA Arts", status: "published", courseIds: [7], reviewJobIds: [] },
          { url: "https://uni.edu/c/b", title: "BSc Nursing", status: "pending", courseIds: [], reviewJobIds: ["rj1"] },
          { url: "https://uni.edu/c/c", title: "MBA", status: "missing", courseIds: [], reviewJobIds: [] },
        ] } }] }));
    const onReviewJob = vi.fn();
    const { container } = render(<ScrapePromptAssistant jobId="j1" onReviewJob={onReviewJob} />);
    expect((await screen.findByTestId("comparison-partial")).textContent).toMatch(/Partial comparison.*Page 3 timed out/);
    expect(screen.queryByText(/all courses/i)).toBeNull();
    expect(screen.getByTestId("count-comparison-missing").textContent).toBe("1");
    expect(screen.getByTestId("count-comparison-published").textContent).toBe("1");
    expect(screen.getByTestId("status-comparison-row-2").textContent).toBe("Not recorded");
    expect(screen.getByTestId("status-comparison-row-0").textContent).toBe("Approved active");
    expect(screen.queryByText(/Visible in search/i)).toBeNull();
    expect(screen.getByTestId("text-comparison-approved-note").textContent).toMatch(/does not prove/);
    expect(screen.getByTestId("link-comparison-source-0").getAttribute("href")).toBe("https://uni.edu/courses");
    expect(container.querySelector("a[href^='javascript']")).toBeNull();
    fireEvent.click(screen.getByTestId("button-comparison-row-1"));
    fireEvent.click(screen.getByTestId("button-comparison-review-1-rj1"));
    expect(onReviewJob).toHaveBeenCalledWith("rj1");
  });

  it("runs bounded recovery without manual selection", async () => {
    const courses = Array.from({ length: 23 }, (_, i) => ({ url: `https://uni.edu/c/${i}`, title: `Course ${i}` }));
    const thread: AssistantThread = { ...empty, status: "ready", messages: [
      { role: "assistant", content: "Candidates found.", createdAt: "2024-01-01T00:00:00Z", actions: [
        { id: "rc", kind: "recover_catalogue", label: "Recover", courseCount: 23, fields: [], state: "proposed", courses }] }] };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(json(thread)).mockResolvedValueOnce(json(thread));
    render(<ScrapePromptAssistant jobId="j1" />);
    fireEvent.click(await screen.findByTestId("button-run-action-rc"));
    expect(screen.queryByTestId("dialog-confirm-action")).toBeNull();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({ confirm: true, courseUrls: courses.slice(0, 20).map(c => c.url) });
  });

  it("confirms a fresh catalogue scan with an honest description and plain body", async () => {
    const thread: AssistantThread = { ...empty, status: "ready", messages: [
      { role: "assistant", content: "Partial.", createdAt: "2024-01-01T00:00:00Z", actions: [
        { id: "sc", kind: "scan_catalogue", label: "Run fresh catalogue discovery into review", courseCount: 0, fields: [], state: "proposed" }] }] };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(json(thread)).mockResolvedValueOnce(json(thread));
    render(<ScrapePromptAssistant jobId="j1" />);
    fireEvent.click(await screen.findByTestId("button-run-action-sc"));
    expect(screen.queryByTestId("dialog-confirm-action")).toBeNull();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({ confirm: true });
  });
});
