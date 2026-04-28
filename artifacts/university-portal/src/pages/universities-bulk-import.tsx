import { useState, useRef } from "react";
import { Link } from "wouter";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, AlertTitle, AlertDescription } from "@/components/ui/alert";
import { ArrowLeft, Upload, Download, CheckCircle2, AlertCircle } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { useQueryClient } from "@tanstack/react-query";
import { getListUniversitiesQueryKey } from "@workspace/api-client-react";

const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");

const SAMPLE_CSV = `name,website,scrape_url,country,city
Macquarie University,https://www.mq.edu.au,https://www.mq.edu.au/study/find-a-course/courses,Australia,Sydney
Bond University,https://bond.edu.au,https://bond.edu.au/program-list,Australia,Gold Coast
Australian Catholic University,https://www.acu.edu.au,https://www.acu.edu.au/study-at-acu/find-a-course,Australia,North Sydney
`;

type ImportResult = {
  created: number;
  skipped: number;
  errors: string[];
};

export default function UniversitiesBulkImport() {
  const [file, setFile] = useState<File | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<ImportResult | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const { toast } = useToast();
  const qc = useQueryClient();

  const onFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setResult(null);
    setErrorMsg(null);
    const f = e.target.files?.[0] ?? null;
    setFile(f);
  };

  const downloadSample = () => {
    const blob = new Blob([SAMPLE_CSV], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "universities-sample.csv";
    a.click();
    URL.revokeObjectURL(url);
  };

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!file) {
      setErrorMsg("Please choose a CSV file first.");
      return;
    }
    setSubmitting(true);
    setResult(null);
    setErrorMsg(null);
    try {
      const fd = new FormData();
      fd.append("file", file);
      const res = await fetch(`${BASE}/api/universities/bulk-import`, {
        method: "POST",
        credentials: "include",
        body: fd,
      });
      const body = await res.json();
      if (!res.ok) {
        setErrorMsg(body?.detail ?? body?.error ?? `Upload failed (${res.status})`);
        return;
      }
      setResult(body as ImportResult);
      qc.invalidateQueries({ queryKey: getListUniversitiesQueryKey() });
      toast({
        title: "Import complete",
        description: `${body.created} created, ${body.skipped} skipped, ${body.errors?.length ?? 0} error(s).`,
      });
      if (fileInputRef.current) fileInputRef.current.value = "";
      setFile(null);
    } catch (err) {
      setErrorMsg((err as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="space-y-5 max-w-3xl">
      <div className="flex items-center gap-3">
        <Link href="/universities">
          <Button variant="ghost" size="sm" className="gap-1.5">
            <ArrowLeft className="h-4 w-4" /> Back to Universities
          </Button>
        </Link>
      </div>

      <div>
        <h1 className="text-2xl font-bold tracking-tight text-gray-900">Bulk Import Universities</h1>
        <p className="text-sm text-gray-500 mt-0.5">
          Upload a CSV with one row per university to add many at once.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">CSV format</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3 text-sm">
          <p>
            Required columns: <code className="font-mono bg-gray-100 px-1 rounded">name</code>,{" "}
            <code className="font-mono bg-gray-100 px-1 rounded">website</code>,{" "}
            <code className="font-mono bg-gray-100 px-1 rounded">country</code>,{" "}
            <code className="font-mono bg-gray-100 px-1 rounded">city</code>.
          </p>
          <p>
            Optional: <code className="font-mono bg-gray-100 px-1 rounded">scrape_url</code> (defaults to{" "}
            <code className="font-mono bg-gray-100 px-1 rounded">website</code> if omitted).
          </p>
          <p>
            Rows whose name already exists in the database (case-insensitive match) are
            skipped, not duplicated. <code className="font-mono bg-gray-100 px-1 rounded">country</code> and{" "}
            <code className="font-mono bg-gray-100 px-1 rounded">city</code> must be specified — the literal
            value &quot;Unknown&quot; is rejected.
          </p>
          <Button type="button" variant="outline" size="sm" className="gap-1.5" onClick={downloadSample}>
            <Download className="h-4 w-4" /> Download sample CSV
          </Button>
        </CardContent>
      </Card>

      <form onSubmit={onSubmit} className="space-y-4">
        <div className="space-y-2">
          <Label htmlFor="csv-file">CSV file</Label>
          <Input
            id="csv-file"
            ref={fileInputRef}
            type="file"
            accept=".csv,text/csv"
            onChange={onFileChange}
          />
        </div>
        <Button type="submit" disabled={!file || submitting} className="gap-1.5">
          <Upload className="h-4 w-4" />
          {submitting ? "Uploading…" : "Upload and import"}
        </Button>
      </form>

      {errorMsg && (
        <Alert variant="destructive">
          <AlertCircle className="h-4 w-4" />
          <AlertTitle>Upload failed</AlertTitle>
          <AlertDescription>{errorMsg}</AlertDescription>
        </Alert>
      )}

      {result && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base flex items-center gap-2">
              <CheckCircle2 className="h-5 w-5 text-emerald-600" />
              Import results
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            <div className="grid grid-cols-3 gap-3">
              <div className="rounded border p-3">
                <div className="text-xs text-gray-500">Created</div>
                <div className="text-2xl font-semibold text-emerald-600">{result.created}</div>
              </div>
              <div className="rounded border p-3">
                <div className="text-xs text-gray-500">Skipped (already exist)</div>
                <div className="text-2xl font-semibold text-gray-700">{result.skipped}</div>
              </div>
              <div className="rounded border p-3">
                <div className="text-xs text-gray-500">Errors</div>
                <div className="text-2xl font-semibold text-rose-600">{result.errors.length}</div>
              </div>
            </div>
            {result.errors.length > 0 && (
              <div>
                <div className="font-medium mb-1">Error details</div>
                <ul className="list-disc list-inside space-y-1 text-rose-700">
                  {result.errors.map((e, i) => (
                    <li key={i}>{e}</li>
                  ))}
                </ul>
              </div>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
