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

test("rejects online study mode without campus evidence", () => {
  const snapshot = buildCourseReviewSnapshot(
    {
      courseName: "GCITS",
      duration: 0.5,
      durationTerm: "Year",
      studyMode: "Online",
      internationalFee: 9000,
      currency: "AUD",
      intakeMonths: ["May"],
      ieltsOverall: 6.5,
      courseLocation: undefined,
    },
    [{
      url: "https://vit.edu.au/mits/gcits",
      pageType: "course_page",
      extractionMethod: "cheerio",
      content: "Graduate Certificate. Learning Mode Online. International student. IELTS Academic 6.5. Fee $9,000.",
    }],
  );

  assert.equal(snapshot.eligibility.eligibilityStatus, "rejected");
  assert.equal(snapshot.eligibility.reason, "Study mode is online with no physical campus evidence");
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

test("does not create false conflicts from same-page noisy evidence", () => {
  const snapshot = buildCourseReviewSnapshot(
    {
      courseName: "Bachelor of Nursing",
      degreeLevel: "Bachelor",
      duration: 3,
      durationTerm: "Year",
      studyMode: "On Campus",
      courseLocation: "Sydney, Melbourne, Brisbane, Adelaide",
      internationalFee: 105000,
      currency: "AUD",
      feeTerm: "Full Course",
      intakeMonths: ["September"],
      ieltsOverall: 7,
    },
    [{
      url: "https://www.torrens.edu.au/courses/health/bachelor-of-nursing",
      pageType: "course_page",
      extractionMethod: "cheerio",
      content: "Study mode On campus Campus locations Sydney, Melbourne, Brisbane, Adelaide. Apply online now. Main intake is September. Academic IELTS 7.0 in three components and 6.5 in writing. International fee A$105000 full course.",
    }],
  );

  assert.equal(snapshot.conflicts.some((conflict) => conflict.fieldKey === "studyMode"), false);
  assert.equal(snapshot.conflicts.some((conflict) => conflict.fieldKey === "intakeMonths"), false);
  assert.equal(snapshot.conflicts.some((conflict) => conflict.fieldKey === "ieltsOverall"), false);
  assert.equal(snapshot.resolutions.find((resolution) => resolution.fieldKey === "studyMode")?.status, "accepted");
  assert.equal(snapshot.resolutions.find((resolution) => resolution.fieldKey === "intakeMonths")?.status, "accepted");
  assert.equal(snapshot.resolutions.find((resolution) => resolution.fieldKey === "ieltsOverall")?.status, "accepted");
  assert.equal(snapshot.resolutions.find((resolution) => resolution.fieldKey === "internationalFee")?.status, "accepted");
});

test("ignores unrelated university-level sources for course-specific fee and duration", () => {
  const snapshot = buildCourseReviewSnapshot(
    {
      courseName: "BBus - Marketing Specialisation",
      degreeLevel: "Bachelor",
      duration: 3,
      durationTerm: "Year",
      studyMode: "Blended",
      courseLocation: "Sydney, Melbourne, Geelong, Adelaide",
      internationalFee: 48000,
      currency: "AUD",
      feeTerm: "Full Course",
      intakeMonths: ["March", "May", "August", "September", "December"],
      ieltsOverall: 6,
    },
    [
      {
        url: "https://vit.edu.au/bachelor-of-business/bbus-marketing",
        pageType: "course_page",
        extractionMethod: "cheerio",
        content: "2026 intakes: 02-Mar-2026 25-May-2026 3-Aug-2026 7-Sep-2026 7-Dec-2026. Duration 3 Years (Full-Time). INTERNATIONAL (On campus) Duration 3 Years (6 semesters) $48,000. Delivery Mode Face-to-Face/Blended Learning mode for International On-Campus Students.",
      },
      {
        url: "https://vit.edu.au/",
        pageType: "english_page",
        extractionMethod: "cheerio",
        content: "5 year pathway. AUD 10394. Start in May.",
      },
    ],
  );

  assert.equal(snapshot.conflicts.some((conflict) => conflict.fieldKey === "duration"), false);
  assert.equal(snapshot.conflicts.some((conflict) => conflict.fieldKey === "internationalFee"), false);
  assert.equal(snapshot.resolutions.find((resolution) => resolution.fieldKey === "duration")?.status, "accepted");
  assert.equal(snapshot.resolutions.find((resolution) => resolution.fieldKey === "internationalFee")?.status, "accepted");
});
