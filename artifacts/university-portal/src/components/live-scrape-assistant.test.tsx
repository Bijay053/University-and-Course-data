// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

const permissions = vi.hoisted(() => ({ allowed: true }));
vi.mock("@/context/auth", () => ({
  useAuth: () => ({ can: () => permissions.allowed, canAny: () => permissions.allowed }),
}));
vi.mock("@/components/scrape-prompt-assistant", () => ({
  ScrapePromptAssistant: (p: { jobId: string; onChanged?: () => void; onReviewJob?: (id: string) => void }) => (
    <div data-testid={`mock-assistant-${p.jobId}`}>
      <button onClick={() => p.onChanged?.()}>changed</button>
      <button onClick={() => p.onReviewJob?.("child-1")}>review</button>
    </div>
  ),
}));

import { LiveDiagnosticsPanel, LiveScrapeAssistant, LIVE_DIAGNOSTICS_POLL_MS, liveDiagnosticsUrl } from "./live-scrape-assistant";

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); permissions.allowed = true; });
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const diag = (jobId: string, extra: Record<string, unknown> = {}) => ({
  jobId, status: "running", processed: 12, total: 40, elapsedSeconds: 130, coursesPerMinute: 5.5, etaSeconds: 300,
  timeouts: 2, retries: 1, duplicateRequests: 0, aiEnrichments: 3, heartbeatAgeSeconds: 4,
  issues: [], capabilities: { runtimeChanges: false }, ...extra,
});

const runtime = { ceiling: 4, applied: { concurrency: 4, retries: 2, backoffSeconds: 0 } };

it("previews exact job-only values without claiming worker completion", async () => {
  const f = vi.spyOn(globalThis, "fetch").mockImplementation(async (url, options) => {
    if (options?.method === "POST") return json({ status: "queued" }, 202);
    return json(diag("scoped", { capabilities: { runtimeChanges: true }, runtime }));
  });
  render(<LiveScrapeAssistant jobId="scoped" />);
  fireEvent.change(await screen.findByLabelText("concurrency"), { target: { value: "2" } });
  fireEvent.change(screen.getByLabelText("retries"), { target: { value: "1" } });
  fireEvent.change(screen.getByLabelText("Minimum backoff seconds"), { target: { value: "30" } });
  fireEvent.click(screen.getByText("Preview job-only change"));
  await screen.findByTestId("mock-assistant-scoped");
  const post = f.mock.calls.find(([, options]) => options?.method === "POST")!;
  expect(post[0]).toBe("/api/scrape/jobs/scoped/assistant");
  expect(JSON.parse(String(post[1]?.body))).toEqual({
    prompt: "Set concurrency to 2, retries to 1, backoffSeconds to 30",
  });
  expect(screen.queryByText(/Worker acknowledged/)).toBeNull();
});

it("does not offer controls to view-only users or send runtime requests", async () => {
  permissions.allowed = false;
  const f = vi.spyOn(globalThis, "fetch").mockResolvedValue(json(diag("view", {
    capabilities: { runtimeChanges: true }, runtime,
  })));
  render(<LiveScrapeAssistant jobId="view" />);
  await screen.findByTestId("text-live-processed");
  expect(screen.queryByText("Preview job-only change")).toBeNull();
  expect(f.mock.calls.every(([, options]) => !options?.method)).toBe(true);
});

it("distinguishes a pending request from effective settings and disables duplicates", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(json(diag("pending", {
    capabilities: { runtimeChanges: true },
    runtime: { ...runtime, request: { state: "pending", new: { ...runtime.applied, concurrency: 1 } } },
  })));
  render(<LiveScrapeAssistant jobId="pending" />);
  const button = await screen.findByText("Preview job-only change");
  expect((button as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByRole("status").textContent).toContain("Runtime request: pending");
  expect(screen.queryByText(/Worker acknowledged/)).toBeNull();
});

it("shows nulls as unavailable, never as zero or healthy", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(json(diag("j1", {
    processed: null, coursesPerMinute: null, etaSeconds: null, heartbeatAgeSeconds: null, issues: null, capabilities: null,
  })));
  render(<LiveDiagnosticsPanel jobId="j1" />);
  await screen.findByText("Diagnosis unavailable");
  expect(screen.getByTestId("text-live-processed").textContent).toBe("Unavailable");
  expect(screen.getByTestId("text-live-rate").textContent).toBe("Unavailable");
  fireEvent.click(screen.getByTestId("button-live-details"));
  expect(screen.getByTestId("text-live-issues-unavailable")).toBeTruthy();
  expect(screen.getByTestId("text-live-capabilities").textContent).toMatch(/unavailable/);
});

it("polls every 5 seconds for the active job and surfaces errors with retry", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const f = vi.spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(json(diag("j1", { issues: [{ code: "slow", title: "Slow pages", detail: "Timeouts rising" }] })))
    .mockResolvedValueOnce(json({ detail: "boom" }, 500))
    .mockResolvedValue(json(diag("j1")));
  render(<LiveDiagnosticsPanel jobId="j1" />);
  await screen.findByText("Slow pages");
  expect(f).toHaveBeenCalledTimes(1);
  expect(f.mock.calls[0][0]).toBe(liveDiagnosticsUrl("j1"));
  await act(async () => { await vi.advanceTimersByTimeAsync(LIVE_DIAGNOSTICS_POLL_MS); });
  await screen.findByTestId("live-diagnostics-error");
  expect(f).toHaveBeenCalledTimes(2);
  fireEvent.click(screen.getByTestId("button-live-retry"));
  await waitFor(() => expect(screen.queryByTestId("live-diagnostics-error")).toBeNull());
});

it("does not poll when inactive", async () => {
  const f = vi.spyOn(globalThis, "fetch");
  render(<LiveDiagnosticsPanel jobId="j1" active={false} />);
  await new Promise(r => setTimeout(r, 20));
  expect(f).not.toHaveBeenCalled();
});

it("ignores stale responses after the job changes and keeps slots isolated", async () => {
  let resolveOld: (r: Response) => void = () => {};
  vi.spyOn(globalThis, "fetch").mockImplementation((url) => {
    if (String(url).includes("old")) return new Promise<Response>(r => { resolveOld = r; });
    if (String(url).includes("other")) return Promise.resolve(json(diag("other", { processed: 99, total: 100 })));
    return Promise.resolve(json(diag("new", { processed: 7, total: 8 })));
  });
  const { rerender } = render(<div><LiveDiagnosticsPanel key="old" jobId="old" /><LiveDiagnosticsPanel jobId="other" /></div>);
  rerender(<div><LiveDiagnosticsPanel key="new" jobId="new" /><LiveDiagnosticsPanel jobId="other" /></div>);
  await screen.findByText("7/8");
  await act(async () => { resolveOld(json(diag("old", { processed: 1, total: 2 }))); });
  expect(screen.queryByText("1/2")).toBeNull();
  expect(screen.getByText("99/100")).toBeTruthy();
});

it("reuses the existing assistant with the same jobId and callbacks", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(json(diag("j9")));
  const onChanged = vi.fn(), onReviewJob = vi.fn();
  render(<LiveScrapeAssistant jobId="j9" onChanged={onChanged} onReviewJob={onReviewJob} />);
  fireEvent.click(screen.getByTestId("button-toggle-live-assistant"));
  fireEvent.click(screen.getByText("changed"));
  fireEvent.click(screen.getByText("review"));
  expect(screen.getByTestId("mock-assistant-j9")).toBeTruthy();
  expect(onChanged).toHaveBeenCalledTimes(1);
  expect(onReviewJob).toHaveBeenCalledWith("child-1");
});
