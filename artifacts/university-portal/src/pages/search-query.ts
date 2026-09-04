export type AcademicCredentialFilters = {
  qualification: string;
  scheme: string;
  outOf: string;
  gradingScore: string;
};

export function appendAcademicCredentialParams(
  params: URLSearchParams,
  filters: AcademicCredentialFilters,
) {
  if (filters.qualification) {
    params.set("highest_qualification", filters.qualification);
  }
  if (filters.scheme) params.set("grading_scheme", filters.scheme);
  if (filters.outOf) params.set("grading_out_of", filters.outOf);
  if (filters.gradingScore) params.set("grading_score", filters.gradingScore);
}