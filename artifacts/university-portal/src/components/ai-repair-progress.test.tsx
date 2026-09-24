// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AiRepairProgress, type AutonomousRepair } from "./ai-repair-progress";

const base: AutonomousRepair = {
  enabled: true,
  phase: "live_probe",
  limits: {
    max_attempts: 5,
    max_live_pages: 12,
    max_live_seconds: 180,
    max_verification_runs: 2,
  },
};

afterEach(cleanup);

describe("AiRepairProgress", () => {
  it("shows actual discovery-repair counts without claiming candidates are eligible courses", () => {
    render(
      <AiRepairProgress
        autonomous={{
          ...base,
          discovery_repair: {
            status: "running",
            strategy: "official_sitemap",
            candidate_count: 144,
            verified_course_count: 2,
            message: "Checking MSc and MA samples",
            next_action: "retry_discovery",
          },
        }}
        currentAttempt={1}
      />,
    );

    const status = screen.getByTestId("status-discovery-repair");
    expect(status.textContent).toContain("144 candidate URLs found");
    expect(status.textContent).toContain("2 verified course pages");
    expect(status.textContent).toContain("official sitemap");
    expect(status.textContent).toContain("Next: retry discovery");
    expect(status.textContent).not.toContain("144 eligible");
  });

  it("does not show completed stages when delivery was blocked before attempt one", () => {
    const { container } = render(
      <AiRepairProgress autonomous={{ ...base, phase: "blocked" }} currentAttempt={0} />,
    );
    expect(screen.getByText("Attempt 0/5")).toBeTruthy();
    expect(container.querySelectorAll("li.bg-emerald-100")).toHaveLength(0);
    expect(container.querySelector('[aria-current="step"]')).toBeNull();
  });

  it("shows the bounded live-probe stage and official-source evidence", () => {
    render(
      <AiRepairProgress
        autonomous={base}
        currentAttempt={1}
        liveProbe={{
          status: "checking",
          pages_checked: 4,
          course_pages: 2,
          rejected_pages: 1,
          failures: 1,
          samples: [{
            url: "https://official.example.edu/course/engineering",
            classification: "course page",
            reason: "degree details found",
          }],
        }}
      />,
    );

    expect(screen.getByText("Automatic repair running")).toBeTruthy();
    expect(screen.getByText("Probe official sources").getAttribute("aria-current")).toBe("step");
    expect(screen.getByText(/4 pages checked/)).toBeTruthy();
    expect(screen.getByText("Official source").closest("a")?.getAttribute("href"))
      .toBe("https://official.example.edu/course/engineering");
    expect(screen.getByText(/Attempt 1\/5/)).toBeTruthy();
    expect(screen.getByText(/12 live pages/)).toBeTruthy();
  });

  it("does not call a saved config verified before its verification scrape", () => {
    render(
      <AiRepairProgress
        autonomous={{ ...base, phase: "verification_queued" }}
        currentAttempt={2}
      />,
    );

    expect(screen.queryByText("Repair verified")).toBeNull();
    expect(screen.getByText(/Config saved. Waiting for the automatic verification scrape/)).toBeTruthy();
    expect(screen.getByText("Save config").getAttribute("aria-current")).toBe("step");
  });

  it("retains hydrated metadata and opens the child through the supplied navigation", () => {
    const openVerificationJob = vi.fn();
    render(
      <AiRepairProgress
        autonomous={{
          ...base,
          phase: "needs_review",
          verification_job_id: "job_verify_42",
          verification_status: "completed_with_warnings",
          reason: "Course count improved, but fee coverage regressed.",
        }}
        currentAttempt={3}
        liveProbe={{
          status: "passed",
          pages_checked: 8,
          course_pages: 6,
          rejected_pages: 2,
          failures: 0,
        }}
        onOpenVerificationJob={openVerificationJob}
      />,
    );

    expect(screen.getByText("Repair needs review")).toBeTruthy();
    expect(screen.queryByText("Repair verified")).toBeNull();
    expect(screen.getByText(/Course count improved/)).toBeTruthy();
    expect(screen.getByText(/Verification: completed with warnings/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Open verification job →" }));
    expect(openVerificationJob).toHaveBeenCalledWith("job_verify_42");
  });

  it("turns a zero-course dead end into a user-facing official URL action", () => {
    const onReportOfficialCourse = vi.fn();
    const { container } = render(
      <AiRepairProgress
        autonomous={{ ...base, phase: "blocked", reason: "Accepted live validation is required; no scrape launched." }}
        liveProbe={{ status: "needs_review", pages_checked: 6, course_pages: 0 }}
        currentAttempt={5}
        onReportOfficialCourse={onReportOfficialCourse}
      />,
    );

    expect(screen.getByText("Official course page needed")).toBeTruthy();
    expect(screen.queryByText("Repair verified")).toBeNull();
    expect(container.querySelector("section")?.className).toContain("border-red-200");
    expect(container.querySelectorAll("li.bg-emerald-100")).toHaveLength(0);
    expect(screen.getByText(/Use Report official course URL/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Report official course URL" }));
    expect(onReportOfficialCourse).toHaveBeenCalledOnce();
    expect(screen.queryByText(/Manual investigation/)).toBeNull();
    expect(screen.getByText(/publishing is always manual/)).toBeTruthy();
  });

  it("shows the two-run cumulative verification scope and observed Gemini cost", () => {
    render(
      <AiRepairProgress
        autonomous={{
          ...base,
          phase: "verifying",
          verification_limits: {
            max_courses: 50,
            time_budget_seconds: 600,
            cost_cap_usd: 2,
            max_runs: 2,
            total_time_budget_seconds: 1200,
            total_cost_cap_usd: 2,
            scope: "bounded fresh catalogue verification; not full catalogue coverage",
          },
          comparison: {
            cumulative_counters: { gemini_cost_usd: 0.73 },
          },
        }}
        currentAttempt={2}
      />,
    );

    expect(screen.getByText(/one 50-course sample · up to 2 runs · 1,200 seconds cumulative/)).toBeTruthy();
    expect(screen.getByText(/extraction ceiling: \$2.00 cumulative/)).toBeTruthy();
    expect(screen.getByText(/observed: \$0.73/)).toBeTruthy();
    expect(screen.queryByText(/total budget/i)).toBeNull();
    expect(screen.getByText(/not full catalogue coverage/)).toBeTruthy();
  });

  it("shows an automatic second run with completed and remaining counts as active", () => {
    render(
      <AiRepairProgress
        autonomous={{
          ...base,
          phase: "verification_queued",
          verification_status: "queued",
          reason: "Verification timed out after 31 of 50 selected courses; automatically continuing the exact remaining sample.",
          verification_limits: {
            max_courses: 50,
            time_budget_seconds: 600,
            max_runs: 2,
            total_time_budget_seconds: 1200,
            total_cost_cap_usd: 2,
          },
          continuation: {
            status: "queued",
            round: 2,
            max_runs: 2,
            completed_courses: 31,
            remaining_courses: 19,
            total_time_budget_seconds: 1200,
            total_cost_cap_usd: 2,
          },
          comparison: {
            stop_reason: "time_budget_exhausted",
            verification_limits: {
              budget_exhausted: "time_budget_exhausted",
              time_budget_seconds: 600,
            },
          },
        }}
        currentAttempt={2}
      />,
    );

    expect(screen.getByText("Automatic repair running")).toBeTruthy();
    expect(screen.getByText(/Automatic verification run 2 of 2:/).parentElement?.textContent)
      .toContain("31 completed · 19 remaining");
    expect(screen.queryByText("Stopped:")).toBeNull();
    expect(screen.getByText(/automatically continuing the exact remaining sample/)).toBeTruthy();
  });

  it("compares baseline and verification quality across the supported fields", () => {
    render(
      <AiRepairProgress
        autonomous={{
          ...base,
          phase: "needs_review",
          comparison: {
            baseline: { fee_pct: 40, ielts_pct: 55, duration_pct: 70, location_pct: 60, course_name_pct: 80 },
            verification: { fee_pct: 75, ielts_pct: 85, duration_pct: 90, location_pct: 95, course_name_pct: 100 },
          },
        }}
        currentAttempt={3}
      />,
    );

    expect(screen.getByText("Baseline")).toBeTruthy();
    expect(screen.getByText("Verification")).toBeTruthy();
    expect(screen.getByText("Course names")).toBeTruthy();
    expect(screen.getByText("100%")).toBeTruthy();
  });

  it("reports unresolved fields, regressions, capping, and bounded-sample verification", () => {
    render(
      <AiRepairProgress
        autonomous={{
          ...base,
          phase: "verified",
          verification_limits: { max_courses: 50, time_budget_seconds: 600 },
          comparison: {
            unresolved_fields: ["ielts", "duration"],
            regressions: ["fee coverage"],
            capped: true,
          },
        }}
        currentAttempt={2}
      />,
    );

    expect(screen.getByText("Bounded sample verified")).toBeTruthy();
    expect(screen.getByText(/full catalogue coverage is not certified/)).toBeTruthy();
    expect(screen.getByText("Unresolved fields:").parentElement?.textContent).toContain("ielts, duration");
    expect(screen.getByText("Regressions:").parentElement?.textContent).toContain("fee coverage");
    expect(screen.getByText("Capped:").parentElement?.textContent).toContain("50-course limit");
  });

  it("reports a time-budget stop with actual progress instead of a course cap", () => {
    render(
      <AiRepairProgress
        autonomous={{
          ...base,
          phase: "needs_review",
          comparison: {
            unresolved_fields: ["ielts_pct", "duration_pct", "mode_pct"],
            capped: true,
            verification_limits: {
              max_courses: 50,
              time_budget_seconds: 600,
              selected_courses: 50,
              staged_courses: 30,
              budget_exhausted: "time_budget_exhausted",
            },
            counters: { total_found: 50, current: 31, imported: 30 },
          },
        }}
        currentAttempt={1}
      />,
    );

    expect(screen.getByText("Unresolved fields:").parentElement?.textContent).toContain(
      "ielts, duration, mode",
    );
    expect(screen.getByText("Stopped:").parentElement?.textContent).toContain(
      "10-minute time budget after processing 31 of 50 selected courses and staging 30",
    );
    expect(screen.queryByText("Capped:")).toBeNull();
  });

  it("shows exact audience options, linked English source, parsed values, and backend review reason", () => {
    render(
      <AiRepairProgress
        autonomous={{ ...base, phase: "needs_review" }}
        currentAttempt={2}
        audienceReviews={[{
          url: "https://official.example.edu/course",
          evidence: {
            status: "accepted",
            same_panel: true,
            linked_official: true,
            evidence: [
              { audience: "domestic", container: "audience", value: "home", label: "Domestic January 2027", intake_months: [1], intake_year: 2027 },
              { audience: "international", container: "audience", value: "intl", label: "International March 2027", intake_months: [3], intake_year: 2027, source_url: "https://official.example.edu/english" },
            ],
          },
          proposal: { status: "needs_review", reason: "unbalanced audience evidence", proposals: [] },
        }]}
      />,
    );

    expect(screen.getByText("Selected international option")).toBeTruthy();
    expect(screen.getAllByText("Parsed values:")[0].parentElement?.textContent).toContain("Jan, 2027");
    expect(screen.getByText("Linked official English source").closest("a")?.getAttribute("href"))
      .toBe("https://official.example.edu/english");
    expect(screen.getByText("Why this needs review:").parentElement?.textContent)
      .toContain("unbalanced audience evidence");
  });
});