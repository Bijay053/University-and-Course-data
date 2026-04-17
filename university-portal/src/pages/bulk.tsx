import { useState, useRef } from "react";
import { useListUniversities } from "@workspace/api-client-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { Upload, FileSpreadsheet, CheckCircle2, AlertCircle, Loader2, X } from "lucide-react";

type ImportResult = {
  universityName: string;
  totalRows: number;
  imported: number;
  skipped: number;
  errors: string[];
};

export default function Bulk() {
  const [file, setFile] = useState<File | null>(null);
  const [uniMode, setUniMode] = useState<"existing" | "new">("existing");
  const [universityId, setUniversityId] = useState<string>("");
  const [newUniName, setNewUniName] = useState("");
  const [newUniCountry, setNewUniCountry] = useState("Australia");
  const [newUniCity, setNewUniCity] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<ImportResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const { data: uniData } = useListUniversities({ limit: 100 });
  const universities = uniData?.data ?? [];

  const handleFile = (f: File | null) => {
    setFile(f);
    setResult(null);
    setError(null);
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    const f = e.dataTransfer.files[0];
    if (f && (f.name.endsWith(".xlsx") || f.name.endsWith(".xls"))) handleFile(f);
  };

  const handleImport = async () => {
    if (!file) { setError("Please select a file."); return; }
    if (uniMode === "existing" && !universityId) { setError("Please select a university."); return; }
    if (uniMode === "new" && !newUniName.trim()) { setError("Please enter a university name."); return; }

    setLoading(true);
    setError(null);
    setResult(null);

    const formData = new FormData();
    formData.append("file", file);
    if (uniMode === "existing") {
      formData.append("universityId", universityId);
    } else {
      formData.append("universityName", newUniName.trim());
      formData.append("universityCountry", newUniCountry.trim());
      formData.append("universityCity", newUniCity.trim());
    }

    try {
      const res = await fetch("/api/import/excel", { method: "POST", body: formData });
      const data = await res.json();
      if (!res.ok) { setError(data.error || "Import failed"); return; }
      setResult(data as ImportResult);
    } catch (err) {
      setError("Network error: " + (err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-6 max-w-2xl">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Bulk Upload</h1>
        <p className="text-muted-foreground">Import scraped course data from Excel files into the portal.</p>
      </div>

      <div
        className={`border-2 border-dashed rounded-xl p-10 text-center transition-colors cursor-pointer ${
          file ? "border-blue-400 bg-blue-50" : "border-gray-300 hover:border-blue-300 hover:bg-gray-50"
        }`}
        onClick={() => fileRef.current?.click()}
        onDrop={handleDrop}
        onDragOver={(e) => e.preventDefault()}
      >
        <input
          ref={fileRef}
          type="file"
          accept=".xlsx,.xls"
          className="hidden"
          onChange={(e) => handleFile(e.target.files?.[0] ?? null)}
        />
        {file ? (
          <div className="flex items-center justify-center gap-3">
            <FileSpreadsheet className="w-8 h-8 text-blue-500" />
            <div className="text-left">
              <p className="font-medium text-blue-700">{file.name}</p>
              <p className="text-sm text-blue-500">{(file.size / 1024).toFixed(0)} KB</p>
            </div>
            <button
              className="ml-2 text-gray-400 hover:text-red-500"
              onClick={(e) => { e.stopPropagation(); handleFile(null); if (fileRef.current) fileRef.current.value = ""; }}
            >
              <X className="w-5 h-5" />
            </button>
          </div>
        ) : (
          <div className="text-gray-500">
            <Upload className="w-10 h-10 mx-auto mb-3 text-gray-400" />
            <p className="font-medium">Drop your Excel file here or click to browse</p>
            <p className="text-sm mt-1">Supports .xlsx and .xls — Scrapy pipeline output format</p>
          </div>
        )}
      </div>

      <div className="space-y-4 border rounded-xl p-5">
        <h2 className="font-semibold text-gray-800">University</h2>
        <div className="flex gap-3">
          <Button variant={uniMode === "existing" ? "default" : "outline"} size="sm" onClick={() => setUniMode("existing")}>
            Existing University
          </Button>
          <Button variant={uniMode === "new" ? "default" : "outline"} size="sm" onClick={() => setUniMode("new")}>
            New University
          </Button>
        </div>

        {uniMode === "existing" ? (
          <div>
            <Label>Select University</Label>
            <Select value={universityId} onValueChange={setUniversityId}>
              <SelectTrigger className="mt-1">
                <SelectValue placeholder="Choose a university..." />
              </SelectTrigger>
              <SelectContent>
                {universities.map((u) => (
                  <SelectItem key={u.id} value={String(u.id)}>
                    {u.name} — {u.city}, {u.country}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-4">
            <div className="col-span-2">
              <Label>University Name *</Label>
              <Input className="mt-1" placeholder="e.g. University of Hull" value={newUniName} onChange={(e) => setNewUniName(e.target.value)} />
            </div>
            <div>
              <Label>Country</Label>
              <Input className="mt-1" placeholder="e.g. United Kingdom" value={newUniCountry} onChange={(e) => setNewUniCountry(e.target.value)} />
            </div>
            <div>
              <Label>City</Label>
              <Input className="mt-1" placeholder="e.g. Hull" value={newUniCity} onChange={(e) => setNewUniCity(e.target.value)} />
            </div>
          </div>
        )}
      </div>

      {error && (
        <div className="flex items-start gap-2 rounded-lg bg-red-50 border border-red-200 p-4 text-red-700">
          <AlertCircle className="w-5 h-5 mt-0.5 shrink-0" />
          <p className="text-sm">{error}</p>
        </div>
      )}

      {result && (
        <div className="rounded-xl border border-green-200 bg-green-50 p-5 space-y-3">
          <div className="flex items-center gap-2 text-green-700 font-semibold">
            <CheckCircle2 className="w-5 h-5" />
            Import Complete — {result.universityName}
          </div>
          <div className="grid grid-cols-3 gap-4 text-center">
            <div className="bg-white rounded-lg p-3 border border-green-200">
              <div className="text-2xl font-bold text-gray-800">{result.totalRows}</div>
              <div className="text-xs text-gray-500 mt-0.5">Total Rows</div>
            </div>
            <div className="bg-white rounded-lg p-3 border border-green-200">
              <div className="text-2xl font-bold text-green-600">{result.imported}</div>
              <div className="text-xs text-gray-500 mt-0.5">Imported</div>
            </div>
            <div className="bg-white rounded-lg p-3 border border-green-200">
              <div className="text-2xl font-bold text-amber-500">{result.skipped}</div>
              <div className="text-xs text-gray-500 mt-0.5">Skipped (Duplicates)</div>
            </div>
          </div>
          {result.errors.length > 0 && (
            <div className="space-y-1">
              <p className="text-sm font-medium text-red-600">Errors ({result.errors.length}):</p>
              {result.errors.map((e, i) => (
                <p key={i} className="text-xs text-red-500 bg-white rounded p-1 border border-red-100">{e}</p>
              ))}
            </div>
          )}
        </div>
      )}

      <Button className="w-full" size="lg" onClick={handleImport} disabled={loading || !file}>
        {loading ? (
          <><Loader2 className="w-4 h-4 mr-2 animate-spin" />Importing...</>
        ) : (
          <><Upload className="w-4 h-4 mr-2" />Import Excel File</>
        )}
      </Button>

      <div className="rounded-xl border bg-gray-50 p-5 text-sm text-gray-600 space-y-2">
        <p className="font-medium text-gray-700">Expected Column Format</p>
        <p>The Excel file should use the Scrapy pipeline output format with these key columns:</p>
        <div className="flex flex-wrap gap-1 mt-2">
          {[
            "Course Name", "Category", "Sub Category", "Degree Level", "Duration", "Duration Term",
            "Study Mode", "Study Load", "Intake Month", "International Fee", "Fee Term", "Currency",
            "IELTS Overall", "IELTS Listening", "PTE Overall", "TOEFL Overall",
            "Academic Level", "Academic Country", "Scholarship", "Course Website",
          ].map((col) => (
            <Badge key={col} variant="secondary" className="text-xs">{col}</Badge>
          ))}
        </div>
      </div>
    </div>
  );
}
