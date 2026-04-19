import test from "node:test";
import assert from "node:assert/strict";
import { applyFeedbackRules, inferFeedbackIssue, buildScrapeFeedbackHints } from "./feedback-engine.ts";
import { buildCourseReviewSnapshot } from "./review-engine.ts";

test("infers domestic fee feedback type", () => {
  assert.equal(
    inferFeedbackIssue("VIT picked domestic fee instead of international fee", "internationalFee"),
    "domestic_fee_picked",
  );
});

test("buildScrapeFeedbackHints enables stricter international fee + PDF preference from saved rows", () => {
  const h = buildScrapeFeedbackHints([
    {
      issueType: "domestic_fee_picked",
      reason: "Example: VIT picked domestic fee instead of international fee. Use international fee page or PDF only.",
    },
  ]);
  assert.equal(h.strictInternationalFee, true);
  assert.equal(h.preferFeePdfFirst, true);
  assert.equal(h.activeCount, 1);
});

test("feedback demotes weak fee evidence on rerun", () => {
  const snapshot = buildCourseReviewSnapshot(
    {
      courseName: "Bachelor of Business",
      duration: 3,
      durationTerm: "Year",
      studyMode: "On Campus",
      courseLocation: "Sydney",
      internationalFee: 36000,
      currency: "AUD",
      intakeMonths: ["February", "July"],
      ieltsOverall: 6,
    },
    [{
      url: "https://vit.edu.au/bachelor-of-business",
      pageType: "course_page",
      extractionMethod: "cheerio",
      content: "Bachelor of Business Sydney campus on campus February July IELTS 6.0 international fee AUD 36000",
    }],
  );

  applyFeedbackRules(snapshot, [{
    fieldKey: "internationalFee",
    issueType: "domestic_fee_picked",
    reason: "Use international fee source only for this course",
  }]);

  assert.equal(snapshot.resolutions.find((item) => item.fieldKey === "internationalFee")?.status, "needs_review");
  assert.equal(snapshot.autoPublishStatus, "pending_review");
});
