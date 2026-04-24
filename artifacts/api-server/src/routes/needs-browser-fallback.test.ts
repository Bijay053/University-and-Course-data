import { test } from "node:test";
import assert from "node:assert/strict";
import { needsBrowserFallback, type BrowserFallbackInput } from "../lib/needs-browser-fallback.ts";

const CSU_URL = "https://study.csu.edu.au/courses/postgraduate/master-information-technology";
const NON_CSU_URL = "https://www.example.edu.au/courses/master-it";

function baseData(overrides: Partial<BrowserFallbackInput> = {}): BrowserFallbackInput {
  return {
    courseName: "Master of Information Technology",
    ieltsOverall: 6.5,
    pteOverall: 58,
    toeflOverall: 79,
    internationalFee: 32000,
    duration: "2 Year",
    degreeLevel: "Postgraduate",
    studyMode: "On Campus",
    intakeMonths: ["February", "July"],
    ...overrides,
  };
}

test("CSU page missing courseLocation but otherwise complete escalates to browser", () => {
  const data = baseData({ courseLocation: undefined });
  assert.equal(needsBrowserFallback(data, CSU_URL), true);
});

test("CSU page with empty/whitespace courseLocation also escalates", () => {
  const data = baseData({ courseLocation: "  " });
  assert.equal(needsBrowserFallback(data, CSU_URL), true);
});

test("CSU online-only page missing courseLocation does NOT escalate", () => {
  const data = baseData({ courseLocation: undefined, studyMode: "Online" });
  assert.equal(needsBrowserFallback(data, CSU_URL), false);
});

test("CSU page with courseLocation populated does NOT escalate", () => {
  const data = baseData({ courseLocation: "Bathurst, Wagga Wagga" });
  assert.equal(needsBrowserFallback(data, CSU_URL), false);
});

test("Non-CSU page missing courseLocation does NOT escalate purely on the CSU rule", () => {
  // Other heuristics may still escalate this page, but the CSU branch must not
  // fire for unrelated domains. Provide intakes so the generic
  // "no location AND no intakes" rule doesn't fire either.
  const data = baseData({ courseLocation: undefined });
  assert.equal(needsBrowserFallback(data, NON_CSU_URL), false);
});

test("CSU rule does not fire when URL argument is omitted (legacy callers)", () => {
  // Belt-and-braces: an old call site that forgets to pass the URL must not
  // accidentally escalate every CSU-shaped payload it sees.
  const data = baseData({ courseLocation: undefined });
  // Without URL, the function falls through to the generic rule; with intakes
  // populated and the rest of the fields healthy, escalation should be false.
  assert.equal(needsBrowserFallback(data), false);
});
