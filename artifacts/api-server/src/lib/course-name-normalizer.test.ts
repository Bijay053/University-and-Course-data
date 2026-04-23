import { test } from "node:test";
import assert from "node:assert/strict";
import { normalizeCourseNameCasing, validateNameAgainstSlug } from "./course-name-normalizer.ts";

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

test("preserves MBA acronym in course name", () => {
  assert.equal(normalizeCourseNameCasing("Mba Finance"), "MBA Finance");
});

test("preserves BBUS acronym in course name", () => {
  assert.equal(normalizeCourseNameCasing("Bbus Marketing"), "BBUS Marketing");
});

test("preserves GDBA acronym and lowercase 'of' across a dash separator", () => {
  assert.equal(
    normalizeCourseNameCasing("Gdba - Graduate Diploma Of Business Administration"),
    "GDBA - Graduate Diploma of Business Administration",
  );
});

test("lowercases function words mid-title", () => {
  assert.equal(
    normalizeCourseNameCasing("Bachelor Of Business Studies"),
    "Bachelor of Business Studies",
  );
  assert.equal(
    normalizeCourseNameCasing("Master Of Information Technology And Systems"),
    "Master of Information Technology and Systems",
  );
});

test("does not lowercase a function word when it leads the title", () => {
  assert.equal(normalizeCourseNameCasing("Of Mice And Men"), "Of Mice and Men");
});

test("preserves already-correct names", () => {
  assert.equal(
    normalizeCourseNameCasing("Bachelor of Business Studies"),
    "Bachelor of Business Studies",
  );
  assert.equal(normalizeCourseNameCasing("MBA Finance"), "MBA Finance");
});

test("normalizes casing on names that pass through validateNameAgainstSlug unchanged", () => {
  // No matching slug, no degree head — should still fix the casing.
  assert.equal(
    validateNameAgainstSlug("Mba Finance", "https://example.edu.au/courses/mba-finance"),
    "MBA Finance",
  );
  // Degree head, no slug preposition mismatch — casing still fixed.
  assert.equal(
    validateNameAgainstSlug(
      "Bachelor Of Business Studies",
      "https://example.edu.au/courses/bachelor-of-business-studies",
    ),
    "Bachelor of Business Studies",
  );
});

test("rebuilt-from-slug names also use acronym/function-word casing", () => {
  assert.equal(
    validateNameAgainstSlug(
      "Graduate Diploma Business Administration",
      "https://example.edu.au/courses/gdba-graduate-diploma-of-business-administration",
    ),
    "GDBA Graduate Diploma of Business Administration",
  );
});

test("preserves parentheses and inner casing", () => {
  assert.equal(
    normalizeCourseNameCasing("Bachelor Of Business (mba Pathway)"),
    "Bachelor of Business (MBA Pathway)",
  );
});
