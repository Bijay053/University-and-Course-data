import { test } from "node:test";
import assert from "node:assert/strict";
import { fillFromConcordance, lookupEquivalents } from "./concordance-cache.ts";

test("lookupEquivalents picks the highest row whose ielts <= input", () => {
  const row = lookupEquivalents(6.5, "https://study.csu.edu.au/x");
  assert.equal(row?.ielts, 6.5);
  assert.equal(row?.pte, 58);
  assert.equal(row?.toefl, 79);
});

test("lookupEquivalents handles between-band IELTS by flooring", () => {
  const row = lookupEquivalents(6.7, "https://study.csu.edu.au/x");
  assert.equal(row?.ielts, 6.5);
});

test("lookupEquivalents returns null below floor", () => {
  assert.equal(lookupEquivalents(4.5, "https://study.csu.edu.au/x"), null);
});

test("fillFromConcordance only fills empty slots, never overwrites", () => {
  const course: any = { ieltsOverall: 6.5, pteOverall: 50, toeflOverall: null, cambridgeOverall: null };
  const out = fillFromConcordance(course, "https://study.csu.edu.au/x");
  assert.deepEqual(out.filled.sort(), ["cambridgeOverall", "duolingoOverall", "toeflOverall"].sort());
  assert.equal(course.pteOverall, 50, "pre-existing pte must not be overwritten");
  assert.equal(course.toeflOverall, 79);
  assert.equal(course.cambridgeOverall, 176);
  assert.equal(course.duolingoOverall, 115);
});

test("fillFromConcordance is no-op without ielts anchor", () => {
  const course: any = { ieltsOverall: null, pteOverall: null };
  const out = fillFromConcordance(course, "https://study.csu.edu.au/x");
  assert.deepEqual(out.filled, []);
  assert.equal(course.pteOverall, null);
});
