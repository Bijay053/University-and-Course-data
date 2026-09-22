// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  isTargetedRetryAllFiltered,
  TargetedRetryAllFilteredNotice,
} from "./targeted-retry-diagnostic";

afterEach(cleanup);

describe("targeted retry all-filtered diagnostic", () => {
  it("honestly describes mixed zero-work diagnostics and offers official URL recovery", () => {
    const onReport = vi.fn();
    render(
      <TargetedRetryAllFilteredNotice
        diagnostic={{
          error_type: "targeted_retry_all_filtered",
          selected_count: 8,
          filtered_count: 3,
          processed_count: 0,
          recovery_action: "report_official_course_urls",
        }}
        diagnosticId="retry-123"
        onReportOfficialUrls={onReport}
      />,
    );

    expect(screen.getByText("No remaining selected course URLs were processed.")).toBeTruthy();
    expect(screen.getByText(/5 of the 8 selected course URLs were already resolved/)).toBeTruthy();
    expect(screen.getByText(
      /URL filters prevented 3 remaining unresolved selected course URLs from being processed/,
    )).toBeTruthy();
    expect(screen.getByText(/Existing review records were not changed/)).toBeTruthy();
    expect(screen.queryByText(/filters removed all 8/i)).toBeNull();
    fireEvent.click(screen.getByTestId("button-report-filtered-course-urls-retry-123"));
    expect(onReport).toHaveBeenCalledOnce();
  });

  it("does not claim all selected URLs were filtered when filtered_count is unavailable", () => {
    render(
      <TargetedRetryAllFilteredNotice
        diagnostic={{
          error_type: "targeted_retry_all_filtered",
          selected_count: 8,
          processed_count: 0,
          recovery_action: "report_official_course_urls",
        }}
        diagnosticId="legacy-retry"
        onReportOfficialUrls={vi.fn()}
      />,
    );

    expect(screen.getByText(
      /URL filters prevented the remaining unresolved selected course URLs from being processed/,
    )).toBeTruthy();
    expect(screen.queryByText(/filters removed all/i)).toBeNull();
  });

  it("does not infer the diagnostic from message text or unrelated failures", () => {
    expect(isTargetedRetryAllFiltered({
      error_type: "other_failure",
      message: "No selected courses were processed.",
    })).toBe(false);

    const { container } = render(
      <TargetedRetryAllFilteredNotice
        diagnostic={{ error_type: "other_failure" }}
        diagnosticId="other"
        onReportOfficialUrls={vi.fn()}
      />,
    );
    expect(container.innerHTML).toBe("");
  });
});