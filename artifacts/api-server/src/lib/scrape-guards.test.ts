import test from "node:test";
import assert from "node:assert/strict";
import {
  hasCourseSpecificFeeEvidence,
  isGenericCourseCategoryName,
  shouldTrustGenericUniversityFeeFallback,
} from "./scrape-guards.ts";

test("rejects generic category names as non-course pages", () => {
  assert.equal(isGenericCourseCategoryName("Design"), true);
  assert.equal(isGenericCourseCategoryName("Business"), true);
  assert.equal(isGenericCourseCategoryName("Digital Badges"), true);
  assert.equal(isGenericCourseCategoryName("Master's Degrees"), true);
  assert.equal(isGenericCourseCategoryName("Graduate Diploma"), true);
  assert.equal(isGenericCourseCategoryName("Master of Design"), false);
});

test("requires course-specific evidence before trusting generic fee page fallback", () => {
  const genericFeeText = `
    University Tuition Fees
    There is a higher limit of $186,544 for certain approved medicine courses.
    International students
  `;

  assert.equal(
    shouldTrustGenericUniversityFeeFallback(
      "https://www.torrens.edu.au/international-fees",
      "Master Of Business Administration Mba",
      genericFeeText,
      [186544],
    ),
    false,
  );
});

test("accepts fee fallback when the generic page explicitly mentions the course", () => {
  const courseSpecificText = `
    Master of Business Administration MBA
    Check the international course fee schedule for the cost of your course.
    Tuition fee A$48,000 full course
  `;

  assert.equal(hasCourseSpecificFeeEvidence("Master Of Business Administration Mba", courseSpecificText), true);
  assert.equal(
    shouldTrustGenericUniversityFeeFallback(
      "https://www.torrens.edu.au/international-fees",
      "Master Of Business Administration Mba",
      courseSpecificText,
      [48000],
    ),
    true,
  );
});
