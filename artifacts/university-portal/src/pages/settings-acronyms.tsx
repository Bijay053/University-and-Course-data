import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useToast } from "@/hooks/use-toast";
import { Plus, Trash2, RefreshCw } from "lucide-react";
import { readResponseJson } from "@/lib/readResponseJson";

const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");

type CustomAcronym = {
  id: number;
  acronym: string;
  note: string | null;
  createdAt: string;
};

type AcronymsResponse = {
  defaults: string[];
  custom: CustomAcronym[];
};

export default function SettingsAcronyms() {
  const { toast } = useToast();
  const [defaults, setDefaults] = useState<string[]>([]);
  const [custom, setCustom] = useState<CustomAcronym[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [newAcronym, setNewAcronym] = useState("");
  const [newNote, setNewNote] = useState("");

  const fetchAcronyms = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`${BASE}/api/settings/acronyms`);
      const data = await readResponseJson<AcronymsResponse>(res);
      setDefaults(data?.defaults ?? []);
      setCustom(data?.custom ?? []);
    } catch (err) {
      toast({ title: "Failed to load acronyms", description: (err as Error).message, variant: "destructive" });
    } finally {
      setLoading(false);
    }
  }, [toast]);

  useEffect(() => {
    void fetchAcronyms();
  }, [fetchAcronyms]);

  const handleAdd = async () => {
    const acronym = newAcronym.trim().toUpperCase();
    if (!acronym) return;
    setSaving(true);
    try {
      const res = await fetch(`${BASE}/api/settings/acronyms`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ acronym, note: newNote.trim() || undefined }),
      });
      if (!res.ok) {
        const body = await readResponseJson<{ error?: string }>(res);
        throw new Error(body?.error ?? `HTTP ${res.status}`);
      }
      setNewAcronym("");
      setNewNote("");
      await fetchAcronyms();
      toast({ title: "Added", description: acronym });
    } catch (err) {
      toast({ title: "Failed to add", description: (err as Error).message, variant: "destructive" });
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (id: number, acronym: string) => {
    if (!confirm(`Remove custom acronym "${acronym}"?`)) return;
    try {
      const res = await fetch(`${BASE}/api/settings/acronyms/${id}`, { method: "DELETE" });
      if (!res.ok) throw new Error(await res.text());
      await fetchAcronyms();
    } catch (err) {
      toast({ title: "Failed to delete", description: (err as Error).message, variant: "destructive" });
    }
  };

  return (
    <div className="space-y-6 max-w-3xl">
      <div>
        <h1 className="text-2xl font-bold">Course Name Acronyms</h1>
        <p className="text-sm text-muted-foreground mt-1">
          When a scraped course name contains an acronym (like MBA, BBUS or GDBA), the system keeps it
          uppercase instead of title-casing it ("Mba" → "MBA"). Add new acronyms here whenever a
          university introduces a new course code — changes apply to the next scrape without a redeploy.
        </p>
      </div>

      <div className="border rounded-xl bg-white p-4 space-y-3">
        <Label className="text-sm font-medium">Add a new acronym</Label>
        <div className="flex gap-2">
          <Input
            value={newAcronym}
            onChange={(e) => setNewAcronym(e.target.value.toUpperCase())}
            placeholder="e.g. BPA"
            maxLength={16}
            className="w-32 font-mono uppercase tracking-wider"
            onKeyDown={(e) => { if (e.key === "Enter") void handleAdd(); }}
            disabled={saving}
          />
          <Input
            value={newNote}
            onChange={(e) => setNewNote(e.target.value)}
            placeholder="Optional note (e.g. Bachelor of Professional Accounting)"
            onKeyDown={(e) => { if (e.key === "Enter") void handleAdd(); }}
            disabled={saving}
          />
          <Button onClick={handleAdd} disabled={saving || !newAcronym.trim()}>
            <Plus className="w-4 h-4 mr-1" /> Add
          </Button>
        </div>
        <p className="text-xs text-muted-foreground">
          Use 1–16 letters/digits, starting with a letter. Acronyms are stored in uppercase.
        </p>
      </div>

      <div className="border rounded-xl bg-white overflow-hidden">
        <div className="px-4 py-3 border-b flex items-center justify-between bg-gray-50">
          <span className="text-sm font-medium text-gray-700">
            Custom acronyms — {custom.length}
          </span>
          <Button variant="ghost" size="sm" onClick={fetchAcronyms} disabled={loading}>
            <RefreshCw className={`w-4 h-4 mr-1 ${loading ? "animate-spin" : ""}`} /> Refresh
          </Button>
        </div>
        {loading && custom.length === 0 ? (
          <div className="p-10 text-center text-gray-400">Loading…</div>
        ) : custom.length === 0 ? (
          <div className="p-10 text-center text-gray-400">No custom acronyms yet. Add one above.</div>
        ) : (
          <ul className="divide-y">
            {custom.map((opt) => (
              <li key={opt.id} className="px-4 py-2.5 flex items-center gap-3">
                <span className="font-mono font-semibold text-sm text-gray-800 w-24">{opt.acronym}</span>
                <span className="flex-1 text-sm text-gray-600 truncate">
                  {opt.note ?? <span className="text-gray-300">—</span>}
                </span>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-7 w-7 text-red-600 hover:bg-red-50"
                  onClick={() => void handleDelete(opt.id, opt.acronym)}
                >
                  <Trash2 className="w-3.5 h-3.5" />
                </Button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="border rounded-xl bg-white overflow-hidden">
        <div className="px-4 py-3 border-b bg-gray-50">
          <span className="text-sm font-medium text-gray-700">
            Built-in acronyms — {defaults.length}
          </span>
          <p className="text-xs text-muted-foreground mt-0.5">
            These are always recognized. You don't need to add them.
          </p>
        </div>
        <div className="p-4 flex flex-wrap gap-1.5">
          {defaults.map((a) => (
            <span key={a} className="px-2 py-0.5 rounded bg-gray-100 text-xs font-mono text-gray-700">
              {a}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}
