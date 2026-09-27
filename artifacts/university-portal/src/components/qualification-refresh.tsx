import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  usePreviewQualificationRefresh, useApplyQualificationRefresh,
  type QualificationRefreshPreview, type QualificationRefreshFee,
} from "@workspace/api-client-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from "@/components/ui/dialog";
import { Can } from "@/components/can";

function fee(value: QualificationRefreshFee | null) {
  return value ? `GBP ${value.amount?.toLocaleString() ?? "varies"} · ${value.year ?? "unknown year"} · ${value.term ?? "unknown term"}` : "Not staged";
}

function errorMessage(_error: unknown) {
  return "The current official cohort could not be previewed or applied. Retry preview or review staged evidence; no approval has been performed.";
}

type Props = {
  courseId: number; metadata: unknown; onApplied: () => Promise<unknown>;
};

export function QualificationRefresh(props: Props) {
  if (!props.metadata || typeof props.metadata !== "object" || !("ulaw_qualification_scope" in props.metadata)) return null;
  return <QualificationRefreshDialog {...props} />;
}

function QualificationRefreshDialog({ courseId, onApplied }: Props) {
  const [open, setOpen] = useState(false);
  const [preview, setPreview] = useState<QualificationRefreshPreview | null>(null);
  const [error, setError] = useState("");
  const [done, setDone] = useState("");
  const previewMutation = usePreviewQualificationRefresh();
  const applyMutation = useApplyQualificationRefresh();
  const queryClient = useQueryClient();
  const busy = previewMutation.isPending || applyMutation.isPending;

  async function loadPreview() {
    setOpen(true);
    setError("");
    setDone("");
    setPreview(null);
    try {
      setPreview(await previewMutation.mutateAsync({ courseId }));
    } catch (e) { setError(errorMessage(e)); }
  }

  async function apply() {
    if (!preview) return;
    setError("");
    try {
      const result = await applyMutation.mutateAsync({ courseId, data: { token: preview.token } });
      setDone(`${result.courseIds.length} staged campus rows refreshed. Nothing was published. Review and approve through the usual approval action.`);
      setPreview(null);
      await queryClient.invalidateQueries();
      await onApplied();
    } catch (e) { setError(errorMessage(e)); }
  }

  return <Can permission="staged.approve">
    <Button type="button" variant="outline" size="sm" className="mt-2" onClick={loadPreview}>
      Preview current award cohort
    </Button>
    <Dialog open={open} onOpenChange={(value) => { if (!busy) { setOpen(value); if (!value) setPreview(null); } }}>
      <DialogContent className="max-w-3xl max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Refresh Legal Technology award cohort</DialogTitle>
          <DialogDescription>Recheck official fees, award-specific dates and physical campuses for both awards. Existing IDs and original evidence are retained. Apply changes staging only; it does not publish or remove campuses.</DialogDescription>
        </DialogHeader>
        {busy && <p role="status">Verifying the official source…</p>}
        {error && <p role="alert" className="text-destructive">{error}</p>}
        {done && <p role="status">{done}</p>}
        {preview && <>
          <a href={preview.sourceUrl} target="_blank" rel="noreferrer" className="underline text-sm">Official award source</a>
          <p className="text-sm">{preview.message}</p>
          <div className="space-y-3">
            {preview.changes.map((change) => <section key={`${change.award}-${change.campus}`} className="rounded border p-3 text-sm">
              <h3 className="font-semibold">{change.award} — {change.campus} · {change.kind === "new" ? "New campus" : "Retained campus"}{change.stagedId ? ` · row #${change.stagedId}` : ""}</h3>
              <p>Before: {fee(change.old)}</p>
              <p>After: {fee(change.new)}</p>
              <p>Current starts: {change.intakes.filter(i => i.locations.includes(change.campus)).map(i => `${i.intake} (${i.study_load})`).join(", ")}</p>
            </section>)}
          </div>
        </>}
        <DialogFooter>
          <Button variant="outline" disabled={busy} onClick={() => setOpen(false)}>Close</Button>
          {!done && <Button variant="outline" disabled={busy} onClick={loadPreview}>Refresh preview</Button>}
          {preview && <Button disabled={busy} onClick={apply}>Apply to staging only</Button>}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </Can>;
}