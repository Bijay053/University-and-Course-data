import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useToast } from "@/hooks/use-toast";
import { SettingsTabs } from "@/components/settings-tabs";
import { Plus, Save, Trash2, Sparkles, Search, RefreshCw, X } from "lucide-react";
import { cn } from "@/lib/utils";

const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");

interface ConfigEntry {
  slug: string;
  title: string;
  yaml: string;
}

interface GenerateForm {
  university_name: string;
  website_url: string;
  country: string;
  notes: string;
}

export default function SettingsScraperConfigs() {
  const { toast } = useToast();
  const [configs, setConfigs] = useState<ConfigEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);
  const [editorYaml, setEditorYaml] = useState("");
  const [editorSlug, setEditorSlug] = useState("");
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [filter, setFilter] = useState("");
  const [showNewModal, setShowNewModal] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [genForm, setGenForm] = useState<GenerateForm>({
    university_name: "",
    website_url: "",
    country: "Australia",
    notes: "",
  });
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const fetchConfigs = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`${BASE}/api/settings/scraper-configs`, { credentials: "include" });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      setConfigs(data.configs ?? []);
    } catch (err) {
      toast({ title: "Failed to load configs", description: (err as Error).message, variant: "destructive" });
    } finally {
      setLoading(false);
    }
  }, [toast]);

  useEffect(() => { void fetchConfigs(); }, [fetchConfigs]);

  const selectConfig = (slug: string) => {
    const cfg = configs.find(c => c.slug === slug);
    if (!cfg) return;
    setSelected(slug);
    setEditorSlug(slug);
    setEditorYaml(cfg.yaml);
  };

  const handleSave = async () => {
    if (!editorSlug.trim()) { toast({ title: "Slug required", variant: "destructive" }); return; }
    setSaving(true);
    try {
      const res = await fetch(`${BASE}/api/settings/scraper-configs/${editorSlug.trim()}`, {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ yaml_content: editorYaml }),
      });
      if (!res.ok) { const d = await res.json(); throw new Error(d.detail ?? "Save failed"); }
      toast({ title: "Saved", description: `Config for '${editorSlug}' saved` });
      await fetchConfigs();
      setSelected(editorSlug.trim());
    } catch (err) {
      toast({ title: "Save failed", description: (err as Error).message, variant: "destructive" });
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async () => {
    if (!selected) return;
    if (!confirm(`Delete config for '${selected}'? This cannot be undone.`)) return;
    setDeleting(true);
    try {
      const res = await fetch(`${BASE}/api/settings/scraper-configs/${selected}`, {
        method: "DELETE",
        credentials: "include",
      });
      if (!res.ok) { const d = await res.json(); throw new Error(d.detail ?? "Delete failed"); }
      toast({ title: "Deleted", description: `Config for '${selected}' removed` });
      setSelected(null);
      setEditorYaml("");
      setEditorSlug("");
      await fetchConfigs();
    } catch (err) {
      toast({ title: "Delete failed", description: (err as Error).message, variant: "destructive" });
    } finally {
      setDeleting(false);
    }
  };

  const handleGenerate = async () => {
    if (!genForm.university_name.trim() || !genForm.website_url.trim()) {
      toast({ title: "Name and URL required", variant: "destructive" });
      return;
    }
    setGenerating(true);
    try {
      const res = await fetch(`${BASE}/api/settings/scraper-configs/generate`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(genForm),
      });
      if (!res.ok) { const d = await res.json(); throw new Error(d.detail ?? "Generation failed"); }
      const data = await res.json();
      setEditorYaml(data.yaml ?? "");
      setEditorSlug(data.slug ?? "");
      setSelected(null);
      setShowNewModal(false);
      toast({ title: "Generated!", description: "Review and edit the config below, then save." });
      setTimeout(() => textareaRef.current?.focus(), 100);
    } catch (err) {
      toast({ title: "AI generation failed", description: (err as Error).message, variant: "destructive" });
    } finally {
      setGenerating(false);
    }
  };

  const filtered = configs.filter(c =>
    c.slug.includes(filter.toLowerCase()) || c.title.toLowerCase().includes(filter.toLowerCase())
  );

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Scraper Configs</h1>
        <p className="text-muted-foreground text-sm mt-1">
          Per-university YAML overrides for the web scraper. Changes take effect on the next scrape job.
        </p>
      </div>

      <SettingsTabs />

      <div className="flex gap-4 h-[calc(100vh-280px)] min-h-[500px]">
        {/* Left sidebar — config list */}
        <div className="w-64 flex-shrink-0 border rounded-lg overflow-hidden flex flex-col bg-background">
          <div className="p-2 border-b flex gap-1">
            <div className="relative flex-1">
              <Search className="absolute left-2 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground" />
              <Input
                className="pl-7 h-8 text-xs"
                placeholder="Filter…"
                value={filter}
                onChange={e => setFilter(e.target.value)}
              />
            </div>
            <Button
              size="sm"
              variant="outline"
              className="h-8 w-8 p-0"
              title="Refresh"
              onClick={fetchConfigs}
            >
              <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
            </Button>
          </div>

          <div className="flex-1 overflow-y-auto">
            {loading ? (
              <div className="p-3 text-xs text-muted-foreground">Loading…</div>
            ) : filtered.length === 0 ? (
              <div className="p-3 text-xs text-muted-foreground">No configs found</div>
            ) : (
              filtered.map(cfg => (
                <button
                  key={cfg.slug}
                  onClick={() => selectConfig(cfg.slug)}
                  className={cn(
                    "w-full text-left px-3 py-2 text-sm border-b last:border-b-0 hover:bg-muted/50 transition-colors",
                    selected === cfg.slug && "bg-primary/10 font-medium text-primary"
                  )}
                >
                  <div className="font-medium truncate">{cfg.slug}</div>
                  <div className="text-xs text-muted-foreground truncate">{cfg.title}</div>
                </button>
              ))
            )}
          </div>

          <div className="p-2 border-t">
            <Button
              size="sm"
              className="w-full h-8 text-xs"
              onClick={() => {
                setGenForm({ university_name: "", website_url: "", country: "Australia", notes: "" });
                setShowNewModal(true);
              }}
            >
              <Plus className="h-3.5 w-3.5 mr-1" />
              New Config
            </Button>
          </div>
        </div>

        {/* Right — editor */}
        <div className="flex-1 border rounded-lg overflow-hidden flex flex-col bg-background">
          {!selected && !editorYaml ? (
            <div className="flex-1 flex items-center justify-center text-muted-foreground text-sm">
              Select a config from the list, or click <strong className="mx-1">New Config</strong> to create one.
            </div>
          ) : (
            <>
              <div className="px-4 py-2 border-b flex items-center gap-3">
                <div className="flex-1 flex items-center gap-2">
                  <Label className="text-xs text-muted-foreground whitespace-nowrap">Slug</Label>
                  <Input
                    className="h-7 text-xs font-mono w-48"
                    value={editorSlug}
                    onChange={e => setEditorSlug(e.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, ""))}
                    placeholder="e.g. myuniversity"
                  />
                  <span className="text-xs text-muted-foreground">→ scraper_config/unis/{editorSlug || "…"}.yaml</span>
                </div>
                <div className="flex gap-2">
                  {selected && (
                    <Button
                      size="sm"
                      variant="outline"
                      className="h-7 text-xs text-destructive hover:text-destructive"
                      onClick={handleDelete}
                      disabled={deleting}
                    >
                      <Trash2 className="h-3.5 w-3.5 mr-1" />
                      {deleting ? "Deleting…" : "Delete"}
                    </Button>
                  )}
                  <Button size="sm" className="h-7 text-xs" onClick={handleSave} disabled={saving}>
                    <Save className="h-3.5 w-3.5 mr-1" />
                    {saving ? "Saving…" : "Save"}
                  </Button>
                </div>
              </div>

              <textarea
                ref={textareaRef}
                className="flex-1 resize-none font-mono text-xs p-4 bg-muted/20 focus:outline-none focus:bg-background transition-colors"
                value={editorYaml}
                onChange={e => setEditorYaml(e.target.value)}
                spellCheck={false}
                placeholder={`# University Name\n# Hostname: www.example.edu.au\n\ndiscovery: {}\nextraction:\n  fees:\n    default_currency: "AUD"\n`}
              />

              <div className="px-4 py-1.5 border-t bg-muted/30 text-xs text-muted-foreground flex items-center gap-4">
                <span>{editorYaml.split("\n").length} lines</span>
                <span>Changes take effect on next scrape job</span>
              </div>
            </>
          )}
        </div>
      </div>

      {/* Generate modal */}
      {showNewModal && (
        <div className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4">
          <div className="bg-background rounded-xl shadow-xl w-full max-w-md p-6 space-y-4">
            <div className="flex items-center justify-between">
              <h2 className="font-semibold text-lg">New University Config</h2>
              <button onClick={() => setShowNewModal(false)} className="p-1 rounded hover:bg-muted">
                <X className="h-4 w-4" />
              </button>
            </div>

            <p className="text-sm text-muted-foreground">
              Enter the university details and let AI generate a starter YAML config based on the website structure.
            </p>

            <div className="space-y-3">
              <div>
                <Label className="text-xs">University Name *</Label>
                <Input
                  className="mt-1"
                  placeholder="e.g. Macquarie University"
                  value={genForm.university_name}
                  onChange={e => setGenForm(f => ({ ...f, university_name: e.target.value }))}
                />
              </div>
              <div>
                <Label className="text-xs">Website URL *</Label>
                <Input
                  className="mt-1"
                  placeholder="e.g. https://www.mq.edu.au"
                  value={genForm.website_url}
                  onChange={e => setGenForm(f => ({ ...f, website_url: e.target.value }))}
                />
              </div>
              <div>
                <Label className="text-xs">Country</Label>
                <Input
                  className="mt-1"
                  placeholder="Australia"
                  value={genForm.country}
                  onChange={e => setGenForm(f => ({ ...f, country: e.target.value }))}
                />
              </div>
              <div>
                <Label className="text-xs">Notes for AI (optional)</Label>
                <Input
                  className="mt-1"
                  placeholder="e.g. React SPA, NZ dollars, filters domestic courses"
                  value={genForm.notes}
                  onChange={e => setGenForm(f => ({ ...f, notes: e.target.value }))}
                />
              </div>
            </div>

            <div className="flex gap-2 pt-1">
              <Button variant="outline" className="flex-1" onClick={() => setShowNewModal(false)}>
                Cancel
              </Button>
              <Button
                className="flex-1"
                onClick={handleGenerate}
                disabled={generating || !genForm.university_name.trim() || !genForm.website_url.trim()}
              >
                <Sparkles className="h-4 w-4 mr-2" />
                {generating ? "Generating…" : "Generate with AI"}
              </Button>
            </div>

            <p className="text-xs text-muted-foreground text-center">
              Uses Gemini AI · review the output before saving
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
