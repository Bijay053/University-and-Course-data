import type { CourseReviewSnapshot, FieldCandidate, ReviewFieldKey } from "./review-engine.js";

export interface FeedbackRule {
  fieldKey?: string | null;
  issueType: string;
  reason: string;
  preferredValue?: string | null;
}

/** Aggregated from active `scrape_feedback` rows for a university — drives extraction strategy on reruns */
export type ScrapeFeedbackHints = {
  /** User reported domestic/wrong fee — skip generic course-page fee lines and single-amount guesses */
  strictInternationalFee: boolean;
  /** User asked to use PDF / international fee schedule — consult shared fee PDF before trusting course page */
  preferFeePdfFirst: boolean;
  /** When reading `uniPages.feePage`, bias toward international section */
  forceInternationalFeePageContext: boolean;
  activeCount: number;
  issueTypeSummary: string[];
};

const FEE_ISSUE_TYPES = new Set(["domestic_fee_picked", "international_fee_required", "wrong_fee"]);

/**
 * Build runtime hints from DB rows (or tests). Called once per scrape batch.
 */
export function buildScrapeFeedbackHints(rows: readonly { issueType: string; reason: string }[]): ScrapeFeedbackHints {
  let strictInternationalFee = false;
  let preferFeePdfFirst = false;
  const types: string[] = [];
  for (const r of rows) {
    types.push(r.issueType);
    const t = `${r.reason} ${r.issueType}`.toLowerCase();
    if (FEE_ISSUE_TYPES.has(r.issueType)) strictInternationalFee = true;
    if (/\bpdf\b|fee\s*schedule|international\s+fee|fee\s+page|only.*(?:pdf|fee\s+page)/.test(t)) {
      preferFeePdfFirst = true;
    }
  }
  return {
    strictInternationalFee,
    preferFeePdfFirst,
    forceInternationalFeePageContext: strictInternationalFee,
    activeCount: rows.length,
    issueTypeSummary: [...new Set(types)],
  };
}

export function inferFeedbackIssue(reason: string, fieldKey?: string | null): string {
  const text = reason.toLowerCase();
  const field = (fieldKey || "").toLowerCase();
  if (field.includes("fee") || /\bfee\b|\btuition\b/.test(text)) {
    if (/\bdomestic\b/.test(text)) return "domestic_fee_picked";
    if (/\binternational\b/.test(text)) return "international_fee_required";
    return "wrong_fee";
  }
  if (field.includes("location") || /\bcampus\b|\blocation\b/.test(text)) return "wrong_location";
  if (field.includes("course") || /\bwrong page\b|\bnot a course\b|\blanding page\b/.test(text)) return "wrong_page";
  if (/\bdomestic only\b|\bnot available to international\b/.test(text)) return "domestic_only_missed";
  if (/\bonline only\b|\bfully online\b/.test(text)) return "online_only_missed";
  if (field.includes("ielts") || field.includes("pte") || field.includes("toefl") || /\bielts\b|\bpte\b|\btoefl\b|\brequirement\b/.test(text)) {
    return "wrong_requirement";
  }
  return "generic";
}

function demoteCandidates(candidates: FieldCandidate[], trustPageTypes: string[]) {
  for (const candidate of candidates) {
    if (!trustPageTypes.includes(candidate.pageType)) {
      candidate.confidence = Math.min(candidate.confidence, 0.35);
      if (candidate.decisionStatus === "accepted") candidate.decisionStatus = "needs_review";
      if (candidate.selected) candidate.selected = false;
      candidate.decisionScore = Math.min(candidate.decisionScore, 0.35);
    }
  }
}

function markFieldNeedsReview(snapshot: CourseReviewSnapshot, fieldKey: ReviewFieldKey, reason: string, trustPageTypes?: string[]) {
  const candidates = snapshot.candidates.filter((candidate) => candidate.fieldKey === fieldKey);
  if (trustPageTypes?.length) demoteCandidates(candidates, trustPageTypes);
  const resolution = snapshot.resolutions.find((item) => item.fieldKey === fieldKey);
  if (resolution) {
    resolution.status = "needs_review";
    resolution.reason = resolution.reason ? `${resolution.reason}; ${reason}` : reason;
    resolution.decisionScore = Math.min(resolution.decisionScore, 0.4);
  }
}

export function applyFeedbackRules(snapshot: CourseReviewSnapshot, feedbackRules: FeedbackRule[]) {
  for (const rule of feedbackRules) {
    switch (rule.issueType) {
      case "domestic_fee_picked":
      case "wrong_fee":
      case "international_fee_required":
        markFieldNeedsReview(snapshot, "internationalFee", rule.reason, ["fee_page", "fee_pdf", "brochure_pdf"]);
        break;
      case "wrong_location":
        markFieldNeedsReview(snapshot, "courseLocation", rule.reason, ["course_page"]);
        break;
      case "wrong_requirement":
        markFieldNeedsReview(snapshot, "ieltsOverall", rule.reason, ["english_page", "requirements_page", "brochure_pdf"]);
        markFieldNeedsReview(snapshot, "pteOverall", rule.reason, ["english_page", "requirements_page", "brochure_pdf"]);
        markFieldNeedsReview(snapshot, "toeflOverall", rule.reason, ["english_page", "requirements_page", "brochure_pdf"]);
        break;
      case "wrong_page":
        markFieldNeedsReview(snapshot, "courseName", rule.reason, ["course_page"]);
        snapshot.autoPublishStatus = "pending_review";
        break;
      case "domestic_only_missed":
        snapshot.eligibility.eligibilityStatus = "rejected";
        snapshot.eligibility.reason = rule.reason;
        snapshot.eligibility.internationalEligible = false;
        snapshot.autoPublishStatus = "rejected";
        break;
      case "online_only_missed":
        snapshot.eligibility.eligibilityStatus = "rejected";
        snapshot.eligibility.reason = rule.reason;
        snapshot.eligibility.onCampusAvailable = false;
        snapshot.autoPublishStatus = "rejected";
        break;
      default:
        if (rule.fieldKey === "internationalFee") {
          markFieldNeedsReview(snapshot, "internationalFee", rule.reason, ["fee_page", "fee_pdf", "brochure_pdf"]);
        }
        break;
    }
  }

  if (snapshot.eligibility.eligibilityStatus !== "eligible" && snapshot.autoPublishStatus === "approved") {
    snapshot.autoPublishStatus = snapshot.eligibility.eligibilityStatus === "rejected" ? "rejected" : "pending_review";
  }
  if (snapshot.resolutions.some((item) => item.status !== "accepted") && snapshot.autoPublishStatus === "approved") {
    snapshot.autoPublishStatus = "pending_review";
  }
}
