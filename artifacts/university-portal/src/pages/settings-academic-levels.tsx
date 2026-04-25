import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useToast } from "@/hooks/use-toast";
import { ArrowDown, ArrowUp, Plus, Trash2, RefreshCw } from "lucide-react";
import { readResponseJson } from "@/lib/readResponseJson";
import { SettingsTabs } from "@/components/settings-tabs";

const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");

type LevelOption = {
  id: number;
  name: string;
  sortOrder: number;
  createdAt: string;
};

export default function SettingsAcademicLevels() {
  const { toast } = useToast();
  const [options, setOptions] = useState<LevelOption[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [newName, setNewName] = useState("");
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editingName, setEditingName] = useState("");

  const fetchOptions = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`${BASE}/api/settings/academic-levels`);
      const data = await readResponseJson<{ options: LevelOption[] }>(res);
      setOptions(data?.options ?? []);
    } catch (err) {
      toast({ title: "Failed to load options", description: (err as Error).message, variant: "destructive" });
    } finally {
      setLoading(false);
    }
  }, [toast]);

  useEffect(() => {
    void fetchOptions();
  }, [fetchOptions]);

  const handleAdd = async () => {
    const name = newName.trim();
    if (!name) return;
    setSaving(true);
    try {
      const res = await fetch(`${BASE}/api/settings/academic-levels`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      if (!res.ok) throw new Error(await res.text());
      setNewName("");
      await fetchOptions();
      toast({ title: "Added", description: name });
    } catch (err) {
      toast({ title: "Failed to add", description: (err as Error).message, variant: "destructive" });
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (id: number, name: string) => {
    if (!confirm(`Delete "${name}"?`)) return;
    try {
      const res = await fetch(`${BASE}/api/settings/academic-levels/${id}`, { method: "DELETE" });
      if (!res.ok) throw new Error(await res.text());
      await fetchOptions();
    } catch (err) {
      toast({ title: "Failed to delete", description: (err as Error).message, variant: "destructive" });
    }
  };

  const handleRename = async (id: number) => {
    const name = editingName.trim();
    if (!name) return;
    try {
      const res = await fetch(`${BASE}/api/settings/academic-levels/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      if (!res.ok) throw new Error(await res.text());
      setEditingId(null);
      await fetchOptions();
    } catch (err) {
      toast({ title: "Failed to rename", description: (err as Error).message, variant: "destructive" });
    }
  };

  const move = async (index: number, direction: -1 | 1) => {
    const next = index + direction;
    if (next < 0 || next >= options.length) return;
    const reordered = [...options];
    [reordered[index], reordered[next]] = [reordered[next], reordered[index]];
    const items = reordered.map((o, i) => ({ id: o.id, sortOrder: i + 1 }));
    setOptions(reordered.map((o, i) => ({ ...o, sortOrder: i + 1 })));
    try {
      const res = await fetch(`${BASE}/api/settings/academic-levels/reorder`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items }),
      });
      if (!res.ok) throw new Error(await res.text());
    } catch (err) {
      toast({ title: "Failed to reorder", description: (err as Error).message, variant: "destructive" });
      await fetchOptions();
    }
  };

  return (
    <div className="space-y-6 max-w-3xl">
      <div>
        <h1 className="text-2xl font-bold">Settings</h1>
      </div>
      <SettingsTabs />
      <div>
        <h2 className="text-xl font-semibold">Academic Level Options</h2>
        <p className="text-sm text-muted-foreground mt-1">
          Manage the dropdown list shown when adding or editing academic requirements. The order
          here controls the order in the dropdown.
        </p>
      </div>

      <div className="border rounded-xl bg-white p-4 space-y-3">
        <Label className="text-sm font-medium">Add a new level</Label>
        <div className="flex gap-2">
          <Input
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder="e.g. Bachelor's degree with Honours"
            onKeyDown={(e) => { if (e.key === "Enter") void handleAdd(); }}
            disabled={saving}
          />
          <Button onClick={handleAdd} disabled={saving || !newName.trim()}>
            <Plus className="w-4 h-4 mr-1" /> Add Level
          </Button>
        </div>
      </div>

      <div className="border rounded-xl bg-white overflow-hidden">
        <div className="px-4 py-3 border-b flex items-center justify-between bg-gray-50">
          <span className="text-sm font-medium text-gray-700">
            {options.length} level{options.length === 1 ? "" : "s"}
          </span>
          <Button variant="ghost" size="sm" onClick={fetchOptions} disabled={loading}>
            <RefreshCw className={`w-4 h-4 mr-1 ${loading ? "animate-spin" : ""}`} /> Refresh
          </Button>
        </div>
        {loading && options.length === 0 ? (
          <div className="p-10 text-center text-gray-400">Loading…</div>
        ) : options.length === 0 ? (
          <div className="p-10 text-center text-gray-400">No options defined yet. Add one above.</div>
        ) : (
          <ul className="divide-y">
            {options.map((opt, idx) => (
              <li key={opt.id} className="px-4 py-2.5 flex items-center gap-3">
                <span className="text-xs text-gray-400 w-6 text-right">{idx + 1}.</span>
                {editingId === opt.id ? (
                  <Input
                    autoFocus
                    value={editingName}
                    onChange={(e) => setEditingName(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") void handleRename(opt.id);
                      if (e.key === "Escape") setEditingId(null);
                    }}
                    onBlur={() => void handleRename(opt.id)}
                    className="flex-1 h-8"
                  />
                ) : (
                  <button
                    type="button"
                    className="flex-1 text-left text-sm text-gray-800 hover:text-blue-700"
                    onClick={() => { setEditingId(opt.id); setEditingName(opt.name); }}
                    title="Click to rename"
                  >
                    {opt.name}
                  </button>
                )}
                <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => void move(idx, -1)} disabled={idx === 0}>
                  <ArrowUp className="w-3.5 h-3.5" />
                </Button>
                <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => void move(idx, 1)} disabled={idx === options.length - 1}>
                  <ArrowDown className="w-3.5 h-3.5" />
                </Button>
                <Button variant="ghost" size="icon" className="h-7 w-7 text-red-600 hover:bg-red-50" onClick={() => void handleDelete(opt.id, opt.name)}>
                  <Trash2 className="w-3.5 h-3.5" />
                </Button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
