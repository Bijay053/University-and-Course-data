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

test("accepts abbreviated start-date months (Jun, Sep) in intake evidence", () => {
  const snapshot = buildCourseReviewSnapshot(
    {
      courseName: "Master of Information Technology (Advanced)",
      degreeLevel: "Master",
      duration: 1,
      durationTerm: "Year",
      studyMode: "On Campus",
      courseLocation: "Ultimo campus",
      internationalFee: 72000,
      currency: "AUD",
      feeTerm: "Full Course",
      intakeMonths: ["June", "September"],
      ieltsOverall: 5.5,
    },
    [{
      url: "https://www.torrens.edu.au/courses/technology/master-of-information-technology-advanced",
      pageType: "course_page",
      extractionMethod: "browser",
      content: "Start date 01 Jun 2026 14 Sep 2026. International fee A$72000.",
    }],
  );

  assert.equal(snapshot.conflicts.some((conflict) => conflict.fieldKey === "intakeMonths"), false);
  assert.equal(snapshot.resolutions.find((resolution) => resolution.fieldKey === "intakeMonths")?.status, "accepted");
});

test("english_page does not leak into non-English fields (CSU shared-page case)", () => {
  const snapshot = buildCourseReviewSnapshot(
    {
      courseName: "Bachelor of Business Studies",
      degreeLevel: "Bachelor",
      duration: 3,
      durationTerm: "Year",
      studyMode: "On Campus",
      courseLocation: "Bathurst",
      internationalFee: 30000,
      currency: "AUD",
      feeTerm: "Annual",
      intakeMonths: ["February", "July"],
      ieltsOverall: 6.5,
    },
    [
      {
        url: "https://study.csu.edu.au/courses/business/bachelor-business-studies",
        pageType: "course_page",
        extractionMethod: "cheerio",
        content: "",
      },
      {
        url: "https://study.csu.edu.au/international/english-language-requirements",
        pageType: "english_page",
        extractionMethod: "cheerio",
        content: "IELTS 6.5 overall. PTE 58. TOEFL 79. Study at our Bathurst campus on campus full-time. February and July intake. Bachelor degree, 3 year duration, A$30000 international fee.",
      },
    ],
  );

  const studyModeCands = snapshot.candidates.filter((c) => c.fieldKey === "studyMode");
  assert.equal(
    studyModeCands.some((c) => c.pageType === "english_page"),
    false,
    "english_page must not produce a studyMode candidate",
  );
  const locationCands = snapshot.candidates.filter((c) => c.fieldKey === "courseLocation");
  assert.equal(
    locationCands.some((c) => c.pageType === "english_page"),
    false,
    "english_page must not produce a courseLocation candidate",
  );
  const feeCands = snapshot.candidates.filter((c) => c.fieldKey === "internationalFee");
  assert.equal(
    feeCands.some((c) => c.pageType === "english_page"),
    false,
    "english_page must not produce an internationalFee candidate",
  );
  const intakeCands = snapshot.candidates.filter((c) => c.fieldKey === "intakeMonths");
  assert.equal(
    intakeCands.some((c) => c.pageType === "english_page"),
    false,
    "english_page must not produce an intakeMonths candidate",
  );

  const ieltsCands = snapshot.candidates.filter((c) => c.fieldKey === "ieltsOverall");
  assert.equal(
    ieltsCands.some((c) => c.pageType === "english_page"),
    true,
    "english_page IS still allowed to produce an ieltsOverall candidate",
  );
});

test("preferApprovedValue keeps existing when allowReplace is false", () => {
  assert.equal(preferApprovedValue("old", "new", false), "old");
  assert.equal(preferApprovedValue("old", "new", true), "new");
  assert.equal(preferApprovedValue("old", "" as any, true), "old");
});

test("course_page with sparse pageText plus extracted-fields footer produces course_page evidence rows (CSU-style JS-hydrated case)", () => {
  // Simulates the production CSU bug: the static page text is essentially
  // just the H1 ("Bachelor of Business Studies"), but extractWithCheerio
  // pulled duration / location / fee / intakes from inline JSON. The
  // provenance footer (built by buildCoursePageProvenanceFooter in
  // scrape.ts) makes those values matchable so they get credited to the
  // course_page rather than dropped entirely.
  const sparseHeading = "Bachelor of Business Studies | CSU";
  const provenanceFooter =
    "\n\n[course-page extracted fields] courseName: Bachelor of Business Studies; degreeLevel: Bachelor; duration: 3 Year; location: Bathurst, Wagga Wagga; international fee: AUD 30000 Annual; intake: February, July; IELTS 6.5.";

  const snapshot = buildCourseReviewSnapshot(
    {
      courseName: "Bachelor of Business Studies",
      degreeLevel: "Bachelor",
      duration: 3,
      durationTerm: "Year",
      studyMode: "On Campus",
      courseLocation: "Bathurst, Wagga Wagga",
      internationalFee: 30000,
      currency: "AUD",
      feeTerm: "Annual",
      intakeMonths: ["February", "July"],
      ieltsOverall: 6.5,
    },
    [{
      url: "https://study.csu.edu.au/courses/bachelor-business-studies",
      pageType: "course_page",
      extractionMethod: "cheerio",
      content: sparseHeading + provenanceFooter,
    }],
  );

  const coursePageFields = new Set(
    snapshot.candidates
      .filter((c) => c.pageType === "course_page")
      .map((c) => c.fieldKey),
  );

  for (const required of ["courseName", "duration", "courseLocation", "internationalFee", "intakeMonths", "ieltsOverall"]) {
    assert.ok(
      coursePageFields.has(required as any),
      `expected at least one course_page evidence row for ${required}, got fields: ${[...coursePageFields].join(", ")}`,
    );
  }
});
