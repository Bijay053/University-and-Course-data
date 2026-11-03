import { useEffect, useRef, useState } from "react";
import { CheckCircle2, Flag, Loader2 } from "lucide-react";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Input } from "@/components/ui/input";
import {
  REPORT_FIELDS, REPORT_KINDS, type ReportField, type ReportKind,
  useSubmitCourseErrorReport, validateReport,
} from "@/lib/course-error-reports";

type Props = {
  courseId?: number; courseName: string; universityName: string; className?: string;
  insight?: { universityId: number; id: number; country: string };
};

const empty = { kind: "incorrect" as ReportKind, field: "fees" as ReportField, message: "", source_url: "", website: "" };

export function ReportCourseErrorForm({ courseId, courseName, universityName, insight, onDone }: Omit<Props, "className"> & { onDone?: () => void }) {
  const [values, setValues] = useState(empty);
  const [errors, setErrors] = useState<ReturnType<typeof validateReport>>({});
  const [done, setDone] = useState(false);
  const inFlight = useRef(false);
  const submit = useSubmitCourseErrorReport();
  const resetRef = useRef(submit.reset);
  resetRef.current = submit.reset;

  useEffect(() => {
    setValues(empty); setErrors({}); setDone(false); inFlight.current = false; resetRef.current();
  }, [courseId, insight?.id, insight?.universityId]);

  const set = <K extends keyof typeof empty>(k: K, v: (typeof empty)[K]) => {
    const next = { ...values, [k]: v };
    setValues(next);
    if (k === "message" || k === "source_url") {
      const field = k === "message" ? "message" : "source_url";
      setErrors((current) => current[field] ? { ...current, [field]: validateReport(next)[field] } : current);
    }
  };

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (inFlight.current || submit.isPending) return;
    const errs = validateReport(values);
    setErrors(errs);
    if (Object.keys(errs).length) return;
    inFlight.current = true;
    const src = values.source_url.trim();
    submit.mutate(
      {
        ...(insight ? { university_id: insight.universityId, insight_id: insight.id } : { course_id: courseId }),
        kind: values.kind, field: insight ? "key_insights" : values.field, message: values.message.trim(),
        ...(src ? { source_url: src } : {}), ...(values.website ? { website: values.website } : {}),
      },
      { onSuccess: () => setDone(true), onSettled: () => { inFlight.current = false; } },
    );
  }

  if (done) {
    return (
      <div role="status" className="py-4 text-center" data-testid="status-report-success">
        <CheckCircle2 className="mx-auto h-9 w-9 text-[hsl(var(--sic-teal,174_60%_35%))]" aria-hidden />
        <p className="mt-3 text-lg font-semibold">Thanks, your report is with our team</p>
        <p className="mt-1 text-sm text-muted-foreground">We check every report against the institution's published details before changing anything.</p>
        {onDone && <button type="button" onClick={onDone} className="mt-4 h-10 rounded-lg border px-5 text-sm font-semibold" data-testid="button-report-close">Close</button>}
      </div>
    );
  }

  const len = values.message.trim().length;
  const sel = "h-10 w-full rounded-md border border-input bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
  return (
    <form onSubmit={onSubmit} noValidate className="space-y-4" data-testid="form-report-error" aria-busy={submit.isPending}>
      <div className="rounded-lg bg-muted/60 px-3 py-2 text-sm" data-testid="text-report-course">
        <p className="text-[11px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">{insight ? "Key Insights" : "Course"}</p>
        <p className="font-semibold">{courseName}</p>
        <p className="text-muted-foreground">{universityName}</p>
      </div>
      <fieldset>
        <legend className="mb-1.5 text-sm font-medium">What is wrong?</legend>
        <div className="grid grid-cols-2 gap-2">
          {REPORT_KINDS.map((k) => (
            <label key={k.value} className={`flex cursor-pointer items-center gap-2 rounded-md border px-3 py-2 text-sm ${values.kind === k.value ? "border-primary bg-primary/5 font-medium" : "border-input"}`}>
              <input type="radio" name="report-kind" value={k.value} checked={values.kind === k.value} onChange={() => set("kind", k.value)} data-testid={`radio-kind-${k.value}`} />
              {k.label}
            </label>
          ))}
        </div>
      </fieldset>
      {!insight && <div className="space-y-1.5">
        <Label htmlFor="report-field">Which detail?</Label>
        <select id="report-field" className={sel} value={values.field} onChange={(e) => set("field", e.target.value as ReportField)} data-testid="select-report-field">
          {REPORT_FIELDS.filter((f) => f.value !== "key_insights").map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
        </select>
      </div>}
      <div className="space-y-1.5">
        <Label htmlFor="report-message">Description</Label>
        <Textarea id="report-message" rows={4} maxLength={2000} value={values.message}
          onChange={(e) => set("message", e.target.value)} aria-invalid={!!errors.message}
          aria-describedby="report-message-hint" placeholder="For example: the 2025 tuition fee is listed as AUD 32,400 on the university page." data-testid="input-report-message" />
        <p id="report-message-hint" className={`flex justify-between text-xs ${errors.message ? "text-destructive" : "text-muted-foreground"}`}>
          <span data-testid="error-report-message">{errors.message ?? "At least 10 characters."}</span><span>{len}/2000</span>
        </p>
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="report-source">Source link <span className="font-normal text-muted-foreground">(optional)</span></Label>
        <Input id="report-source" type="url" inputMode="url" value={values.source_url} onChange={(e) => set("source_url", e.target.value)}
          aria-invalid={!!errors.source_url} aria-describedby={errors.source_url ? "report-source-error" : undefined} placeholder="https://" data-testid="input-report-source" />
        {errors.source_url && <p id="report-source-error" className="text-xs text-destructive" data-testid="error-report-source">{errors.source_url}</p>}
      </div>
      <div aria-hidden="true" style={{ position: "absolute", left: "-10000px", width: 1, height: 1, overflow: "hidden" }}>
        <label htmlFor="report-website">Website</label>
        <input id="report-website" name="website" tabIndex={-1} autoComplete="off" value={values.website} onChange={(e) => set("website", e.target.value)} data-testid="input-report-website" />
      </div>
      {submit.isError && (
        <div role="alert" className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive" data-testid="error-report-submit">
          {submit.error instanceof TypeError ? "We could not reach the server. Check your connection and try again." : submit.error.message}
          {" "}Your details are kept, so you can try again.
        </div>
      )}
      <button type="submit" disabled={submit.isPending} data-testid="button-report-submit"
        className="inline-flex h-10 w-full items-center justify-center gap-2 rounded-lg bg-primary text-sm font-semibold text-primary-foreground hover:opacity-90 disabled:opacity-60">
        {submit.isPending && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />}
        {submit.isPending ? "Sending report" : submit.isError ? "Try again" : "Send report"}
      </button>
      <p className="text-xs text-muted-foreground">Reports go to our editors for review. They do not change {insight ? "Key Insights" : "course details"} directly.</p>
    </form>
  );
}

export function ReportCourseErrorButton({ courseId, courseName, universityName, insight, className }: Props) {
  const [open, setOpen] = useState(false);
  useEffect(() => { setOpen(false); }, [courseId, insight?.id, insight?.universityId]);
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <button type="button" data-testid="button-report-error"
          className={className ?? "inline-flex h-9 items-center justify-center gap-1.5 rounded-lg text-xs font-semibold text-muted-foreground hover:text-foreground"}>
          <Flag className="h-3.5 w-3.5" aria-hidden /> Report an error
        </button>
      </DialogTrigger>
      <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Report an error</DialogTitle>
          <DialogDescription>Tell us what is missing or incorrect {insight ? "in these Key Insights" : "on this course listing"}.</DialogDescription>
        </DialogHeader>
        <ReportCourseErrorForm key={insight ? `insight-${insight.universityId}-${insight.id}` : courseId} courseId={courseId} courseName={courseName} universityName={universityName} insight={insight} onDone={() => setOpen(false)} />
      </DialogContent>
    </Dialog>
  );
}
