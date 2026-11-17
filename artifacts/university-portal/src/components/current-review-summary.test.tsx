// @vitest-environment jsdom
import React from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { CurrentReviewSummary } from "./current-review-summary";

afterEach(cleanup);

it("distinguishes current 149 pending entries from the original 182", () => {
  render(<CurrentReviewSummary pendingCount={149} originallyStaged={182} />);
  expect(screen.getByTestId("current-review-summary").textContent).toContain("Current review: 149 pending entries");
  expect(screen.getByText(/33 fewer than/).textContent).toContain("182 originally staged");
  expect(screen.getByText(/logs are historical/)).toBeTruthy();
});

it("updates counts and the reload time after a saved merge or undo", () => {
  const { rerender } = render(<CurrentReviewSummary pendingCount={149} originallyStaged={182} refreshedAt={1000} />);
  rerender(<CurrentReviewSummary pendingCount={147} originallyStaged={182} refreshedAt={2000} />);
  expect(screen.getByTestId("current-review-summary").textContent).toContain("35 fewer");
  expect(document.querySelector("time")?.dateTime).toBe(new Date(2000).toISOString());
  rerender(<CurrentReviewSummary pendingCount={149} originallyStaged={182} refreshedAt={3000} />);
  expect(screen.getByTestId("current-review-summary").textContent).toContain("33 fewer");
});

it("handles new entries, zero pending, and missing historical counters honestly", () => {
  const { rerender } = render(<CurrentReviewSummary pendingCount={184} originallyStaged={182} />);
  expect(screen.getByText(/2 more than/)).toBeTruthy();
  rerender(<CurrentReviewSummary pendingCount={0} originallyStaged={182} />);
  expect(screen.getByTestId("current-review-summary").textContent).toContain("0 pending entries");
  rerender(<CurrentReviewSummary pendingCount={1} />);
  expect(screen.getByTestId("current-review-summary").textContent).toContain("1 pending entry.");
  expect(screen.queryByText(/originally staged/)).toBeNull();
});

it("never substitutes original staged totals for an unavailable current count", () => {
  render(<CurrentReviewSummary pendingCount={null} originallyStaged={182} />);
  expect(screen.queryByTestId("current-review-summary")).toBeNull();
  expect(screen.getByText(/count has not loaded/)).toBeTruthy();
});
