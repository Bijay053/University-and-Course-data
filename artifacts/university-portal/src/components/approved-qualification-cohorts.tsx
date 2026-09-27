import { useRef, useState } from "react";
import { listApprovedQualificationCohorts, type ApprovedQualificationCohort } from "@workspace/api-client-react";
import { Can } from "@/components/can";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { QualificationRefresh } from "@/components/qualification-refresh";

export function ApprovedQualificationCohorts({ universities, onApplied }: {
  universities: { id: number; name: string }[];
  onApplied: (jobId: string) => Promise<unknown>;
}) {
  const [university, setUniversity] = useState("");
  const [cohorts, setCohorts] = useState<ApprovedQualificationCohort[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const request = useRef(0);

  async function discover() {
    const revision = ++request.current;
    setBusy(true);
    setError("");
    setCohorts(null);
    try {
      const result = await listApprovedQualificationCohorts(Number(university), { cache: "no-store", credentials: "include" });
      if (revision === request.current) setCohorts(result.cohorts);
    } catch (e) {
      if (revision === request.current) {
        const detail = e as { data?: { detail?: string }; message?: string };
        setError(detail.data?.detail || detail.message || "Could not find approved cohorts. Retry discovery.");
      }
    } finally {
      if (revision === request.current) setBusy(false);
    }
  }

  return <Can permission="staged.approve">
    <Card>
      <CardHeader><CardTitle className="text-base">Refresh an approved Legal Technology cohort</CardTitle></CardHeader>
      <CardContent className="space-y-3">
        <p className="text-sm text-muted-foreground">Already approved courses are not in pending Review. Find their original award cohort here, preview current official fees and campuses, then return it to Review without changing the published catalogue.</p>
        <div className="flex flex-wrap items-end gap-2">
          <label className="text-sm">University for approved cohort
            <select aria-label="University for approved cohort" value={university} className="block rounded border bg-background p-2"
              onChange={(e) => { ++request.current; setUniversity(e.target.value); setCohorts(null); setError(""); setBusy(false); }}>
              <option value="">Choose university</option>
              {universities.map(u => <option key={u.id} value={u.id}>{u.name}</option>)}
            </select>
          </label>
          <Button variant="outline" disabled={!university || busy} onClick={discover}>
            {busy ? "Finding approved cohorts…" : "Find approved award cohorts"}
          </Button>
        </div>
        {error && <p role="alert" className="text-destructive">{error}</p>}
        {cohorts?.length === 0 && <p role="status">No complete approved Legal Technology cohorts found. Pending cohorts remain in Review.</p>}
        {cohorts?.map(cohort => <section key={`${cohort.jobId}-${cohort.splitFromId}`} className="rounded border p-3">
          <h3 className="font-medium">{cohort.label} — {cohort.latest ? "Latest approved cohort" : "Earlier approved cohort"}</h3>
          <p className="text-sm">Fee years: {cohort.feeYears.join(", ")} · {cohort.courseIds.length} staged campus rows</p>
          <p className="text-xs text-muted-foreground break-all">Source job: {cohort.jobId} · Original split #{cohort.splitFromId} · {cohort.createdAt}</p>
          <QualificationRefresh courseId={cohort.rowId} metadata={{ ulaw_qualification_scope: true }} onApplied={async () => {
            // Exact source job, never the latest-job fallback.
            const loaded = await onApplied(cohort.jobId);
            if (loaded === false) throw new Error("Staging was refreshed, but Review could not load. Retry opening this source job from history.");
            setCohorts(current => current?.filter(c => c.jobId !== cohort.jobId || c.splitFromId !== cohort.splitFromId) ?? null);
          }} />
        </section>)}
      </CardContent>
    </Card>
  </Can>;
}