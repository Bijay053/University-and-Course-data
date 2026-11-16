export type FixFieldResult = {
  ok?: boolean;
  attempted?: boolean;
  outcome?: string;
  target_fields?: string[];
  updated_fields?: string[] | null;
  refreshed_evidence_fields?: string[] | null;
};

const metadata = new Set([
  "scrape_warnings", "requirement_status", "completeness", "completeness_score",
  "auto_publish_status", "decision_score", "extraction_method",
]);

const companions: Record<string, string[]> = {
  international_fee: ["currency", "fee_term", "fee_year"],
  duration: ["duration_term"],
  intake_months: ["intake_days"],
  academic_level: ["academic_score", "score_type", "academic_country"],
  academic_score: ["academic_level", "score_type", "academic_country", "other_requirement"],
  other_requirement: ["academic_level", "academic_score", "score_type", "academic_country"],
};

export function matchesFixScope(field: string, scope: string[]) {
  if (!scope.length || scope.includes(field)
    || scope.some((target) => companions[target]?.includes(field))) return true;
  const family = field.split("_", 1)[0];
  return ["ielts", "pte", "toefl", "cambridge", "det"].includes(family)
    && (scope.includes("english_requirements") || scope.includes(`${family}_overall`));
}

// Legacy jobs retain their original issue-resolution counters. Derive field
// changes from their saved results as well, so reopening them needs no re-run.
export function fixFieldChanges(
  results: FixFieldResult[],
  requested: string[] = [],
  matches = matchesFixScope,
) {
  const counts = { valueUpdated: 0, valueUnchanged: 0, evidenceRefreshedOnly: 0 };
  for (const row of results) {
    if (row.ok === false || row.attempted === false
      || ["failed", "skipped", "already_resolved"].includes(row.outcome ?? "")) continue;
    const scope = requested.length ? requested : row.target_fields ?? [];
    const relevant = (fields?: string[] | null) =>
      (fields ?? []).some((field) => !metadata.has(field) && matches(field, scope));
    if (relevant(row.updated_fields)) counts.valueUpdated++;
    else {
      counts.valueUnchanged++;
      if (relevant(row.refreshed_evidence_fields)) counts.evidenceRefreshedOnly++;
    }
  }
  return counts;
}
