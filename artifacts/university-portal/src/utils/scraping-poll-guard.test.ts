import { describe, it, expect, vi, beforeEach } from "vitest";
import { shouldLoadForBackgroundJob } from "./scraping-poll-guard";

/**
 * Regression test for the pollJobStatus background-job guard.
 *
 * Bug: When Job B completed in the background while the user had the review
 * panel open for Job A, pollJobStatus called loadStagedCourses(Job B),
 * silently replacing the visible review table with Job B's data.
 *
 * Fix: shouldLoadForBackgroundJob() returns false when the review panel is
 * already showing a different job; the caller logs a debug message and skips
 * the loadStagedCourses call.
 */
describe("shouldLoadForBackgroundJob — pollJobStatus guard", () => {
  // ── Helper: simulate the refs the React component tracks ─────────────────
  function makePanel(showReview: boolean, reviewJobId: string | null) {
    return { showReview, reviewJobId };
  }

  // ── Core regression scenario ──────────────────────────────────────────────

  it("returns false when review panel is open for a DIFFERENT job (the bug scenario)", () => {
    // Job A is being reviewed by the user.
    const panel = makePanel(true, "job-a");

    // Job B completes in the background → must NOT replace the review table.
    expect(shouldLoadForBackgroundJob("job-b", panel)).toBe(false);
  });

  it("returns true when review panel is open for the SAME job", () => {
    // The user is reviewing Job A and Job A finishes a re-poll.
    const panel = makePanel(true, "job-a");

    expect(shouldLoadForBackgroundJob("job-a", panel)).toBe(true);
  });

  it("returns true when review panel is closed (no active review)", () => {
    // No review is open → auto-open is fine.
    const panel = makePanel(false, null);

    expect(shouldLoadForBackgroundJob("job-b", panel)).toBe(true);
  });

  it("returns true when review panel is closed even if reviewJobId has a stale value", () => {
    // showReview=false means the panel is hidden; stale reviewJobId is irrelevant.
    const panel = makePanel(false, "job-a");

    expect(shouldLoadForBackgroundJob("job-b", panel)).toBe(true);
  });

  it("returns true when reviewJobId is null regardless of showReview", () => {
    // Panel might be shown but no job loaded yet (edge case on first open).
    const panel = makePanel(true, null);

    expect(shouldLoadForBackgroundJob("job-b", panel)).toBe(true);
  });

  // ── Manual click (force=true) always wins ─────────────────────────────────

  it("returns true with force=true even when a different job is being reviewed", () => {
    // User explicitly clicks 'Review' on Job B while Job A is displayed.
    const panel = makePanel(true, "job-a");

    expect(shouldLoadForBackgroundJob("job-b", panel, true)).toBe(true);
  });

  it("returns true with force=true when panel is closed", () => {
    const panel = makePanel(false, null);

    expect(shouldLoadForBackgroundJob("job-b", panel, true)).toBe(true);
  });

  // ── Console log emitted when guard blocks the load ────────────────────────

  it("caller can detect the ignored case via the return value and log appropriately", () => {
    const consoleSpy = vi.spyOn(console, "debug").mockImplementation(() => {});

    const panel = makePanel(true, "job-a");
    const shouldLoad = shouldLoadForBackgroundJob("job-b", panel);

    // The util itself does not log — the React component does.  But we
    // verify the contract: false return → caller should log and skip.
    if (!shouldLoad) {
      console.debug(
        "[SCRAPE_UI] ignored background staged-course load because review panel is open for another job",
        { backgroundJobId: "job-b", openJobId: "job-a" },
      );
    }

    expect(consoleSpy).toHaveBeenCalledOnce();
    expect(consoleSpy).toHaveBeenCalledWith(
      "[SCRAPE_UI] ignored background staged-course load because review panel is open for another job",
      { backgroundJobId: "job-b", openJobId: "job-a" },
    );

    consoleSpy.mockRestore();
  });
});
