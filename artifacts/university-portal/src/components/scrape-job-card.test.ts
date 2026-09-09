import { describe, expect, it } from "vitest";

import {
  activeRepairFromStartConflict,
  countSuspiciousSkipped,
  hasReviewableCourses,
  isCategoryPageWarningStale,
  runtimeProgressFromStatus,
  shouldOfferIdenticalContinuation,
  shouldShowAutomaticUrlRepair,
} from "./scrape-job-card";

describe("runtimeProgressFromStatus", () => {
  it("uses the persisted processed/total values from every status poll", () => {
    expect(runtimeProgressFromStatus({
      current: 17,
      total: 40,
      totalFound: 40,
    })).toEqual({ current: 17, total: 40 });
  });

  it("supports resumed jobs and clamps stale counters to the catalogue total", () => {
    expect(runtimeProgressFromStatus({
      current: 257,
      totalFound: 409,
    })).toEqual({ current: 257, total: 409 });
    expect(runtimeProgressFromStatus({
      current: 411,
      totalFound: 409,
    })).toEqual({ current: 409, total: 409 });
  });
});

describe("isCategoryPageWarningStale", () => {
  it("drops a warning from an obsolete 14-link candidate set when the job has 182 URLs", () => {
    expect(isCategoryPageWarningStale(14, 182)).toBe(true);
  });

  it("keeps a warning that describes the current job total", () => {
    expect(isCategoryPageWarningStale(14, 14)).toBe(false);
  });
});

describe("countSuspiciousSkipped", () => {
  it("excludes intentional audience-policy rejections", () => {
    expect(countSuspiciousSkipped(208, {
      online_only: 179,
      category_landing_page_missing_degree_qualifier: 27,
      other: 2,
    })).toBe(29);
  });

  it("classifies the legacy prefixed keys emitted by production jobs", () => {
    expect(countSuspiciousSkipped(208, {
      "rejected:_online_only": 181,
      "rejected:_category_landing_page_missing_": 27,
    })).toBe(27);
  });

  it("keeps the conservative legacy total when reason details are unavailable", () => {
    expect(countSuspiciousSkipped(208)).toBe(208);
  });

  it("never returns a negative count when reason totals drift", () => {
    expect(countSuspiciousSkipped(5, { online_only: 7 })).toBe(0);
  });
});

describe("hasReviewableCourses", () => {
  it("keeps review visible when a zero-stage continuation inherits parent rows", () => {
    expect(hasReviewableCourses({ imported: 0 }, 101)).toBe(true);
  });

  it("uses the cumulative status count while the pending count loads", () => {
    expect(hasReviewableCourses({ imported: 101 }, null)).toBe(true);
  });

  it("hides review only after the chain has no pending rows", () => {
    expect(hasReviewableCourses({ imported: 101 }, 0)).toBe(false);
  });
});

describe("shouldOfferIdenticalContinuation", () => {
  const base = {
    completedJobId: "job_child",
    errors: 79,
    unresolvedCount: 79,
    isContinuation: false,
    browserRescueAttempted: false,
    browserRescueWasBlocked: false,
  };

  it("offers one bounded continuation for a completed original run", () => {
    expect(shouldOfferIdenticalContinuation(base)).toBe(true);
  });

  it("does not offer the same continuation again for a child run after reload", () => {
    expect(shouldOfferIdenticalContinuation({
      ...base,
      isContinuation: true,
    })).toBe(false);
  });

  it("does not use raw errors as retry eligibility when no URLs remain retryable", () => {
    expect(shouldOfferIdenticalContinuation({
      ...base,
      unresolvedCount: 0,
    })).toBe(false);
  });
});

describe("shouldShowAutomaticUrlRepair", () => {
  it("shows one-click repair for a completed high-drop job without requiring preloaded candidates", () => {
    expect(shouldShowAutomaticUrlRepair("job_123", "high_drop_rate")).toBe(true);
  });

  it("does not offer URL-filter repair without a completed job or for category-page diagnosis", () => {
    expect(shouldShowAutomaticUrlRepair(null, "high_drop_rate")).toBe(false);
    expect(shouldShowAutomaticUrlRepair("job_123", "category_pages")).toBe(false);
  });
});

describe("activeRepairFromStartConflict", () => {
  it("returns the existing repair owner so polling can attach to it", () => {
    expect(activeRepairFromStartConflict({
      detail: "already running",
      active_repair: {
        job_id: "job_owner",
        session_id: "repair_123",
        status: "running",
      },
    })).toEqual({
      jobId: "job_owner",
      sessionId: "repair_123",
      status: "running",
    });
  });

  it("rejects conflicts that do not identify an attachable session", () => {
    expect(activeRepairFromStartConflict({ detail: "already running" })).toBeNull();
    expect(activeRepairFromStartConflict({
      active_repair: { job_id: "job_owner", session_id: "" },
    })).toBeNull();
  });
});