import { test } from "node:test";
import assert from "node:assert/strict";
import { validateNameAgainstSlug } from "./course-name-normalizer.ts";

test("preserves name when slug also lacks preposition", () => {
  const out = validateNameAgainstSlug(
    "Bachelor Business Studies",
    "https://study.csu.edu.au/international/courses/bachelor-business-studies",
  );
  assert.equal(out, "Bachelor Business Studies");
});

test("reconstructs missing 'of' from slug", () => {
  const out = validateNameAgainstSlug(
    "Bachelor Business Studies",
    "https://study.csu.edu.au/international/courses/bachelor-of-business-studies",
  );
  assert.equal(out, "Bachelor of Business Studies");
});

test("reconstructs missing 'of' for Master degrees", () => {
  const out = validateNameAgainstSlug(
    "Master Health Management",
    "https://example.edu.au/courses/master-of-health-management",
  );
  assert.equal(out, "Master of Health Management");
});

test("does not touch non-degree names", () => {
  const out = validateNameAgainstSlug(
    "International Student Information",
    "https://example.edu.au/info/international-of-student-information",
  );
  assert.equal(out, "International Student Information");
});

test("does not rewrite when slug content is unrelated", () => {
  const out = validateNameAgainstSlug(
    "Bachelor Business",
    "https://example.edu.au/courses/master-of-fine-arts",
  );
  assert.equal(out, "Bachelor Business");
});

test("ignores invalid url gracefully", () => {
  const out = validateNameAgainstSlug("Bachelor Business", "");
  assert.equal(out, "Bachelor Business");
});

test("preserves Roman numerals from slug", () => {
  const out = validateNameAgainstSlug(
    "Master Education",
    "https://example.edu.au/courses/master-of-education-ii",
  );
  assert.equal(out, "Master of Education II");
});

test("strips .html suffix from slug", () => {
  const out = validateNameAgainstSlug(
    "Bachelor Nursing",
    "https://example.edu.au/courses/bachelor-of-nursing.html",
  );
  assert.equal(out, "Bachelor of Nursing");
});
