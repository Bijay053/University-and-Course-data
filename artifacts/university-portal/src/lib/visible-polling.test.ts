// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import { preferFreshRepair, startVisiblePolling } from "./visible-polling";

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

it("discovers a 139-course repair after an older completed one-course result", async () => {
  vi.useFakeTimers();
  vi.spyOn(document, "hidden", "get").mockReturnValue(false);
  const old = { jobId: "old", sourceJobId: "review", status: "completed", processed: 1,
    total: 1, createdAt: "2026-11-12T01:00:00Z" };
  const next = { ...old, jobId: "new", status: "running", processed: 50, total: 139,
    createdAt: "2026-11-12T02:00:00Z" };
  let shown = old;
  const lookup = vi.fn().mockResolvedValueOnce(old).mockResolvedValue(next);
  const stop = startVisiblePolling(async () => {
    shown = preferFreshRepair(shown, await lookup());
  }, 15000, vi.fn(), true);
  await vi.advanceTimersByTimeAsync(0);
  expect(shown.total).toBe(1);
  await vi.advanceTimersByTimeAsync(15000);
  expect(shown).toEqual(next);
  stop();
});

it("refreshes on focus without overlapping requests and aborts on cleanup", async () => {
  vi.useFakeTimers();
  vi.spyOn(document, "hidden", "get").mockReturnValue(false);
  let finish!: () => void;
  let signal!: AbortSignal;
  const read = vi.fn((value: AbortSignal) => {
    signal = value;
    return new Promise<void>(resolve => { finish = resolve; });
  });
  const stop = startVisiblePolling(read, 15000, vi.fn());
  window.dispatchEvent(new Event("focus"));
  window.dispatchEvent(new Event("focus"));
  await vi.advanceTimersByTimeAsync(15000);
  expect(read).toHaveBeenCalledTimes(1);
  stop();
  expect(signal.aborted).toBe(true);
  finish();
  await vi.advanceTimersByTimeAsync(30000);
  window.dispatchEvent(new Event("focus"));
  expect(read).toHaveBeenCalledTimes(1);
});

it("shows a transient read failure and keeps attempting read-only refreshes", async () => {
  vi.useFakeTimers();
  vi.spyOn(document, "hidden", "get").mockReturnValue(false);
  const error = vi.fn();
  const read = vi.fn().mockRejectedValueOnce(new Error("unavailable")).mockResolvedValue(undefined);
  const stop = startVisiblePolling(read, 15000, error, true);
  await vi.advanceTimersByTimeAsync(0);
  expect(error).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(15000);
  expect(read).toHaveBeenCalledTimes(2);
  stop();
});

it("pauses hidden tabs and refreshes immediately when visible again", async () => {
  vi.useFakeTimers();
  const visibility = vi.spyOn(document, "hidden", "get").mockReturnValue(true);
  const read = vi.fn().mockResolvedValue(undefined);
  const stop = startVisiblePolling(read, 15000, vi.fn(), true);
  await vi.advanceTimersByTimeAsync(30000);
  expect(read).not.toHaveBeenCalled();
  visibility.mockReturnValue(false);
  document.dispatchEvent(new Event("visibilitychange"));
  await vi.advanceTimersByTimeAsync(0);
  expect(read).toHaveBeenCalledTimes(1);
  visibility.mockReturnValue(true);
  document.dispatchEvent(new Event("visibilitychange"));
  await vi.advanceTimersByTimeAsync(30000);
  expect(read).toHaveBeenCalledTimes(1);
  stop();
});

it("does not replace newer progress with late older or regressed responses", () => {
  const current = { jobId: "new", sourceJobId: "review", status: "running", processed: 50,
    createdAt: "2026-11-12T02:00:00Z" };
  expect(preferFreshRepair(current, { ...current, jobId: "old", status: "completed",
    processed: 1, createdAt: "2026-11-12T01:00:00Z" })).toBe(current);
  expect(preferFreshRepair(current, { ...current, jobId: "old", status: "completed",
    processed: 1, createdAt: undefined })).toBe(current);
  expect(preferFreshRepair(current, { ...current, processed: 45 })).toBe(current);
  const completed = { ...current, status: "completed", processed: 139 };
  expect(preferFreshRepair(completed, { ...completed, status: "running" })).toBe(completed);
  expect(preferFreshRepair(current, { ...current, sourceJobId: "other-review" }).sourceJobId)
    .toBe("other-review");
});
