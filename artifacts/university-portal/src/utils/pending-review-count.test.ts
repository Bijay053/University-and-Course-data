import { describe, expect, it } from "vitest";
import { countPendingReviewCourses } from "./pending-review-count";

describe("countPendingReviewCourses", () => {
  it("counts only pending courses from the current API envelope", () => {
    expect(countPendingReviewCourses({
      courses: [
        { id: 1, status: "pending" },
        { id: 2, status: "approved" },
        { id: 3, status: "pending" },
      ],
    })).toBe(2);
  });

  it("keeps the same rule for the legacy bare-array response", () => {
    expect(countPendingReviewCourses([
      { id: 1, status: "pending" },
      { id: 2, status: "rejected" },
    ])).toBe(1);
  });

  it("does not invent a review count from an invalid response", () => {
    expect(countPendingReviewCourses({ unexpected: [] })).toBeNull();
  });
});