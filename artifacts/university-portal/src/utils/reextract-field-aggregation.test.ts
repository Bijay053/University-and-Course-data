import { describe, expect, it } from "vitest";
import { mergeReextractFieldResults } from "./reextract-field-aggregation";

function createAggregation() {
  return {
    valueUpdatedFields: new Set<string>(),
    provenanceOnlyFields: new Set<string>(),
  };
}

describe("mergeReextractFieldResults", () => {
  it("separates value updates from source-only refreshes in one response", () => {
    const aggregation = createAggregation();

    mergeReextractFieldResults(aggregation, [{
      updated_fields: ["tuition_fee", "ielts_overall"],
      refreshed_evidence_fields: ["ielts_overall", "course_duration"],
    }]);

    expect([...aggregation.valueUpdatedFields].sort()).toEqual([
      "ielts_overall",
      "tuition_fee",
    ]);
    expect([...aggregation.provenanceOnlyFields]).toEqual(["course_duration"]);
  });

  it("merges field lists across multiple batches with value updates taking precedence", () => {
    const aggregation = createAggregation();

    mergeReextractFieldResults(aggregation, [{
      updated_fields: ["tuition_fee"],
      refreshed_evidence_fields: ["course_duration", "ielts_overall"],
    }]);
    mergeReextractFieldResults(aggregation, [{
      updated_fields: ["ielts_overall"],
      refreshed_evidence_fields: ["study_mode", "tuition_fee"],
    }]);

    expect([...aggregation.valueUpdatedFields].sort()).toEqual([
      "ielts_overall",
      "tuition_fee",
    ]);
    expect([...aggregation.provenanceOnlyFields].sort()).toEqual([
      "course_duration",
      "study_mode",
    ]);
  });
});