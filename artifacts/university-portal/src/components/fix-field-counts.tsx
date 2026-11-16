import { fixFieldChanges, type FixFieldResult } from "@/utils/fix-field-changes";

export function FixFieldCounts({ results, requested = [] }: {
  results: FixFieldResult[];
  requested?: string[];
}) {
  const counts = fixFieldChanges(results, requested);
  return <>
    <span className="text-green-700 mr-3">{counts.valueUpdated} courses with field values corrected</span>
    <span className="mr-3">{counts.valueUnchanged} with field values unchanged</span>
    <span className="mr-3">{counts.evidenceRefreshedOnly} of these had evidence refreshed</span>
  </>;
}
