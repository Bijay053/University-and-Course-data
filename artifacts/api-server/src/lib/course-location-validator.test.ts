import { test } from "node:test";
import assert from "node:assert/strict";
import { validateCourseLocation } from "./course-location-validator.ts";

test("strips garbage placeholder", () => {
  assert.equal(validateCourseLocation("test"), null);
});

test("rejects 'On Campus' (study mode, not a location)", () => {
  assert.equal(validateCourseLocation("On Campus"), null);
});

test("rejects google error message", () => {
  assert.equal(
    validateCourseLocation("If you're having trouble accessing Google Search, please click here for support"),
    null,
  );
});

test("rejects 'Session 1 : March 2' date label", () => {
  assert.equal(validateCourseLocation("Session 1 : March 2"), null);
});

test("strips exchange-partner foreign campuses, keeps Port Macquarie", () => {
  const out = validateCourseLocation(
    "Port Macquarie, Jilin Uni - Finance & Economics, Tianjin University of Commerce, Yangzhou University",
  );
  assert.equal(out, "Port Macquarie");
});

test("rejects partner uni 'SPACE University of Hong Kong'", () => {
  assert.equal(validateCourseLocation("SPACE University of Hong Kong"), null);
});

test("rejects prose >150 chars", () => {
  const long = "This program is delivered in partnership with several institutions across Asia and is available for cohort intakes throughout the year subject to enrolment minimums.";
  assert.equal(validateCourseLocation(long), null);
});

test("keeps multiple valid AU campuses", () => {
  const out = validateCourseLocation("Sydney, Melbourne, Brisbane");
  assert.equal(out, "Sydney, Melbourne, Brisbane");
});

test("keeps Bathurst (CSU campus)", () => {
  assert.equal(validateCourseLocation("Bathurst"), "Bathurst");
});

test("returns null for empty / whitespace", () => {
  assert.equal(validateCourseLocation(""), null);
  assert.equal(validateCourseLocation("   "), null);
  assert.equal(validateCourseLocation(null), null);
});
