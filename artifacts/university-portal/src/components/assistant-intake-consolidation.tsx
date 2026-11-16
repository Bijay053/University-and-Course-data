import { safeSourceUrl } from "./assistant-safe-url";
import { IntakeFeeSchedules, type IntakeFeeSchedule } from "./intake-fee-schedules";

export type IntakeConsolidation = {
  courseName: string;
  canonicalUrl: string;
  entryYear: number;
  keepId: number;
  excludeIds: number[];
  intakes: string[];
  feeSchedules?: IntakeFeeSchedule[];
  conflicts: Record<string, { id: number; value: unknown; expectedIntake?: string }[]>;
  rows: { id: number; name: string; url: string; intakes: string[] | null;
    fee: number | null; currency: string | null; feeYear: number | null }[];
};

export function AssistantIntakeConsolidation({ preview }: { preview: IntakeConsolidation }) {
  const conflicts = Object.entries(preview.conflicts);
  return (
    <section aria-label="Intake consolidation preview" className="my-3 rounded-lg border border-slate-200 bg-white p-3 text-xs">
      <h4 className="font-semibold">{preview.courseName}</h4>
      <p className="mt-1">Proposed intakes: {preview.intakes.join(", ")} {preview.entryYear}</p>
      <p className="mt-1 text-slate-600">
        Keep row {preview.keepId}; softly exclude {preview.excludeIds.join(", ")}.
        Original records and evidence remain available. Nothing is published.
      </p>
      <div className="mt-2 overflow-x-auto">
        <table className="w-full text-left">
          <thead><tr><th>Existing row</th><th>Stored intakes</th><th>Fee and year</th><th>Proposed action</th></tr></thead>
          <tbody>{preview.rows.map(row => {
            const href = safeSourceUrl(row.url);
            return <tr key={row.id} className="border-t align-top">
              <td className="py-2 pr-3">{row.id}: {href
                ? <a href={href} target="_blank" rel="noopener noreferrer nofollow" className="underline">{row.name}</a>
                : row.name}</td>
              <td className="py-2 pr-3">{row.intakes?.join(", ") || "Unverified"}</td>
              <td className="py-2 pr-3">{row.fee == null ? "Unverified" : `${row.currency ?? ""} ${row.fee.toLocaleString()}`} · {row.feeYear ?? "year unverified"}</td>
              <td className="py-2">{row.id === preview.keepId ? "Keep and rename" : "Soft exclude after merge"}</td>
            </tr>;
          })}</tbody>
        </table>
      </div>
      {preview.feeSchedules && <IntakeFeeSchedules schedules={preview.feeSchedules} />}
      {conflicts.length > 0 && <div role="status" className="mt-2 rounded bg-amber-50 p-2 text-amber-950">
        <p className="font-semibold">Merge blocked — conflicting values need review</p>
        <ul className="list-disc pl-4">{conflicts.map(([field, values]) =>
          <li key={field}>{field}: {values.map(value =>
            `row ${value.id}: ${typeof value.value === "string" ? value.value : JSON.stringify(value.value)}${value.expectedIntake ? ` (intake offering: ${value.expectedIntake})` : ""}`
          ).join("; ")}</li>)}</ul>
      </div>}
    </section>
  );
}
