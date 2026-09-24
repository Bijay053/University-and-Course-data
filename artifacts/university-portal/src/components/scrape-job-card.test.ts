// @vitest-environment jsdom

import React from "react";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

const permissionState = vi.hoisted(() => ({ canTriggerRepair: true }));
vi.mock("@/components/can", () => ({
  useCan: () => ({
    can: (permission: string) => permission === "scraping.trigger" && permissionState.canTriggerRepair,
    canAny: () => false,
  }),
}));

import {
  activeRepairFromStartConflict,
  countSuspiciousSkipped,
  hasCompletedExtractionErrors,
  hasReviewableCourses,
  isCategoryPageWarningStale,
  onlyKnownExcludedUrls,
  repairJobIdForTerminalState,
  runtimeProgressFromStatus,
  searchProviderAccessFailure,
  shouldOfferIdenticalContinuation,
  shouldShowAutomaticUrlRepair,
  shouldShowScrapeDiagnostics,
  ScrapeJobCard,
} from "./scrape-job-card";

afterEach(() => {
  cleanup();
  sessionStorage.clear();
  permissionState.canTriggerRepair = true;
  vi.unstubAllGlobals();
});

it("does not treat known Law online variants as campus course losses", () => {
  const online = "https://www.law.ac.uk/study/postgraduate/law/llm/online/";
  expect(onlyKnownExcludedUrls([online])).toBe(true);
  expect(onlyKnownExcludedUrls([online, online.replace("/online/", "/")])).toBe(false);
  expect(onlyKnownExcludedUrls([online.replace("www.law.ac.uk", "example.edu")])).toBe(false);
  expect(onlyKnownExcludedUrls([])).toBe(false);
});

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

async function renderCompletedCard(errors: number, diagnosis?: unknown): Promise<HTMLElement> {
  sessionStorage.setItem("scrape_slot_1_jobId", "job-complete");
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.startsWith("/api/scrape/status/job-complete")) {
      return jsonResponse({
        status: "completed",
        universityId: 7,
        universityName: "Test University",
        totalFound: 100,
        imported: 100 - errors,
        skipped: 0,
        errors,
        current: 100,
        logs: [],
      });
    }
    if (url === "/api/scrape/staged/job-complete") return jsonResponse([]);
    if (url === "/api/scrape/jobs/job-complete/diagnose" && diagnosis) return jsonResponse(diagnosis);
    if (url.includes("/ai-repair-status")) return jsonResponse({ status: "not_started" });
    return jsonResponse({});
  }));

  render(React.createElement(ScrapeJobCard, {
    slotId: 1,
    slotIndex: 0,
    universities: [{ id: 7, name: "Test University" }],
    onReviewReady: () => undefined,
  }));

  return waitFor(() => {
    const card = screen.getByText("Errors").closest(".rounded-xl");
    expect(card).not.toBeNull();
    return card as HTMLElement;
  });
}

it("turns a selector recommendation into a course report action", async () => {
  await renderCompletedCard(1, {
    ok: true, job_id: "job-complete", university_id: 7,
    diagnosis: {
      summary: "Course locations are missing.",
      root_causes: [],
      recommended_actions: [{
        action: "Check the missing location rule",
        detail: "Adjust the location field selector in the Recipe Editor.",
        auto_fixable: false, fix_type: "recipe_fix",
      }],
      discovery_verdict: "ok", location_verdict: "missing",
    },
  });
  await userEvent.click(screen.getByRole("button", { name: /AI Scrape Diagnostics/ }));
  const reportAction = await screen.findByRole("button", { name: "Report official course URL" });
  expect(screen.queryByText(/location field selector/)).toBeNull();
  await userEvent.click(reportAction);
  expect(await screen.findByTestId("input-report-urls")).toBeTruthy();
});

async function renderFailedFilterCollapseCard(): Promise<void> {
  sessionStorage.setItem("scrape_slot_1_jobId", "job-filter-collapse");
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.startsWith("/api/scrape/status/job-filter-collapse")) {
      return jsonResponse({
        status: "failed",
        universityId: 75,
        universityName: "Canterbury Christ Church University (CCCU)",
        totalFound: 55,
        imported: 0,
        skipped: 0,
        errors: 0,
        current: 0,
        total: 55,
        logs: [{
          event: "warning",
          kind: "extract_allow_url_filter",
          message: "URL filter dropped 55 / 55 URLs (100%)",
          drop_pct: 100,
          dropped: 55,
          kept: 0,
          dropped_sample: ["/study-here/courses/accounting-and-finance"],
        }],
      });
    }
    if (url === "/api/scrape/staged/job-filter-collapse") return jsonResponse([]);
    if (url.includes("/auto-repair-candidates")) return jsonResponse({ ok: true, candidates: [] });
    if (url.includes("/ai-repair-status")) return jsonResponse({ status: "not_started" });
    return jsonResponse({});
  }));

  render(React.createElement(ScrapeJobCard, {
    slotId: 1,
    slotIndex: 0,
    universities: [{ id: 75, name: "Canterbury Christ Church University (CCCU)" }],
    onReviewReady: () => undefined,
  }));

  await waitFor(() => {
    expect(screen.getByRole("button", { name: "Run automatic repair" })).toBeTruthy();
  });
}

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

  it("does not offer recovery for SIT policy, stale-panel, and alias skips", () => {
    expect(countSuspiciousSkipped(259, {
      online_only: 101,
      not_listed_in_international_fee_schedule: 74,
      sit_course_panel_missing: 40,
      duplicate_url_in_job: 34,
      domestic_only: 10,
    })).toBe(0);
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

describe("hasCompletedExtractionErrors", () => {
  it("does not treat lifecycle completion with extraction errors as an all-clear", () => {
    expect(hasCompletedExtractionErrors("done", { errors: 82 })).toBe(true);
  });

  it("does not warn before completion or for a clean completed run", () => {
    expect(hasCompletedExtractionErrors("running", { errors: 82 })).toBe(false);
    expect(hasCompletedExtractionErrors("done", { errors: 0 })).toBe(false);
  });
});

describe("completed ScrapeJobCard quality state", () => {
  it("always offers self-service recovery after a scrape completes", async () => {
    await renderCompletedCard(0);

    expect(
      screen.getByRole("button", { name: "Report missing or incorrect courses" }),
    ).toBeTruthy();
  });

  it("renders a warning rather than a green success header when extraction errors remain", async () => {
    const card = await renderCompletedCard(82);

    expect(screen.getByText("Test University — Completed with errors")).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toContain(
      "Completed with extraction errors — quality is not all clear",
    );
    expect(screen.getByRole("alert").textContent).toContain("82 candidates failed extraction");
    const header = screen.getByText("Test University — Completed with errors").closest(".border-b");
    expect(header?.className).toContain("bg-amber-50");
    expect(header?.className).not.toContain("bg-green-50");
    expect(card.textContent).not.toContain("Test University — Done");
  });

  it("keeps the usual completed state when no extraction errors remain", async () => {
    await renderCompletedCard(0);

    expect(screen.getByText("Test University")).toBeTruthy();
    expect(screen.queryByText(/quality is not all clear/i)).toBeNull();
    const header = screen.getByText("Test University").closest(".border-b");
    expect(header?.className).toContain("bg-green-50");
    expect(header?.className).not.toContain("bg-amber-50");
  });
});

describe("failed URL-filter repair action", () => {
  it("renders after reload when only the active job identity is restored", async () => {
    await renderFailedFilterCollapseCard();
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

describe("repairJobIdForTerminalState", () => {
  it("uses the persisted active job after a failed card reload", () => {
    expect(repairJobIdForTerminalState(null, "job_failed", "error")).toBe("job_failed");
  });

  it("prefers the completed job identity and does not expose running jobs", () => {
    expect(repairJobIdForTerminalState("job_complete", "job_old", "done")).toBe("job_complete");
    expect(repairJobIdForTerminalState(null, "job_running", "running")).toBeNull();
  });
});

describe("shouldShowScrapeDiagnostics", () => {
  it("keeps diagnostics available when a filter-collapse job ends in error", () => {
    expect(shouldShowScrapeDiagnostics("job_filter_collapse", "error")).toBe(true);
  });

  it("does not expose terminal diagnostics while a job is still running", () => {
    expect(shouldShowScrapeDiagnostics("job_running", "extract")).toBe(false);
    expect(shouldShowScrapeDiagnostics(null, "error")).toBe(false);
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

describe("SearchStax provider discovery recovery", () => {
  it("recognizes only a structured SearchStax access denial or narrow historical correlation", () => {
    expect(searchProviderAccessFailure({
      provider: "searchstax",
      http_status: 401,
      kind: "provider_access_denied",
      message: "Course search unavailable",
    })).toMatchObject({ provider: "searchstax", http_status: 401 });
    expect(searchProviderAccessFailure(null, [{
      kind: "discovery",
      message: "SearchStax HTTP 401 unauthorized",
    }])).toMatchObject({ kind: "provider_access_denied" });
    expect(searchProviderAccessFailure({
      provider: "searchstax",
      http_status: 403,
      kind: "provider_access_denied",
      message: "Course search unavailable",
    })).toMatchObject({ provider: "searchstax", http_status: 403 });
    expect(searchProviderAccessFailure(null, [{
      kind: "discovery",
      message: "SearchStax HTTP 403 Forbidden",
    }])).toMatchObject({ kind: "provider_access_denied", http_status: 403 });
    expect(searchProviderAccessFailure(null, [{
      kind: "discovery",
      message: "Official catalogue returned HTTP 401",
    }])).toBeNull();
    expect(searchProviderAccessFailure(null, [{
      kind: "discovery",
      message: "SearchStax found 42 courses",
    }])).toBeNull();
  });

  it("offers repair on a rehydrated failed zero-course job and posts the original job id", async () => {
    sessionStorage.setItem("scrape_slot_9_jobId", "leeds-trinity-original");
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith("/api/scrape/status/leeds-trinity-original")) {
        return jsonResponse({
          status: "failed",
          universityId: 401,
          universityName: "Leeds Trinity University",
          url: "https://www.leedstrinity.ac.uk/courses/",
          totalFound: 0,
          imported: 0,
          skipped: 0,
          errors: 0,
          logs: [{
            event: "error",
            message: "SearchStax HTTP 401 unauthorized at https://search.example.invalid/?api_key=secret",
          }],
          provider_failure: {
            provider: "searchstax",
            http_status: 401,
            kind: "provider_access_denied",
            message: "Course search unavailable",
          },
        });
      }
      if (url === "/api/scrape/staged/leeds-trinity-original") return jsonResponse([]);
      if (url === "/api/scrape/jobs/leeds-trinity-original/ai-repair-status") {
        return jsonResponse({ status: "not_started" });
      }
      if (url === "/api/scrape/jobs/leeds-trinity-original/ai-repair" && init?.method === "POST") {
        return jsonResponse({
          session_id: "repair-leeds",
          job_id: "leeds-trinity-original",
          status: "queued",
          autonomous: {
            enabled: true,
            phase: "queued",
            discovery_repair: { status: "queued" },
          },
        });
      }
      return jsonResponse({ reports: [], source_exclusions: {} });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(React.createElement(ScrapeJobCard, {
      slotId: 9,
      slotIndex: 0,
      universities: [{ id: 401, name: "Leeds Trinity University" }],
      onReviewReady: () => undefined,
    }));

    const repair = await screen.findByRole("button", { name: "Repair discovery and retry" });
    expect(screen.queryByRole("button", { name: "Continue" })).toBeNull();
    expect(screen.getByText(/course search is unavailable/i)).toBeTruthy();
    expect(screen.queryByText(/api_key=secret/i)).toBeNull();
    await userEvent.click(repair);
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/scrape/jobs/leeds-trinity-original/ai-repair",
        expect.objectContaining({ method: "POST", credentials: "include" }),
      );
    });
    expect(await screen.findByText("Trying official alternatives…")).toBeTruthy();
  });

  it("does not expose the repair trigger without scraping.trigger permission", async () => {
    permissionState.canTriggerRepair = false;
    sessionStorage.setItem("scrape_slot_10_jobId", "provider-denied-job");
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/scrape/status/provider-denied-job")) {
        return jsonResponse({
          status: "failed",
          universityName: "Leeds Trinity University",
          totalFound: 0,
          provider_failure: {
            provider: "searchstax",
            http_status: 401,
            kind: "provider_access_denied",
          },
        });
      }
      if (url.includes("/ai-repair-status")) return jsonResponse({ status: "not_started" });
      if (url.includes("/staged/")) return jsonResponse([]);
      return jsonResponse({ reports: [], source_exclusions: {} });
    }));

    render(React.createElement(ScrapeJobCard, {
      slotId: 10,
      slotIndex: 0,
      universities: [],
      onReviewReady: () => undefined,
    }));

    expect(await screen.findByTestId("text-repair-permission-required")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /repair discovery and retry/i })).toBeNull();
  });

  it("opens the existing report with the official catalogue URL when automatic repair is blocked", async () => {
    sessionStorage.setItem("scrape_slot_11_jobId", "provider-blocked-job");
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/scrape/status/provider-blocked-job")) {
        return jsonResponse({
          status: "failed",
          universityName: "Leeds Trinity University",
          url: "https://www.leedstrinity.ac.uk/courses/",
          totalFound: 0,
          provider_failure: {
            provider: "searchstax",
            http_status: 401,
            kind: "provider_access_denied",
          },
        });
      }
      if (url.endsWith("/ai-repair-status")) {
        return jsonResponse({
          session_id: "blocked-repair",
          job_id: "provider-blocked-job",
          status: "completed",
          current_attempt: 1,
          attempts: [],
          final_verdict: null,
          uni_name: "Leeds Trinity University",
          started_at: null,
          completed_at: null,
          error: null,
          autonomous: {
            enabled: true,
            phase: "blocked",
            discovery_repair: {
              status: "blocked",
              strategy: "official_sitemap",
              candidate_count: 144,
              verified_course_count: 2,
              next_action: "report_official_url",
            },
          },
        });
      }
      if (url.includes("/staged/")) return jsonResponse([]);
      return jsonResponse({ reports: [], source_exclusions: {} });
    }));

    render(React.createElement(ScrapeJobCard, {
      slotId: 11,
      slotIndex: 0,
      universities: [],
      onReviewReady: () => undefined,
    }));

    const report = await screen.findByRole("button", { name: "Report official catalogue or course URL" });
    await userEvent.click(report);
    const urlInput = await screen.findByTestId("input-report-urls") as HTMLTextAreaElement;
    expect(urlInput.value).toBe("https://www.leedstrinity.ac.uk/courses/");
  });
});