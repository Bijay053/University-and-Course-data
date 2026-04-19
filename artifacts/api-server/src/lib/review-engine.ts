const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

const MONTH_ABBREV_TO_FULL: Record<string, string> = {
  jan: "January", feb: "February", mar: "March", apr: "April",
  may: "May", jun: "June", jul: "July", aug: "August",
  sep: "September", oct: "October", nov: "November", dec: "December",
};

const MONTH_TOKEN_PATTERN = `${MONTH_NAMES.join("|")}|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec`;

/** Collect full month names from text (supports Jun, Sep, etc.). */
function monthsFoundInText(text: string): string[] {
  const seen: string[] = [];
  const re = new RegExp(`\\b(${MONTH_TOKEN_PATTERN})\\b`, "gi");
  for (const m of text.matchAll(re)) {
    const raw = m[1];
    if (!raw) continue;
    const full = MONTH_NAMES.includes(raw)
      ? raw
      : MONTH_ABBREV_TO_FULL[raw.toLowerCase().slice(0, 3)];
    if (full && !seen.includes(full)) seen.push(full);
  }
  return seen;
}

export const REVIEW_FIELD_KEYS = [
  "courseName",
  "degreeLevel",
  "duration",
  "studyMode",
  "courseLocation",
  "internationalFee",
  "intakeMonths",
  "ieltsOverall",
  "pteOverall",
  "toeflOverall",
  "academicRequirement",
] as const;

export type ReviewFieldKey = typeof REVIEW_FIELD_KEYS[number];

export type ReviewCourseData = {
  courseName: string;
  courseWebsite?: string;
  courseLocation?: string;
  duration?: number;
  durationTerm?: string;
  studyMode?: string;
  degreeLevel?: string;
  description?: string;
  intakeMonths?: string[];
  internationalFee?: number;
  feeTerm?: string;
  feeYear?: number;
  currency?: string;
  ieltsOverall?: number;
  ieltsListening?: number;
  ieltsSpeaking?: number;
  ieltsWriting?: number;
  ieltsReading?: number;
  pteOverall?: number;
  toeflOverall?: number;
  cambridgeOverall?: number;
  duolingoOverall?: number;
  academicLevel?: string;
  academicScore?: number;
  scoreType?: string;
  academicCountry?: string;
  otherRequirement?: string;
  domesticOnly?: boolean;
  onlineOnly?: boolean;
};

export interface ReviewSource {
  url: string;
  pageType: "course_page" | "fee_page" | "english_page" | "requirements_page" | "brochure_pdf" | "fee_pdf" | "listing_page" | "other";
  extractionMethod: "cheerio" | "browser" | "pdf" | "ai" | "manual" | "import";
  content: string;
}

export interface FieldCandidate {
  fieldKey: ReviewFieldKey;
  candidateValue: string | null;
  normalizedValue: string | null;
  sourceUrl: string | null;
  pageType: ReviewSource["pageType"] | "derived";
  extractionMethod: ReviewSource["extractionMethod"] | "derived";
  rawText: string | null;
  snippet: string | null;
  confidence: number;
  validationStatus: "accepted" | "rejected" | "needs_review";
  decisionStatus: "accepted" | "rejected" | "needs_review";
  decisionScore: number;
  selected: boolean;
}

export interface FieldConflict {
  fieldKey: ReviewFieldKey;
  valueA: string | null;
  valueB: string | null;
  conflictType: "source_mismatch" | "existing_mismatch" | "eligibility_mismatch";
  reason: string;
}

export interface FieldResolution {
  fieldKey: ReviewFieldKey;
  finalValue: string | null;
  status: "accepted" | "rejected" | "needs_review";
  decisionScore: number;
  reason: string | null;
}

export interface EligibilityAssessment {
  studentMarket: "international" | "both" | "domestic_only" | "unknown";
  deliveryMode: "on_campus" | "mixed" | "online_only" | "unknown";
  internationalEligible: boolean;
  onCampusAvailable: boolean;
  eligibilityStatus: "eligible" | "rejected" | "needs_review";
  reason: string;
  confidence: number;
  evidenceText: string | null;
}

export interface CourseReviewSnapshot {
  candidates: FieldCandidate[];
  resolutions: FieldResolution[];
  conflicts: FieldConflict[];
  eligibility: EligibilityAssessment;
  autoPublishStatus: "approved" | "pending_review" | "rejected";
  decisionScore: number;
}

export function preferApprovedValue<T>(existingValue: T, incomingValue: T, allowReplace: boolean): T {
  if (!allowReplace) return existingValue;
  if (incomingValue == null || incomingValue === "" as T) return existingValue;
  return incomingValue;
}

function normalizeWhitespace(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

function escapeRegex(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function valueToString(fieldKey: ReviewFieldKey, data: ReviewCourseData): string | null {
  switch (fieldKey) {
    case "courseName":
      return data.courseName || null;
    case "degreeLevel":
      return data.degreeLevel || null;
    case "duration":
      return data.duration != null && data.durationTerm ? `${data.duration} ${data.durationTerm}` : null;
    case "studyMode":
      return data.studyMode || null;
    case "courseLocation":
      return data.courseLocation || null;
    case "internationalFee":
      return data.internationalFee != null ? `${data.currency || "AUD"} ${data.internationalFee}${data.feeTerm ? ` ${data.feeTerm}` : ""}` : null;
    case "intakeMonths":
      return data.intakeMonths?.length ? data.intakeMonths.join(", ") : null;
    case "ieltsOverall":
      return data.ieltsOverall != null ? String(data.ieltsOverall) : null;
    case "pteOverall":
      return data.pteOverall != null ? String(data.pteOverall) : null;
    case "toeflOverall":
      return data.toeflOverall != null ? String(data.toeflOverall) : null;
    case "academicRequirement":
      if (data.academicLevel) return data.academicScore != null ? `${data.academicLevel} ${data.academicScore}` : data.academicLevel;
      if (data.otherRequirement) return data.otherRequirement;
      return null;
  }
}

function normalizeFieldValue(fieldKey: ReviewFieldKey, value: string | null): string | null {
  if (!value) return null;
  const cleaned = normalizeWhitespace(value);
  if (fieldKey === "courseName" || fieldKey === "degreeLevel" || fieldKey === "studyMode" || fieldKey === "courseLocation" || fieldKey === "academicRequirement") {
    return cleaned.toLowerCase();
  }
  if (fieldKey === "intakeMonths") {
    return cleaned
      .split(",")
      .map((part) => part.trim().toLowerCase())
      .filter(Boolean)
      .sort()
      .join(",");
  }
  if (fieldKey === "ieltsOverall" || fieldKey === "pteOverall" || fieldKey === "toeflOverall") {
    const numeric = parseFloat(cleaned);
    if (Number.isFinite(numeric)) return String(numeric);
  }
  return cleaned.toLowerCase().replace(/\b(aud|a\$)\b/g, "aud").replace(/,/g, "");
}

function sourceBaseConfidence(source: ReviewSource): number {
  const pageWeight: Record<ReviewSource["pageType"], number> = {
    course_page: 0.9,
    fee_page: 0.95,
    english_page: 0.95,
    requirements_page: 0.88,
    brochure_pdf: 0.82,
    fee_pdf: 0.9,
    listing_page: 0.45,
    other: 0.55,
  };
  const methodWeight: Record<ReviewSource["extractionMethod"], number> = {
    cheerio: 0.03,
    browser: 0.04,
    pdf: 0.02,
    ai: -0.08,
    manual: 0.05,
    import: -0.05,
  };
  return Math.max(0.2, Math.min(0.99, pageWeight[source.pageType] + methodWeight[source.extractionMethod]));
}

function findSnippet(source: ReviewSource, patterns: RegExp[]): string | null {
  for (const pattern of patterns) {
    const match = source.content.match(pattern);
    if (!match || match.index == null) continue;
    const start = Math.max(0, match.index - 90);
    const end = Math.min(source.content.length, match.index + Math.max(match[0].length + 260, 220));
    return normalizeWhitespace(source.content.slice(start, end));
  }
  return null;
}

function buildPatterns(fieldKey: ReviewFieldKey, value: string): RegExp[] {
  const escaped = escapeRegex(value);
  switch (fieldKey) {
    case "courseName":
      return [new RegExp(escaped, "i")];
    case "degreeLevel":
      return [new RegExp(`\\b${escaped}\\b`, "i")];
    case "duration":
      return [new RegExp(`\\b${escaped.replace(/\s+/g, "\\s+")}\\b`, "i"), /\b(duration|length|study period)\b[\s\S]{0,80}?\b\d+(?:\.\d+)?\s*(year|month|week|semester|trimester)s?\b/i];
    case "studyMode":
      return [new RegExp(escaped.replace(/\s+/g, "\\s+"), "i"), /\b(on campus|online|face[- ]to[- ]face|blended|mixed|hybrid|in person)\b/i];
    case "courseLocation":
      return [new RegExp(escaped.replace(/\s+/g, "\\s+"), "i"), /\b(campus|location|locations)\b[\s\S]{0,120}/i];
    case "internationalFee":
      {
        const digits = value.replace(/[^\d]/g, "");
        if (!digits) return [];
        const commaFlexible = digits.length > 3
          ? digits.replace(/\B(?=(\d{3})+(?!\d))/g, ",?")
          : digits;
        return [
          new RegExp(`\\b(?:aud|a\\$|\\$)?\\s*${commaFlexible}\\b`, "i"),
          new RegExp(`\\b(?:international|tuition|fee|fees)\\b[\\s\\S]{0,120}?(?:aud|a\\$|\\$)?\\s*${commaFlexible}\\b`, "i"),
        ];
      }
    case "intakeMonths":
      return [/\b(intake|intakes|commence|start)\b[\s\S]{0,140}/i, new RegExp(`\\b(${MONTH_TOKEN_PATTERN})\\b`, "i")];
    case "ieltsOverall":
      if (/^\d+(?:\.\d+)?$/.test(value)) {
        const numeric = parseFloat(value);
        const numericPattern = Number.isInteger(numeric) ? `${numeric}(?:\\.0)?` : `${numeric}`;
        return [new RegExp(`\\bielts\\b[\\s\\S]{0,120}?\\b${numericPattern}\\b`, "i")];
      }
      return [/\bielts\b[\s\S]{0,120}?\b\d(?:\.\d)?\b/i];
    case "pteOverall":
      return [/\bpte\b[\s\S]{0,120}?\b\d{2}\b/i];
    case "toeflOverall":
      return [/\btoefl\b[\s\S]{0,120}?\b\d{2,3}\b/i];
    case "academicRequirement":
      return [/\b(entry|admission|academic|required|requirement|eligib)\b[\s\S]{0,220}/i];
  }
}

function validateField(fieldKey: ReviewFieldKey, normalizedValue: string | null, rawValue: string | null): "accepted" | "rejected" | "needs_review" {
  if (!normalizedValue || !rawValue) return "rejected";
  switch (fieldKey) {
    case "internationalFee": {
      const amount = parseFloat(rawValue.replace(/[^\d.]/g, ""));
      if (!Number.isFinite(amount) || amount < 1000 || amount > 200000) return "rejected";
      return /\b(aud|a\$|\$)\b/i.test(rawValue) ? "accepted" : "needs_review";
    }
    case "ieltsOverall": {
      const score = parseFloat(rawValue);
      if (!Number.isFinite(score) || score < 4 || score > 9) return "rejected";
      return "accepted";
    }
    case "pteOverall": {
      const score = parseFloat(rawValue);
      if (!Number.isFinite(score) || score < 30 || score > 90) return "rejected";
      return "accepted";
    }
    case "toeflOverall": {
      const score = parseFloat(rawValue);
      if (!Number.isFinite(score) || score < 30 || score > 120) return "rejected";
      return "accepted";
    }
    case "duration":
      return /\b(year|month|week|semester|trimester)\b/i.test(rawValue) ? "accepted" : "needs_review";
    case "intakeMonths":
      return monthsFoundInText(rawValue).length > 0 ? "accepted" : "needs_review";
    default:
      return rawValue.trim().length >= 2 ? "accepted" : "needs_review";
  }
}

function makeDerivedCandidate(fieldKey: ReviewFieldKey, rawValue: string | null): FieldCandidate | null {
  if (!rawValue) return null;
  const normalized = normalizeFieldValue(fieldKey, rawValue);
  const validationStatus = validateField(fieldKey, normalized, rawValue);
  const baseDecision = validationStatus === "accepted" ? "needs_review" : validationStatus;
  return {
    fieldKey,
    candidateValue: rawValue,
    normalizedValue: normalized,
    sourceUrl: null,
    pageType: "derived",
    extractionMethod: "derived",
    rawText: rawValue,
    snippet: rawValue,
    confidence: validationStatus === "accepted" ? 0.45 : 0.2,
    validationStatus,
    decisionStatus: baseDecision,
    decisionScore: validationStatus === "accepted" ? 0.45 : 0.2,
    selected: false,
  };
}

function extractSourceCandidates(fieldKey: ReviewFieldKey, source: ReviewSource): Array<{ value: string; snippet: string | null; confidence: number }> {
  const results: Array<{ value: string; snippet: string | null; confidence: number }> = [];
  const pageType = source.pageType;
  const feeSourceAllowed = pageType === "course_page" || pageType === "fee_page" || pageType === "fee_pdf" || pageType === "brochure_pdf";
  const courseDetailSourceAllowed = pageType === "course_page" || pageType === "listing_page" || pageType === "brochure_pdf";
  const pushValue = (value: string, snippet: string | null, confidence: number) => {
    const normalized = normalizeFieldValue(fieldKey, value);
    if (!normalized) return;
    if (results.some((entry) => normalizeFieldValue(fieldKey, entry.value) === normalized)) return;
    results.push({ value, snippet, confidence });
  };

  if (fieldKey === "internationalFee") {
    if (!feeSourceAllowed) return results;
    const matches = Array.from(source.content.matchAll(/\b(?:aud|a\$|\$)\s*([\d,]{4,})/gi)).slice(0, 4);
    for (const match of matches) {
      const snippet = findSnippet(source, [new RegExp(`${escapeRegex(match[0])}`, "i"), /\b(international|tuition|fee|fees)\b[\s\S]{0,120}/i]);
      pushValue(`AUD ${match[1].replace(/,/g, "")}`, snippet, sourceBaseConfidence(source) - 0.02);
    }
  } else if (fieldKey === "ieltsOverall") {
    const match = source.content.match(/\bielts\b[\s\S]{0,120}?\b(\d(?:\.\d)?)\b/i);
    if (match) pushValue(match[1], findSnippet(source, [/\bielts\b[\s\S]{0,120}?\b\d(?:\.\d)?\b/i]), sourceBaseConfidence(source));
  } else if (fieldKey === "pteOverall") {
    const match = source.content.match(/\bpte\b[\s\S]{0,120}?\b(\d{2})\b/i);
    if (match) pushValue(match[1], findSnippet(source, [/\bpte\b[\s\S]{0,120}?\b\d{2}\b/i]), sourceBaseConfidence(source));
  } else if (fieldKey === "toeflOverall") {
    const match = source.content.match(/\btoefl\b[\s\S]{0,120}?\b(\d{2,3})\b/i);
    if (match) pushValue(match[1], findSnippet(source, [/\btoefl\b[\s\S]{0,120}?\b\d{2,3}\b/i]), sourceBaseConfidence(source));
  } else if (fieldKey === "duration") {
    if (!courseDetailSourceAllowed) return results;
    const match = source.content.match(/\b(\d(?:\.\d+)?)\s*(year|month|week|semester|trimester)s?\b/i);
    if (match) pushValue(`${match[1]} ${match[2]}`, findSnippet(source, [/\b(duration|length|study period)\b[\s\S]{0,80}?\b\d+(?:\.\d+)?\s*(year|month|week|semester|trimester)s?\b/i]), sourceBaseConfidence(source) - 0.05);
  } else if (fieldKey === "intakeMonths") {
    if (!courseDetailSourceAllowed) return results;
    const intakeContext = source.content.match(/\b(intake|intakes|commence|commencing|start|starts|starting|start\s*date)\b[\s\S]{0,220}/i)?.[0] || null;
    if (intakeContext) {
      const months = monthsFoundInText(intakeContext);
      if (months.length > 0) pushValue(months.join(", "), normalizeWhitespace(intakeContext), sourceBaseConfidence(source) - 0.08);
    }
  } else if (fieldKey === "studyMode") {
    if (!courseDetailSourceAllowed) return results;
    const explicitMode =
      source.content.match(/\b(?:study|delivery|attendance)\s*mode\b[\s:.-]{0,20}(on campus|online only|online|face[- ]to[- ]face|blended|mixed|hybrid|in person)\b/i) ||
      source.content.match(/\b(on campus|online only|face[- ]to[- ]face|blended|mixed|hybrid|in person)\b[\s\S]{0,40}\b(?:study|delivery|attendance)\s*mode\b/i);
    if (explicitMode) {
      const valueMatch = explicitMode[1] || explicitMode[0].match(/\b(on campus|online only|online|face[- ]to[- ]face|blended|mixed|hybrid|in person)\b/i)?.[1];
      if (valueMatch) pushValue(valueMatch, normalizeWhitespace(explicitMode[0]), sourceBaseConfidence(source) - 0.04);
    }
  }

  return results;
}

function collectCandidatesForField(fieldKey: ReviewFieldKey, data: ReviewCourseData, sources: ReviewSource[]): FieldCandidate[] {
  const candidates: FieldCandidate[] = [];
  const rawValue = valueToString(fieldKey, data);
  if (rawValue) {
    for (const source of sources) {
      const snippet = findSnippet(source, buildPatterns(fieldKey, rawValue));
      if (!snippet) continue;
      const normalized = normalizeFieldValue(fieldKey, rawValue);
      const validationStatus = validateField(fieldKey, normalized, rawValue);
      const confidence = Math.min(0.99, sourceBaseConfidence(source) + 0.04);
      candidates.push({
        fieldKey,
        candidateValue: rawValue,
        normalizedValue: normalized,
        sourceUrl: source.url,
        pageType: source.pageType,
        extractionMethod: source.extractionMethod,
        rawText: snippet,
        snippet,
        confidence,
        validationStatus,
        decisionStatus: validationStatus === "accepted" ? "needs_review" : validationStatus,
        decisionScore: confidence,
        selected: false,
      });
    }
  }

  for (const source of sources) {
    for (const extracted of extractSourceCandidates(fieldKey, source)) {
      const normalized = normalizeFieldValue(fieldKey, extracted.value);
      const validationStatus = validateField(fieldKey, normalized, extracted.value);
      candidates.push({
        fieldKey,
        candidateValue: extracted.value,
        normalizedValue: normalized,
        sourceUrl: source.url,
        pageType: source.pageType,
        extractionMethod: source.extractionMethod,
        rawText: extracted.snippet,
        snippet: extracted.snippet,
        confidence: extracted.confidence,
        validationStatus,
        decisionStatus: validationStatus === "accepted" ? "needs_review" : validationStatus,
        decisionScore: extracted.confidence,
        selected: false,
      });
    }
  }

  if (candidates.length === 0) {
    const fallback = makeDerivedCandidate(fieldKey, rawValue);
    if (fallback) candidates.push(fallback);
  }

  return candidates.filter((candidate, index, all) =>
    index === all.findIndex((entry) =>
      entry.sourceUrl === candidate.sourceUrl &&
      entry.normalizedValue === candidate.normalizedValue &&
      entry.pageType === candidate.pageType,
    )
  );
}

function resolveField(fieldKey: ReviewFieldKey, candidates: FieldCandidate[]): { resolution: FieldResolution; candidates: FieldCandidate[]; conflicts: FieldConflict[] } {
  const valid = candidates.filter((candidate) => candidate.validationStatus !== "rejected");
  const sorted = [...valid].sort((a, b) => b.confidence - a.confidence);
  const conflicts: FieldConflict[] = [];

  if (sorted.length === 0) {
    return {
      resolution: { fieldKey, finalValue: null, status: "needs_review", decisionScore: 0, reason: "No trustworthy evidence" },
      candidates,
      conflicts,
    };
  }

  const winner = sorted[0];
  const conflicting = sorted.find((candidate) =>
    candidate.normalizedValue &&
    winner.normalizedValue &&
    candidate.normalizedValue !== winner.normalizedValue &&
    candidate.sourceUrl &&
    winner.sourceUrl &&
    candidate.sourceUrl !== winner.sourceUrl
  );
  if (conflicting && Math.abs(conflicting.confidence - winner.confidence) < 0.15) {
    conflicts.push({
      fieldKey,
      valueA: winner.candidateValue,
      valueB: conflicting.candidateValue,
      conflictType: "source_mismatch",
      reason: `Conflicting ${fieldKey} evidence across official sources`,
    });
  }

  let status: FieldResolution["status"] = "accepted";
  let reason: string | null = null;
  if (conflicts.length > 0) {
    status = "needs_review";
    reason = conflicts[0].reason;
  } else if (winner.confidence < 0.72 || winner.pageType === "derived") {
    status = "needs_review";
    reason = "Evidence is too weak for auto-publish";
  }

  for (const candidate of candidates) {
    candidate.selected = candidate === winner;
    candidate.decisionStatus = candidate === winner ? status : (candidate.validationStatus === "rejected" ? "rejected" : "needs_review");
    candidate.decisionScore = candidate === winner ? winner.confidence : candidate.confidence;
  }

  return {
    resolution: {
      fieldKey,
      finalValue: winner.candidateValue,
      status,
      decisionScore: winner.confidence,
      reason,
    },
    candidates,
    conflicts,
  };
}

function assessEligibility(data: ReviewCourseData, sources: ReviewSource[]): EligibilityAssessment {
  const combined = normalizeWhitespace(sources.map((source) => source.content).join(" "));
  const evidence = findSnippet(
    { url: data.courseWebsite || null || "", pageType: "course_page", extractionMethod: "cheerio", content: combined } as ReviewSource,
    [
      /\b(international students|cricos|student visa|available to international students)\b[\s\S]{0,140}/i,
      /\b(domestic only|australian citizens only|permanent residents only|not available to international students)\b[\s\S]{0,140}/i,
      /\b(on campus|face[- ]to[- ]face|blended|hybrid|mixed|online only|distance education)\b[\s\S]{0,140}/i,
    ],
  );
  if (data.domesticOnly || /\b(domestic only|not available to international students|australian citizens only|permanent residents only)\b/i.test(combined)) {
    return {
      studentMarket: "domestic_only",
      deliveryMode: data.onlineOnly ? "online_only" : /on campus|face[- ]to[- ]face|campus/i.test(combined) || !!data.courseLocation ? "on_campus" : "unknown",
      internationalEligible: false,
      onCampusAvailable: !data.onlineOnly,
      eligibilityStatus: "rejected",
      reason: "Domestic-only evidence found",
      confidence: 0.96,
      evidenceText: evidence,
    };
  }
  if (data.onlineOnly || /\b(online only|fully online|distance education only|external only)\b/i.test(combined)) {
    return {
      studentMarket: /\b(international students|cricos|student visa)\b/i.test(combined) ? "international" : "unknown",
      deliveryMode: "online_only",
      internationalEligible: !data.domesticOnly,
      onCampusAvailable: false,
      eligibilityStatus: "rejected",
      reason: "Online-only evidence found",
      confidence: 0.94,
      evidenceText: evidence,
    };
  }
  if ((data.studyMode || "").trim().toLowerCase() === "online" && !data.courseLocation) {
    return {
      studentMarket: /\b(international students|cricos|student visa)\b/i.test(combined) ? "international" : "unknown",
      deliveryMode: "online_only",
      internationalEligible: !data.domesticOnly,
      onCampusAvailable: false,
      eligibilityStatus: "rejected",
      reason: "Study mode is online with no physical campus evidence",
      confidence: 0.93,
      evidenceText: evidence,
    };
  }

  const internationalSignal = data.internationalFee != null || /\b(international students|cricos|student visa|international tuition)\b/i.test(combined);
  const campusSignal = !!data.courseLocation || /\b(on campus|face[- ]to[- ]face|in person|campus)\b/i.test((data.studyMode || "").toLowerCase()) || /\b(on campus|face[- ]to[- ]face|in person|campus)\b/i.test(combined);
  const mixedSignal = /\b(blended|hybrid|mixed)\b/i.test((data.studyMode || "").toLowerCase()) || /\b(blended|hybrid|mixed)\b/i.test(combined);

  if (internationalSignal && (campusSignal || mixedSignal)) {
    return {
      studentMarket: /\b(domestic|local)\b/i.test(combined) ? "both" : "international",
      deliveryMode: mixedSignal ? "mixed" : "on_campus",
      internationalEligible: true,
      onCampusAvailable: true,
      eligibilityStatus: "eligible",
      reason: "International and on-campus evidence found",
      confidence: mixedSignal ? 0.82 : 0.91,
      evidenceText: evidence,
    };
  }

  return {
    studentMarket: internationalSignal ? "international" : "unknown",
    deliveryMode: campusSignal ? "on_campus" : "unknown",
    internationalEligible: internationalSignal,
    onCampusAvailable: campusSignal,
    eligibilityStatus: "needs_review",
    reason: "Eligibility evidence is incomplete or unclear",
    confidence: 0.45,
    evidenceText: evidence,
  };
}

export function buildCourseReviewSnapshot(data: ReviewCourseData, sources: ReviewSource[]): CourseReviewSnapshot {
  const candidates: FieldCandidate[] = [];
  const resolutions: FieldResolution[] = [];
  const conflicts: FieldConflict[] = [];

  for (const fieldKey of REVIEW_FIELD_KEYS) {
    const fieldCandidates = collectCandidatesForField(fieldKey, data, sources);
    const resolved = resolveField(fieldKey, fieldCandidates);
    candidates.push(...resolved.candidates);
    resolutions.push(resolved.resolution);
    conflicts.push(...resolved.conflicts);
  }

  const eligibility = assessEligibility(data, sources);
  const requiredFields: ReviewFieldKey[] = ["courseName", "duration", "internationalFee", "intakeMonths", "ieltsOverall"];
  const requiredFailures = resolutions.filter((resolution) => requiredFields.includes(resolution.fieldKey) && resolution.status !== "accepted");
  const autoPublishStatus =
    eligibility.eligibilityStatus !== "eligible" ? (eligibility.eligibilityStatus === "rejected" ? "rejected" : "pending_review") :
    conflicts.length > 0 || requiredFailures.length > 0 ? "pending_review" :
    "approved";
  const acceptedScores = resolutions.filter((resolution) => resolution.status === "accepted").map((resolution) => resolution.decisionScore);
  const decisionScore = acceptedScores.length > 0 ? acceptedScores.reduce((sum, value) => sum + value, 0) / acceptedScores.length : 0;

  return {
    candidates,
    resolutions,
    conflicts,
    eligibility,
    autoPublishStatus,
    decisionScore,
  };
}
