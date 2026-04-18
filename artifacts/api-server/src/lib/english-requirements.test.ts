import test from "node:test";
import assert from "node:assert/strict";
import { parseEnglishRequirementsFromText } from "./english-requirements.ts";

test("parses VIT IELTS wording with 'overall score of'", () => {
  const parsed = parseEnglishRequirementsFromText(
    "An IELTS Academic overall score of 6.5, with no band below 6.0, or an equivalent score in another approved English language test.",
    "browser",
  );

  assert.equal(parsed.ielts.overall, 6.5);
  assert.equal(parsed.ielts.listening, 6.0);
  assert.equal(parsed.ielts.reading, 6.0);
  assert.equal(parsed.ielts.writing, 6.0);
  assert.equal(parsed.ielts.speaking, 6.0);
});

test("parses VIT vocational IELTS band score wording", () => {
  const parsed = parseEnglishRequirementsFromText(
    "Cert III in EAL / Academic IELTS band score of 5.5 or equivalent",
    "browser",
  );

  assert.equal(parsed.ielts.overall, 5.5);
});
