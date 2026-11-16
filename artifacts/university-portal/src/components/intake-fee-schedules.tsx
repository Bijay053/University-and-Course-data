import { safeSourceUrl } from "./assistant-safe-url";

export type IntakeFeeSchedule = {
  feeYear: number; amount: number; currency: string; basis: string;
  entryYear: number; intakes: string[]; sourceRowIds: number[]; sourceUrls: string[];
  applicabilityNeedsReview: boolean;
};
export type IntakeFeeScheduleSnapshot = {
  schedules: IntakeFeeSchedule[]; snapshotMatchesCurrent: boolean;
  verification: string; publicationHold: boolean;
};

export function IntakeFeeSchedules({ schedules, snapshotMatchesCurrent = true }: {
  schedules: IntakeFeeSchedule[]; snapshotMatchesCurrent?: boolean;
}) {
  return <section aria-label="Intake-specific tuition schedules" className="my-2 whitespace-normal rounded border border-amber-200 bg-amber-50 p-2 text-left text-xs">
    <p className="font-semibold">Separate fee years retained</p>
    <p>Existing-record snapshots, not newly verified official evidence.</p>
    {!snapshotMatchesCurrent && <p role="alert" className="font-semibold text-red-800">Current course values differ from this saved schedule. Review required.</p>}
    <ul className="mt-2 space-y-2">
      {schedules.map((s, index) => <li key={`${s.feeYear}-${index}`}>
        <p><strong>Fee year {s.feeYear}:</strong> {s.currency} {s.amount.toLocaleString()} / {s.basis}</p>
        <p>Proposed intake applicability: {s.intakes.join(", ")} {s.entryYear}</p>
        {s.applicabilityNeedsReview && <p className="font-semibold">Intake mismatch needs correction before merging.</p>}
        <p>Original rows: {s.sourceRowIds.join(", ")}</p>
        <div className="flex flex-wrap gap-2">{s.sourceUrls.map((url, i) => {
          const safe = safeSourceUrl(url);
          return safe ? <a key={url} href={safe} className="underline" target="_blank"
            rel="noopener noreferrer nofollow">Source {i + 1}</a> : null;
        })}</div>
      </li>)}
    </ul>
    <p className="mt-2 font-medium">Publication held: do not replace these schedules with one fee year.</p>
  </section>;
}
