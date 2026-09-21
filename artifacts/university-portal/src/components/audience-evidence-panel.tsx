import { ExternalLink } from "lucide-react";

export type AudienceEvidenceRow = {
  audience?: string;
  label?: string;
  value?: string;
  container?: string;
  source_url?: string | null;
  source_official?: boolean;
  intake_months?: number[];
  intake_year?: number | null;
};

export type AudienceEvidence = {
  status?: string;
  evidence?: AudienceEvidenceRow[];
  issues?: string[];
  same_panel?: boolean;
  linked_official?: boolean;
};

export type AudienceProposal = {
  status?: string;
  reason?: string;
  proposals?: Array<{
    intake_months?: number[];
    source_urls?: string[];
    english?: { central_page?: string };
  }>;
};

const MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function parsedValues(row: AudienceEvidenceRow) {
  const months = (row.intake_months ?? []).map(month => MONTHS[month] ?? String(month));
  if (row.intake_year) months.push(String(row.intake_year));
  return months.length ? months.join(", ") : "No parsed intake values";
}

export function AudienceEvidencePanel({
  evidence,
  proposal,
  compact = false,
}: {
  evidence: AudienceEvidence;
  proposal?: AudienceProposal;
  compact?: boolean;
}) {
  const rows = evidence.evidence ?? [];
  const international = rows.filter(row => row.audience === "international");
  const stopReasons = [
    ...(evidence.issues ?? []),
    ...(proposal?.status === "needs_review" && proposal.reason ? [proposal.reason] : []),
  ].filter((value, index, all) => all.indexOf(value) === index);

  return (
    <section aria-label="Audience and linked-source evidence" className="rounded border border-sky-200 bg-white/80 p-2 text-[9px] text-gray-700">
      <div className="flex flex-wrap items-center gap-2">
        <strong className="text-sky-900">Audience selector evidence</strong>
        <span>Panel: {rows[0]?.container ?? "not identified"}</span>
        <span>{evidence.same_panel ? "same panel confirmed" : "same panel not confirmed"}</span>
        <span>{evidence.linked_official ? "official links confirmed" : "official link not confirmed"}</span>
      </div>

      {rows.length > 0 ? (
        <div className={`mt-1.5 grid gap-1 ${compact ? "" : "sm:grid-cols-2"}`}>
          {rows.map((row, index) => {
            const selected = row.audience === "international";
            return (
              <div key={`${row.container}-${row.value}-${index}`} className={`rounded border px-2 py-1.5 ${selected ? "border-indigo-200 bg-indigo-50" : "border-gray-200 bg-gray-50"}`}>
                <div className="flex items-center justify-between gap-2">
                  <strong className="capitalize text-gray-800">{row.audience ?? "Unknown audience"}</strong>
                  {selected && <span className="rounded-full bg-indigo-100 px-1.5 py-0.5 font-semibold text-indigo-800">Selected international option</span>}
                </div>
                <p className="mt-0.5"><strong>Option:</strong> {row.label || row.value || "—"}</p>
                {row.label && row.value && row.label !== row.value && <p><strong>Stored value:</strong> {row.value}</p>}
                <p><strong>Parsed values:</strong> {parsedValues(row)}</p>
                {row.source_url && (
                  <a href={row.source_url} target="_blank" rel="noreferrer" className="mt-0.5 inline-flex items-center gap-0.5 font-medium text-blue-700 underline">
                    Linked official English source <ExternalLink className="h-2.5 w-2.5" />
                  </a>
                )}
              </div>
            );
          })}
        </div>
      ) : (
        <p className="mt-1 text-gray-500">No typed domestic/international selector rows were retained.</p>
      )}

      {international.length === 0 && rows.length > 0 && (
        <p className="mt-1 text-amber-800"><strong>International option:</strong> none retained.</p>
      )}
      {stopReasons.length > 0 && (
        <div className="mt-1.5 rounded border border-amber-200 bg-amber-50 px-2 py-1 text-amber-900">
          <strong>Why this needs review:</strong> {stopReasons.join("; ")}
        </div>
      )}
    </section>
  );
}