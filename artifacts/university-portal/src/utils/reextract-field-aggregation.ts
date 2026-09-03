export type ReextractCourseResult = {
  updated_fields?: string[] | null;
  refreshed_evidence_fields?: string[] | null;
};

export type ReextractFieldAggregation = {
  valueUpdatedFields: Set<string>;
  provenanceOnlyFields: Set<string>;
};

export function mergeReextractFieldResults(
  aggregation: ReextractFieldAggregation,
  results: ReextractCourseResult[],
): void {
  for (const result of results) {
    const updatedFields = new Set(result.updated_fields ?? []);

    for (const field of updatedFields) {
      aggregation.valueUpdatedFields.add(field);
      aggregation.provenanceOnlyFields.delete(field);
    }

    for (const field of result.refreshed_evidence_fields ?? []) {
      if (
        !updatedFields.has(field)
        && !aggregation.valueUpdatedFields.has(field)
      ) {
        aggregation.provenanceOnlyFields.add(field);
      }
    }
  }
}