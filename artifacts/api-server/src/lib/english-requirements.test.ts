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

test("parses ASA admissions policy table wording for PTE and TOEFL", () => {
  const parsed = parseEnglishRequirementsFromText(
    "PTE (Pearson Test of English Academic) Academic Score of 50 TOEFL iBT (Test of English as a Foreign Language) 60 Duolingo English Test Overall score of 100",
    "shared",
  );

  assert.equal(parsed.pte.overall, 50);
  assert.equal(parsed.toefl.overall, 60);
  assert.equal(parsed.det.overall, 100);
});

test("parses CSU postgraduate coursework branch with concordance equivalents", () => {
  const parsed = parseEnglishRequirementsFromText(
    `
    Charles Sturt University
    2. for undergraduate courses have obtained an Academic IELTS within the last 2 years with:
       1. a minimum overall score of 6.0
       2. no individual score below 5.5
    3. for postgraduate coursework courses have obtained an Academic IELTS within the last 2 years with:
       1. a minimum overall score of 6.0
       2. no individual score below 6.0
    4. for higher degree by research courses have obtained at Academic IELTS within the last 2 years with:
       1. a minimum overall score of 6.5
       2. no individual score below 6.0
    Charles Sturt English Language Proficiency concordance tables
    Overall score IELTS Academic TOEFL IBT LANGUAGECERT Academic Test Michigan English Test (MET) PTE Academic TOEFL PBT
    6.0 60 65 53 50 550
    6.5 79 70 58 58 577
    Individual skill requirement IELTS Academic Michigan English Test (MET) TOEFL IBT PTE Academic TOEFL PBT
    5.5 53 43 51 51 7 16 8 18 42 4
    6 56 48 55 57 12 18 13 21 50 4.5
    6.5 59 53 59 64 20 20 19 24 58
    `,
    "shared",
    { courseName: "Master of Professional Information Technology", degreeLevel: "Master" },
  );

  assert.equal(parsed.ielts.overall, 6.0);
  assert.equal(parsed.ielts.listening, 6.0);
  assert.equal(parsed.pte.overall, 50);
  assert.equal(parsed.pte.listening, 50);
  assert.equal(parsed.toefl.overall, 60);
  assert.equal(parsed.toefl.listening, 12);
  assert.equal(parsed.toefl.reading, 13);
  assert.equal(parsed.toefl.writing, 21);
  assert.equal(parsed.toefl.speaking, 18);
});

test("does not apply CSU generic minimums to higher-English override courses", () => {
  const parsed = parseEnglishRequirementsFromText(
    `
    Charles Sturt University
    2. for undergraduate courses have obtained an Academic IELTS within the last 2 years with:
       1. a minimum overall score of 6.0
       2. no individual score below 5.5
    3. for postgraduate coursework courses have obtained an Academic IELTS within the last 2 years with:
       1. a minimum overall score of 6.0
       2. no individual score below 6.0
    4. for higher degree by research courses have obtained at Academic IELTS within the last 2 years with:
       1. a minimum overall score of 6.5
       2. no individual score below 6.0
    Courses with higher English language proficiency
    Master of Teaching (Primary)
    Master of Teaching (Secondary)
    `,
    "shared",
    { courseName: "Master of Teaching (Primary)", degreeLevel: "Master" },
  );

  assert.equal(parsed.ielts.overall, null);
  assert.equal(parsed.pte.overall, null);
  assert.equal(parsed.toefl.overall, null);
});

test("parses KBS bachelor and postgraduate English requirements column", () => {
  const parsed = parseEnglishRequirementsFromText(
    `
    Kaplan Business School
    English for Academic Purposes 1
    English for Academic Purposes 2
    Diploma
    Bachelor and all Postgraduate programs
    Academic IELTS, including One Skill Retake
    Overall 5.0 (with a minimum 5.0 in writing)
    Overall 5.5 (with a minimum 5.0 in writing)
    Overall 5.5 (with a minimum 5.0 in writing)
    Overall 6.0, with not less than 6.0 for Speaking and Writing and 5.5 for Listening and Reading
    PTE (Pearson Test of English Academic)
    Academic score of 41
    Academic score of 46
    Academic score of 46
    Academic score of 50
    TOEFL iBT (Test of English as a Foreign Language)
    Total band score of 37 OR Total score of 2.5 (with a minimum 3.0 in writing)
    Total band score of 58 OR Total score of 3.5 (with a minimum 3.0 in all papers)
    Total band score of 58 OR Total score of 3.5 (with a minimum 3.0 in all papers)
    Total band score of 72 OR Total score of 4 (with a minimum 4.0 for Speaking and a minimum 3.5 for Listening, Reading and Writing)
    CAE (Cambridge C1 Advanced Test)
    Overall band score of 169
    Duolingo English Test
    Overall score of 110
    `,
    "shared",
    { courseName: "Bachelor of Business Accounting", degreeLevel: "Bachelor" },
  );

  assert.equal(parsed.ielts.overall, 6);
  assert.equal(parsed.ielts.listening, 5.5);
  assert.equal(parsed.ielts.reading, 5.5);
  assert.equal(parsed.ielts.writing, 6);
  assert.equal(parsed.ielts.speaking, 6);
  assert.equal(parsed.pte.overall, 50);
  assert.equal(parsed.toefl.overall, 72);
  assert.equal(parsed.toefl.listening, 3.5);
  assert.equal(parsed.toefl.reading, 3.5);
  assert.equal(parsed.toefl.writing, 3.5);
  assert.equal(parsed.toefl.speaking, 4);
  assert.equal(parsed.cae.overall, 169);
  assert.equal(parsed.det.overall, 110);
});
