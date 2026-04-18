import test from "node:test";
import assert from "node:assert/strict";
import { buildCourseReviewSnapshot, preferApprovedValue } from "./review-engine.ts";

test("rejects domestic-only courses", () => {
  const snapshot = buildCourseReviewSnapshot(
    {
      courseName: "Bachelor of Business",
      duration: 3,
      durationTerm: "Year",
      studyMode: "On Campus",
      courseLocation: "Sydney",
      internationalFee: 32000,
      currency: "AUD",
      intakeMonths: ["February", "July"],
      ieltsOverall: 6,
      domesticOnly: true,
    },
    [{
      url: "https://example.edu/course",
      pageType: "course_page",
      extractionMethod: "cheerio",
      content: "Bachelor of Business. Domestic only. Sydney campus. IELTS 6.0. International fee A$32000.",
    }],
  );

  assert.equal(snapshot.eligibility.eligibilityStatus, "rejected");
  assert.equal(snapshot.autoPublishStatus, "rejected");
});

test("rejects online-only courses", () => {
  const snapshot = buildCourseReviewSnapshot(
    {
      courseName: "Master of IT",
      duration: 2,
      durationTerm: "Year",
      studyMode: "Online only",
      internationalFee: 28000,
      currency: "AUD",
      intakeMonths: ["March"],
      ieltsOverall: 6.5,
      onlineOnly: true,
    },
    [{
      url: "https://example.edu/course",
      pageType: "course_page",
      extractionMethod: "cheerio",
      content: "Master of IT. Fully online. Online only. IELTS 6.5. International fee A$28000.",
    }],
  );

  assert.equal(snapshot.eligibility.eligibilityStatus, "rejected");
  assert.equal(snapshot.autoPublishStatus, "rejected");
});

test("routes fee conflicts to review", () => {
  const snapshot = buildCourseReviewSnapshot(
    {
      courseName: "Bachelor of Nursing",
      duration: 3,
      durationTerm: "Year",
      studyMode: "On Campus",
      courseLocation: "Melbourne",
      internationalFee: 32000,
      currency: "AUD",
      intakeMonths: ["February", "July"],
      ieltsOverall: 7,
    },
    [
      {
        url: "https://example.edu/course",
        pageType: "course_page",
        extractionMethod: "cheerio",
        content: "Bachelor of Nursing. Melbourne campus. On campus. Intake February and July. IELTS 7.0. International fee A$32000.",
      },
      {
        url: "https://example.edu/international-fees",
        pageType: "fee_page",
        extractionMethod: "cheerio",
        content: "International tuition fee for Bachelor of Nursing is AUD 29500.",
      },
    ],
  );

  assert.ok(snapshot.conflicts.some((conflict) => conflict.fieldKey === "internationalFee"));
  assert.equal(snapshot.resolutions.find((resolution) => resolution.fieldKey === "internationalFee")?.status, "needs_review");
  assert.equal(snapshot.autoPublishStatus, "pending_review");
});

test("preserves approved values when replacement is not allowed", () => {
  assert.equal(preferApprovedValue("Sydney", "Online", false), "Sydney");
  assert.equal(preferApprovedValue("Sydney", "", true), "Sydney");
  assert.equal(preferApprovedValue("Sydney", "Melbourne", true), "Melbourne");
});
