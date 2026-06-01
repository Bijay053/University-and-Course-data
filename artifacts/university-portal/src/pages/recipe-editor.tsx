import { useState, useEffect, useCallback } from "react";
import { useParams, useLocation } from "wouter";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { useToast } from "@/hooks/use-toast";
import {
  ArrowLeft, Save, Plus, Trash2, Globe, Database, Filter,
  Code2, DollarSign, BookOpen, MapPin, ShieldCheck, Zap, RefreshCw,
  FlaskConical, CheckCircle2, XCircle, AlertTriangle, ChevronDown, ChevronUp, Info,
  MousePointerClick, GripVertical, Link, Stethoscope, Loader2, Wand2, WifiOff,
  TrendingUp, ExternalLink, Play, Type
} from "lucide-react";

// ── Types ──────────────────────────────────────────────────────────────────

interface FeeRule {
  amount: number;
  keywords: string[];
}

interface FieldSelector {
  xpath?: string;
  css?: string;
  regex?: string;
  attribute?: string;
}

interface ApiConfig {
  endpoint: string;
  method: string;
  root_path: string;
  course_url_template: string;
  fields: Record<string, string>;
  headers: Record<string, string>;
  pagination?: {
    type: string;
    page_param: string;
    size_param: string;
    page_size: number;
    max_pages: number;
  };
}

interface BandSpecUI {
  ielts_overall?: number | string;
  ielts_each?: number | string;
  pte_overall?: number | string;
  toefl_overall?: number | string;
}

interface BrowserAction {
  action_type: "click_text" | "click_css" | "wait_for_text" | "wait_for_selector" | "expand_text" | "scroll_to";
  value: string;
}

interface Recipe {
  discovery_strategy: string;
  seed_urls: string[];
  extra_course_urls: string[];
  expected_min_courses: number | null;
  expected_max_courses: number | null;
  fallback_strategy: string;
  api?: ApiConfig;
  must_contain: string[];
  block_url_patterns: string[];
  fetch_detail_page: boolean;
  selectors: Record<string, FieldSelector>;
  ielts?: {
    overall_regex: string;
    band_regex: string;
    source_xpath: string;
  };
  intake?: {
    xpath: string;
    regex: string;
    month_map: Record<string, string>;
  };
  fee_currency: string;
  fee_year: number | null;
  fee_rules_undergraduate: FeeRule[];
  fee_rules_postgraduate: FeeRule[];
  fee_reject_keywords: string[];
  fee_prefer_international: boolean;
  fee_follow_links: string[];
  campus?: {
    default_city: string;
    valid_campuses: string[];
    online_only_reject: boolean;
  };
  minimum_completeness: number;
  required_fields: string[];
  block_publish_if: string[];
  follow_links: string[];
  band_reference_url: string;
  band_mapping: Record<string, BandSpecUI>;
  actions: BrowserAction[];
  // Course name cleanup
  course_name_remove_after: string[];
  course_name_remove_year_suffix: boolean;
  course_name_remove_patterns: string[];
  // Location cleanup
  location_replace: Record<string, string>;
  location_allowed_values: string[];
  location_reject_values: string[];
  // Study mode
  study_mode_from_location: boolean;
  study_mode_online_keywords: string[];
}

const EMPTY_RECIPE: Recipe = {
  discovery_strategy: "auto",
  seed_urls: [],
  extra_course_urls: [],
  expected_min_courses: null,
  expected_max_courses: null,
  fallback_strategy: "bfs",
  must_contain: [],
  block_url_patterns: [],
  fetch_detail_page: true,
  selectors: {},
  fee_currency: "AUD",
  fee_year: null,
  fee_rules_undergraduate: [],
  fee_rules_postgraduate: [],
  fee_reject_keywords: [],
  fee_prefer_international: false,
  fee_follow_links: [],
  minimum_completeness: 85,
  required_fields: [],
  block_publish_if: [],
  follow_links: [],
  band_reference_url: "",
  band_mapping: {},
  actions: [],
  course_name_remove_after: [],
  course_name_remove_year_suffix: false,
  course_name_remove_patterns: [],
  location_replace: {},
  location_allowed_values: [],
  location_reject_values: [],
  study_mode_from_location: false,
  study_mode_online_keywords: [],
};

const STANDARD_FIELDS = [
  "course_name", "degree_level", "duration", "intake_month",
  "international_fee", "ielts_overall", "ielts_band",
  "study_mode", "course_location", "description",
  "entry_requirements", "academic_level", "other_requirement",
];

const API_FIELDS = [
  { std: "course_name", label: "Course Name" },
  { std: "degree_level", label: "Degree Level" },
  { std: "study_mode_raw", label: "Study Mode (raw)" },
  { std: "full_time", label: "Full Time flag" },
  { std: "part_time", label: "Part Time flag" },
  { std: "url_slug", label: "URL Slug" },
  { std: "duration", label: "Duration" },
  { std: "campus", label: "Campus" },
  { std: "description", label: "Description" },
];

// ── Helpers ────────────────────────────────────────────────────────────────

function StringListEditor({
  label, values, onChange, placeholder, helpText
}: {
  label: string;
  values: string[];
  onChange: (v: string[]) => void;
  placeholder?: string;
  helpText?: string;
}) {
  const [draft, setDraft] = useState("");

  const add = () => {
    const v = draft.trim();
    if (v && !values.includes(v)) {
      onChange([...values, v]);
      setDraft("");
    }
  };

  return (
    <div className="space-y-2">
      <Label>{label}</Label>
      {helpText && <p className="text-xs text-muted-foreground">{helpText}</p>}
      <div className="flex gap-2">
        <Input
          value={draft}
          onChange={e => setDraft(e.target.value)}
          placeholder={placeholder}
          onKeyDown={e => { if (e.key === "Enter") { e.preventDefault(); add(); }}}
          className="text-sm"
        />
        <Button type="button" variant="outline" size="sm" onClick={add}>
          <Plus className="h-3 w-3" />
        </Button>
      </div>
      <div className="flex flex-wrap gap-1">
        {values.map((v, i) => (
          <Badge key={i} variant="secondary" className="flex items-center gap-1 max-w-full">
            <span className="truncate max-w-xs text-xs">{v}</span>
            <button onClick={() => onChange(values.filter((_, j) => j !== i))} className="ml-1 hover:text-destructive">
              <Trash2 className="h-2.5 w-2.5" />
            </button>
          </Badge>
        ))}
      </div>
    </div>
  );
}

function FeeRulesEditor({
  label, rules, onChange
}: {
  label: string;
  rules: FeeRule[];
  onChange: (r: FeeRule[]) => void;
}) {
  const addRule = () => onChange([...rules, { amount: 0, keywords: [] }]);

  const updateRule = (i: number, patch: Partial<FeeRule>) => {
    const next = [...rules];
    next[i] = { ...next[i], ...patch };
    onChange(next);
  };

  const removeRule = (i: number) => onChange(rules.filter((_, j) => j !== i));

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <Label>{label}</Label>
        <Button type="button" variant="outline" size="sm" onClick={addRule}>
          <Plus className="h-3 w-3 mr-1" /> Add Band
        </Button>
      </div>
      {rules.length === 0 && (
        <p className="text-xs text-muted-foreground italic">No fee bands defined. Click "Add Band" to create one.</p>
      )}
      {rules.map((rule, i) => (
        <Card key={i} className="border-dashed">
          <CardContent className="pt-4 space-y-3">
            <div className="flex items-center gap-3">
              <div className="flex-1">
                <Label className="text-xs">Annual Fee Amount</Label>
                <Input
                  type="number"
                  value={rule.amount || ""}
                  onChange={e => updateRule(i, { amount: parseFloat(e.target.value) || 0 })}
                  placeholder="e.g. 16500"
                  className="mt-1 text-sm"
                />
              </div>
              <Button type="button" variant="ghost" size="sm" onClick={() => removeRule(i)} className="text-destructive mt-5">
                <Trash2 className="h-4 w-4" />
              </Button>
            </div>
            <StringListEditor
              label="Match Keywords (course name contains any of)"
              values={rule.keywords}
              onChange={kws => updateRule(i, { keywords: kws })}
              placeholder="e.g. Computing"
              helpText="Case-insensitive. Checked most-specific first."
            />
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

function KeyValueEditor({
  label, pairs, onChange, keyPlaceholder, valuePlaceholder, helpText
}: {
  label: string;
  pairs: Record<string, string>;
  onChange: (p: Record<string, string>) => void;
  keyPlaceholder?: string;
  valuePlaceholder?: string;
  helpText?: string;
}) {
  const [draftKey, setDraftKey] = useState("");
  const [draftVal, setDraftVal] = useState("");

  const add = () => {
    const k = draftKey.trim(), v = draftVal.trim();
    if (k) {
      onChange({ ...pairs, [k]: v });
      setDraftKey(""); setDraftVal("");
    }
  };

  return (
    <div className="space-y-2">
      <Label>{label}</Label>
      {helpText && <p className="text-xs text-muted-foreground">{helpText}</p>}
      <div className="flex gap-2">
        <Input value={draftKey} onChange={e => setDraftKey(e.target.value)} placeholder={keyPlaceholder || "Key"} className="text-sm" />
        <Input value={draftVal} onChange={e => setDraftVal(e.target.value)} placeholder={valuePlaceholder || "Value"} className="text-sm"
          onKeyDown={e => { if (e.key === "Enter") { e.preventDefault(); add(); }}} />
        <Button type="button" variant="outline" size="sm" onClick={add}><Plus className="h-3 w-3" /></Button>
      </div>
      {Object.entries(pairs).map(([k, v]) => (
        <div key={k} className="flex items-center gap-2 text-sm bg-muted rounded px-2 py-1">
          <code className="text-xs font-mono text-blue-600">{k}</code>
          <span className="text-muted-foreground">→</span>
          <code className="text-xs font-mono flex-1 truncate">{v}</code>
          <button onClick={() => { const p = { ...pairs }; delete p[k]; onChange(p); }} className="text-destructive hover:opacity-70">
            <Trash2 className="h-3 w-3" />
          </button>
        </div>
      ))}
    </div>
  );
}

// ── Band Mapping Editor ────────────────────────────────────────────────────

function BandMappingEditor({
  mapping, onChange
}: {
  mapping: Record<string, BandSpecUI>;
  onChange: (m: Record<string, BandSpecUI>) => void;
}) {
  const [draftBand, setDraftBand] = useState("");
  const [draftSpec, setDraftSpec] = useState<BandSpecUI>({ ielts_overall: "", ielts_each: "" });

  const addBand = () => {
    const k = draftBand.trim();
    if (!k) return;
    onChange({ ...mapping, [k]: draftSpec });
    setDraftBand("");
    setDraftSpec({ ielts_overall: "", ielts_each: "" });
  };

  const removeBand = (k: string) => {
    const next = { ...mapping };
    delete next[k];
    onChange(next);
  };

  return (
    <div className="space-y-3">
      <div className="text-xs text-muted-foreground">
        Maps band labels (e.g. "Band 2") → IELTS scores. Applied when IELTS is still blank after
        all extractors run. Lookup is case-insensitive.
      </div>

      {/* Existing bands */}
      {Object.entries(mapping).length > 0 && (
        <div className="rounded-lg border overflow-hidden">
          <table className="w-full text-xs">
            <thead className="bg-muted/50">
              <tr>
                <th className="px-3 py-2 text-left font-medium">Band Label</th>
                <th className="px-3 py-2 text-left font-medium">IELTS Overall</th>
                <th className="px-3 py-2 text-left font-medium">IELTS Each</th>
                <th className="px-3 py-2 text-left font-medium">PTE Overall</th>
                <th className="px-3 py-2 text-left font-medium">TOEFL iBT</th>
                <th className="px-2 py-2"></th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(mapping).map(([band, spec]) => (
                <tr key={band} className="border-t">
                  <td className="px-3 py-2 font-mono font-semibold text-blue-700">{band}</td>
                  <td className="px-3 py-2">{spec.ielts_overall || "—"}</td>
                  <td className="px-3 py-2">{spec.ielts_each || "—"}</td>
                  <td className="px-3 py-2">{spec.pte_overall || "—"}</td>
                  <td className="px-3 py-2">{spec.toefl_overall || "—"}</td>
                  <td className="px-2 py-2">
                    <button onClick={() => removeBand(band)} className="text-destructive hover:opacity-70">
                      <Trash2 className="h-3 w-3" />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {Object.entries(mapping).length === 0 && (
        <p className="text-xs text-muted-foreground italic">No band mappings defined.</p>
      )}

      {/* Add new band */}
      <div className="rounded-lg border border-dashed p-3 space-y-2 bg-muted/20">
        <div className="text-xs font-medium text-muted-foreground">Add Band</div>
        <div className="flex gap-2 flex-wrap">
          <Input
            value={draftBand}
            onChange={e => setDraftBand(e.target.value)}
            placeholder='Band label (e.g. "Band 2")'
            className="text-sm w-48"
          />
          <Input
            type="number"
            step="0.5"
            value={draftSpec.ielts_overall ?? ""}
            onChange={e => setDraftSpec(s => ({ ...s, ielts_overall: e.target.value ? parseFloat(e.target.value) : "" }))}
            placeholder="IELTS overall (e.g. 6.5)"
            className="text-sm w-44"
          />
          <Input
            type="number"
            step="0.5"
            value={draftSpec.ielts_each ?? ""}
            onChange={e => setDraftSpec(s => ({ ...s, ielts_each: e.target.value ? parseFloat(e.target.value) : "" }))}
            placeholder="IELTS each (e.g. 6.0)"
            className="text-sm w-44"
          />
          <Input
            type="number"
            value={draftSpec.pte_overall ?? ""}
            onChange={e => setDraftSpec(s => ({ ...s, pte_overall: e.target.value ? parseInt(e.target.value) : "" }))}
            placeholder="PTE (optional)"
            className="text-sm w-32"
          />
          <Button type="button" variant="outline" size="sm" onClick={addBand}>
            <Plus className="h-3 w-3 mr-1" /> Add
          </Button>
        </div>
      </div>
    </div>
  );
}

// ── Browser Actions Editor ─────────────────────────────────────────────────

const ACTION_TYPES: { value: BrowserAction["action_type"]; label: string; hint: string }[] = [
  { value: "click_text",       label: "Click Text",         hint: 'Click first visible element whose text contains this (e.g. "International")' },
  { value: "click_css",        label: "Click CSS",          hint: 'Click first element matching a CSS selector (e.g. "[data-tab=\'intl\']")' },
  { value: "wait_for_text",    label: "Wait for Text",      hint: 'Pause until page contains this text (e.g. "estimated annual tuition")' },
  { value: "wait_for_selector",label: "Wait for Selector",  hint: "Pause until this CSS selector appears on the page" },
  { value: "expand_text",      label: "Expand Section",     hint: 'Click an accordion / "Show more" whose trigger text matches' },
  { value: "scroll_to",        label: "Scroll To",          hint: 'Scroll to a CSS selector or anchor (e.g. "#fees")' },
];

function BrowserActionsEditor({
  actions, onChange
}: {
  actions: BrowserAction[];
  onChange: (a: BrowserAction[]) => void;
}) {
  const [draftType, setDraftType] = useState<BrowserAction["action_type"]>("click_text");
  const [draftValue, setDraftValue] = useState("");

  const addAction = () => {
    const v = draftValue.trim();
    if (!v) return;
    onChange([...actions, { action_type: draftType, value: v }]);
    setDraftValue("");
  };

  const removeAction = (i: number) => onChange(actions.filter((_, j) => j !== i));

  const currentHint = ACTION_TYPES.find(a => a.value === draftType)?.hint || "";

  return (
    <div className="space-y-3">
      <div className="text-xs text-muted-foreground">
        Ordered steps executed in the browser after page load, before HTML is captured.
        Use this to click tabs (e.g. "International"), expand sections, or wait for dynamic content.
      </div>

      {/* Ordered list of existing actions */}
      {actions.length > 0 && (
        <div className="space-y-2">
          {actions.map((action, i) => {
            const typeInfo = ACTION_TYPES.find(a => a.value === action.action_type);
            return (
              <div key={i} className="flex items-center gap-2 bg-muted/40 rounded-lg px-3 py-2 border">
                <GripVertical className="h-4 w-4 text-muted-foreground shrink-0" />
                <span className="text-xs font-medium w-36 shrink-0 text-blue-700">{typeInfo?.label}</span>
                <code className="text-xs flex-1 truncate font-mono">{action.value}</code>
                <button onClick={() => removeAction(i)} className="text-destructive hover:opacity-70 shrink-0">
                  <Trash2 className="h-3 w-3" />
                </button>
              </div>
            );
          })}
        </div>
      )}
      {actions.length === 0 && (
        <p className="text-xs text-muted-foreground italic">No browser actions defined. Add steps below.</p>
      )}

      {/* Add new action */}
      <div className="rounded-lg border border-dashed p-3 space-y-2 bg-muted/20">
        <div className="text-xs font-medium text-muted-foreground">Add Action</div>
        <div className="flex gap-2 flex-wrap items-end">
          <div className="w-48">
            <Select value={draftType} onValueChange={v => setDraftType(v as BrowserAction["action_type"])}>
              <SelectTrigger className="text-sm h-9"><SelectValue /></SelectTrigger>
              <SelectContent>
                {ACTION_TYPES.map(a => (
                  <SelectItem key={a.value} value={a.value}>{a.label}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex-1 min-w-48">
            <Input
              value={draftValue}
              onChange={e => setDraftValue(e.target.value)}
              placeholder={currentHint.slice(0, 55)}
              className="text-sm"
              onKeyDown={e => { if (e.key === "Enter") { e.preventDefault(); addAction(); } }}
            />
          </div>
          <Button type="button" variant="outline" size="sm" onClick={addAction}>
            <Plus className="h-3 w-3 mr-1" /> Add
          </Button>
        </div>
        {currentHint && (
          <p className="text-xs text-muted-foreground">{currentHint}</p>
        )}
      </div>

      {actions.length > 0 && (
        <div className="rounded-lg bg-amber-50 border border-amber-200 p-3 text-xs text-amber-800">
          <strong>Note:</strong> Browser actions only run when the scraper uses a browser session (live site,
          not Wayback HTML). For universities with <code>skip_browser_rescue: true</code> in their YAML
          (e.g. JCU), actions are skipped — use <strong>English Follow Links</strong> or
          <strong> Fee Reject Keywords</strong> instead.
        </div>
      )}
    </div>
  );
}

// ── Main Component ─────────────────────────────────────────────────────────

export default function RecipeEditorPage() {
  const { id } = useParams<{ id: string }>();
  const [, navigate] = useLocation();
  const { toast } = useToast();

  const [uniName, setUniName] = useState("");
  const [scrapeUrl, setScrapeUrl] = useState("");
  const [recipe, setRecipe] = useState<Recipe>(EMPTY_RECIPE);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ count: number; sample: any[] } | null>(null);
  const [testingDiscovery, setTestingDiscovery] = useState(false);
  const [discoveryResult, setDiscoveryResult] = useState<any | null>(null);
  const [showDropped, setShowDropped] = useState(false);
  const [diagnosing, setDiagnosing] = useState(false);
  const [diagnoseResult, setDiagnoseResult] = useState<any | null>(null);
  const [beforeSnapshot, setBeforeSnapshot] = useState<Record<string, any> | null>(null);

  // ── Load ──
  useEffect(() => {
    if (!id) return;
    fetch(`/api/universities/${id}/recipe`, { credentials: "include" })
      .then(r => r.json())
      .then(data => {
        setUniName(data.university_name || "");
        setScrapeUrl(data.scrape_url || "");
        if (data.recipe && Object.keys(data.recipe).length > 0) {
          setRecipe({ ...EMPTY_RECIPE, ...data.recipe });
        }
      })
      .catch(() => toast({ title: "Failed to load recipe", variant: "destructive" }))
      .finally(() => setLoading(false));
  }, [id]);

  // ── Save ──
  const save = useCallback(async () => {
    setSaving(true);
    try {
      const resp = await fetch(`/api/universities/${id}/recipe`, {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(recipe),
      });
      if (!resp.ok) throw new Error(await resp.text());
      toast({ title: "Recipe saved", description: "The scraping recipe has been saved to the database." });
    } catch (e: any) {
      toast({ title: "Save failed", description: e.message, variant: "destructive" });
    } finally {
      setSaving(false);
    }
  }, [id, recipe]);

  // ── Test JSON API ──
  const testApi = useCallback(async () => {
    const api = recipe.api;
    if (!api?.endpoint) {
      toast({ title: "No endpoint", description: "Enter an API endpoint first.", variant: "destructive" });
      return;
    }
    setTesting(true);
    setTestResult(null);
    try {
      const resp = await fetch(api.endpoint, { headers: api.headers || {} });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();

      let items: any = data;
      if (api.root_path) {
        const parts = api.root_path.split(".");
        for (const part of parts) {
          items = items?.[part];
        }
      }
      if (!Array.isArray(items)) throw new Error(`root_path '${api.root_path}' did not resolve to an array`);
      setTestResult({ count: items.length, sample: items.slice(0, 3) });
      toast({ title: `API test OK — ${items.length} courses found` });
    } catch (e: any) {
      toast({ title: "API test failed", description: e.message, variant: "destructive" });
    } finally {
      setTesting(false);
    }
  }, [recipe.api]);

  // ── Diagnose ──
  const runDiagnose = useCallback(async () => {
    setDiagnosing(true);
    setDiagnoseResult(null);
    try {
      const resp = await fetch(`/api/universities/${id}/diagnose`, {
        method: "POST",
        credentials: "include",
      });
      if (!resp.ok) throw new Error(await resp.text());
      setDiagnoseResult(await resp.json());
    } catch (e: any) {
      toast({ title: "Diagnostics failed", description: e.message, variant: "destructive" });
    } finally {
      setDiagnosing(false);
    }
  }, [id]);

  // ── Apply Fix from diagnostics ──
  const applyFix = useCallback((patch: Record<string, any>) => {
    // Capture before-state so we can show a before/after comparison after next Diagnose
    if (diagnoseResult?.phase1?.field_completion) {
      setBeforeSnapshot(diagnoseResult.phase1.field_completion);
    }
    patchRecipe(patch);
    toast({
      title: "Fix applied",
      description: "Recipe updated — save it, run a new scrape, then click Diagnose to verify improvement.",
    });
  }, [diagnoseResult]);

  // ── Test Discovery ──
  const testDiscovery = useCallback(async () => {
    setTestingDiscovery(true);
    setDiscoveryResult(null);
    setShowDropped(false);
    try {
      const payload: Record<string, any> = {
        seed_urls: recipe.seed_urls.filter(Boolean),
        must_contain: recipe.must_contain.filter(Boolean),
        block_url_patterns: recipe.block_url_patterns.filter(Boolean),
        expected_min_courses: recipe.expected_min_courses || null,
        time_limit_s: 60,
      };
      if (recipe.api?.endpoint) {
        payload.json_api = {
          endpoint: recipe.api.endpoint,
          root_path: recipe.api.root_path,
          course_url_template: recipe.api.course_url_template,
          method: recipe.api.method,
          fields: recipe.api.fields,
          headers: recipe.api.headers,
        };
      }
      const resp = await fetch(`/api/universities/${id}/recipe/test`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!resp.ok) throw new Error(await resp.text());
      const data = await resp.json();
      setDiscoveryResult(data);
    } catch (e: any) {
      toast({ title: "Test failed", description: e.message, variant: "destructive" });
    } finally {
      setTestingDiscovery(false);
    }
  }, [id, recipe]);

  // ── Patch helpers ──
  const patchRecipe = (patch: Partial<Recipe>) => setRecipe(r => ({ ...r, ...patch }));
  const patchApi = (patch: Partial<ApiConfig>) =>
    setRecipe(r => ({ ...r, api: { ...EMPTY_API, ...(r.api || {}), ...patch } }));
  const patchIelts = (patch: Partial<Recipe["ielts"]>) =>
    setRecipe(r => ({ ...r, ielts: { overall_regex: "", band_regex: "", source_xpath: "", ...(r.ielts || {}), ...patch } }));
  const patchIntake = (patch: Partial<Recipe["intake"]>) =>
    setRecipe(r => ({ ...r, intake: { xpath: "", regex: "", month_map: {}, ...(r.intake || {}), ...patch } }));
  const patchCampus = (patch: Partial<Recipe["campus"]>) =>
    setRecipe(r => ({ ...r, campus: { default_city: "", valid_campuses: [], online_only_reject: false, ...(r.campus || {}), ...patch } }));
  const patchSelector = (field: string, patch: Partial<FieldSelector>) =>
    setRecipe(r => ({ ...r, selectors: { ...r.selectors, [field]: { ...(r.selectors[field] || {}), ...patch } } }));

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-muted-foreground">Loading recipe…</div>
      </div>
    );
  }

  return (
    <div className="max-w-5xl mx-auto p-6 space-y-6">
      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div className="space-y-1">
          <button
            onClick={() => navigate(`/universities/${id}`)}
            className="flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
          >
            <ArrowLeft className="h-4 w-4" /> Back to university
          </button>
          <h1 className="text-2xl font-bold">Advanced Scraping Recipe</h1>
          <p className="text-muted-foreground text-sm">
            {uniName} · <span className="font-mono text-xs">{scrapeUrl}</span>
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Badge variant={recipe.discovery_strategy === "json_api" ? "default" : "secondary"}>
            {recipe.discovery_strategy}
          </Badge>
          <Button variant="outline" onClick={runDiagnose} disabled={diagnosing || saving}>
            {diagnosing
              ? <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              : <Stethoscope className="h-4 w-4 mr-2" />}
            {diagnosing ? "Diagnosing…" : "Diagnose"}
          </Button>
          <Button variant="outline" onClick={testDiscovery} disabled={testingDiscovery || saving}>
            <FlaskConical className="h-4 w-4 mr-2" />
            {testingDiscovery ? "Testing…" : "Test Discovery"}
          </Button>
          <Button onClick={save} disabled={saving || testingDiscovery}>
            <Save className="h-4 w-4 mr-2" />
            {saving ? "Saving…" : "Save Recipe"}
          </Button>
        </div>
      </div>

      {/* ── Discovery Test Results ──────────────────────────────────────── */}
      {testingDiscovery && (
        <Card className="border-blue-200 bg-blue-50">
          <CardContent className="pt-4 pb-3">
            <div className="flex items-center gap-2 text-blue-700 text-sm">
              <FlaskConical className="h-4 w-4 animate-pulse" />
              <span>Running discovery test — opening seed URLs and counting course links…</span>
              <span className="text-xs text-blue-500">(up to 60 s)</span>
            </div>
          </CardContent>
        </Card>
      )}

      {discoveryResult && !testingDiscovery && (() => {
        const r = discoveryResult;
        const verdict = r.status as "PASS" | "WARN" | "FAIL" | "NOT_CONFIGURED";

        // NOT_CONFIGURED: no seeds and no API endpoint — the recipe hasn't been
        // set up yet.  Show a neutral gray card, not a red FAIL card.
        if (verdict === "NOT_CONFIGURED") {
          return (
            <Card className="border-gray-200 bg-gray-50">
              <CardHeader className="pb-3 pt-4">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <Info className="h-5 w-5 text-gray-400" />
                    <CardTitle className="text-base">Discovery Test Result</CardTitle>
                    <span className="text-xs font-bold px-2 py-0.5 rounded-full bg-gray-200 text-gray-600">NOT CONFIGURED</span>
                  </div>
                  <button onClick={() => setDiscoveryResult(null)} className="text-xs text-muted-foreground hover:text-foreground">Dismiss</button>
                </div>
              </CardHeader>
              <CardContent className="pt-0 space-y-3">
                <p className="text-sm text-gray-600">
                  No seed URLs or API endpoint configured — there was nothing to test.
                </p>
                <p className="text-sm text-gray-500">
                  Add course listing page URLs under <strong>Discovery Seed URLs</strong> below
                  (e.g. <code className="text-xs bg-gray-100 px-1 rounded">https://www.example.com/study/undergraduate/courses</code>),
                  then click <strong>Test Discovery</strong> again.
                </p>
              </CardContent>
            </Card>
          );
        }

        const colors = {
          PASS: { card: "border-green-200 bg-green-50", badge: "bg-green-100 text-green-800", icon: <CheckCircle2 className="h-5 w-5 text-green-600" /> },
          WARN: { card: "border-yellow-200 bg-yellow-50", badge: "bg-yellow-100 text-yellow-800", icon: <AlertTriangle className="h-5 w-5 text-yellow-600" /> },
          FAIL: { card: "border-red-200 bg-red-50",   badge: "bg-red-100 text-red-800",   icon: <XCircle className="h-5 w-5 text-red-600" /> },
        }[verdict as "PASS" | "WARN" | "FAIL"];

        return (
          <Card className={colors.card}>
            <CardHeader className="pb-3 pt-4">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  {colors.icon}
                  <CardTitle className="text-base">Discovery Test Result</CardTitle>
                  <span className={`text-xs font-bold px-2 py-0.5 rounded-full ${colors.badge}`}>{verdict}</span>
                </div>
                <span className="text-xs text-muted-foreground">{r.elapsed_s}s</span>
              </div>
            </CardHeader>
            <CardContent className="space-y-4 pt-0">

              {/* Overall counts */}
              <div className="grid grid-cols-3 gap-3">
                <div className="bg-white rounded-lg p-3 text-center border">
                  <div className="text-2xl font-bold">{r.raw_found}</div>
                  <div className="text-xs text-muted-foreground">Raw links found</div>
                </div>
                <div className="bg-white rounded-lg p-3 text-center border">
                  <div className="text-2xl font-bold">{r.after_filter_count}</div>
                  <div className="text-xs text-muted-foreground">After filters</div>
                </div>
                <div className="bg-white rounded-lg p-3 text-center border">
                  <div className={`text-2xl font-bold ${verdict === "PASS" ? "text-green-600" : verdict === "WARN" ? "text-yellow-600" : "text-red-600"}`}>
                    {r.expected_min_courses ? `${r.after_filter_count} / ${r.expected_min_courses}` : r.after_filter_count}
                  </div>
                  <div className="text-xs text-muted-foreground">{r.expected_min_courses ? "Found / Expected" : "Total after filter"}</div>
                </div>
              </div>

              {/* Discovery incomplete banner */}
              {r.discovery_incomplete && (
                <div className="rounded-lg border border-red-300 bg-red-50 p-3 flex gap-3 items-start">
                  <XCircle className="h-5 w-5 text-red-500 mt-0.5 shrink-0" />
                  <div className="flex-1 min-w-0">
                    <div className="font-semibold text-red-800 text-sm">Discovery incomplete</div>
                    <div className="text-sm text-red-700 mt-0.5">{r.discovery_incomplete_message}</div>
                    <div className="text-xs text-red-600 mt-1.5 font-medium">
                      Fix the seed URLs before running a full scrape. If you want to run extraction anyway,
                      lower <code>expected_min_courses</code> or add the correct course listing page URLs above.
                    </div>
                  </div>
                </div>
              )}

              {/* Configured seed URLs tested */}
              {(r.configured_seed_urls?.length > 0 || r.seed_results?.length > 0) && (
                <div>
                  <div className="text-xs font-semibold text-muted-foreground mb-1 uppercase tracking-wide">
                    Seed URLs tested ({r.configured_seed_urls?.length ?? r.seed_results?.length})
                  </div>
                  <div className="text-xs text-muted-foreground mb-2">
                    These are the exact URLs submitted for testing. The scraper visits each one and counts course links found.
                  </div>
                  <div className="space-y-1">
                    {(r.seed_results?.length > 0 ? r.seed_results : r.configured_seed_urls?.map((u: string) => ({ url: u, raw_found: 0, status: "pending" }))).map((sr: any, i: number) => (
                      <div key={i} className="flex items-center gap-2 bg-white rounded p-2 border text-sm">
                        <span className={`text-xs font-bold px-1.5 py-0.5 rounded shrink-0 ${
                          sr.status === "ok" ? "bg-green-100 text-green-700"
                          : sr.status === "blocked_403" ? "bg-orange-100 text-orange-700"
                          : sr.status === "pending" ? "bg-gray-100 text-gray-500"
                          : "bg-red-100 text-red-700"
                        }`}>
                          {sr.status === "ok" ? "OK" : sr.status === "blocked_403" ? "403 (CF)" : sr.status === "pending" ? "–" : sr.status.toUpperCase()}
                        </span>
                        <span className="font-mono text-xs flex-1 min-w-0 break-all">{sr.url}</span>
                        {sr.status !== "pending" && (
                          <span className={`font-bold text-sm shrink-0 ${sr.raw_found === 0 ? "text-red-600" : "text-green-700"}`}>
                            {sr.raw_found}
                          </span>
                        )}
                        <span className="text-xs text-muted-foreground shrink-0">
                          {sr.status === "blocked_403" ? "blocked (browser scrape will bypass)" : sr.status !== "pending" ? "links" : ""}
                        </span>
                      </div>
                    ))}
                  </div>
                  {r.seed_results?.some((sr: any) => sr.raw_found === 0 && sr.status === "ok") && (
                    <div className="mt-2 text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded p-2">
                      ⚠ One or more seed URLs returned 0 links via HTTP.
                      This often means the page is JS-rendered (links are loaded client-side) — the browser scrape will still work.
                      If this is not a JS site, verify the URL is the correct course listing page, not a hub/overview page.
                    </div>
                  )}
                </div>
              )}

              {/* JSON API result */}
              {r.api_result && (
                <div>
                  <div className="text-xs font-semibold text-muted-foreground mb-1 uppercase tracking-wide">JSON API</div>
                  <div className="bg-white rounded p-3 border text-sm space-y-1">
                    <div className="flex items-center gap-2">
                      <span className={`text-xs font-bold px-1.5 py-0.5 rounded ${r.api_result.status === "ok" ? "bg-green-100 text-green-700" : "bg-red-100 text-red-700"}`}>
                        {r.api_result.status.toUpperCase()}
                      </span>
                      <span>{r.api_result.records_found ?? 0} records returned</span>
                      {r.api_result.urls_generated != null && (
                        <span className="text-muted-foreground">· {r.api_result.urls_generated} URLs generated</span>
                      )}
                    </div>
                    {r.api_result.root_path_used && (
                      <div className="text-xs text-muted-foreground">root_path: <code>{r.api_result.root_path_used}</code></div>
                    )}
                  </div>
                </div>
              )}

              {/* Filter stats */}
              {(r.dropped_count > 0 || r.filters_applied?.must_contain?.length > 0) && (
                <div>
                  <button
                    onClick={() => setShowDropped(v => !v)}
                    className="flex items-center gap-1 text-xs font-semibold text-muted-foreground uppercase tracking-wide hover:text-foreground"
                  >
                    {showDropped ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
                    Filters ({r.dropped_count} dropped, {r.drop_pct}%)
                  </button>
                  {showDropped && (
                    <div className="mt-1 space-y-1">
                      {r.filters_applied?.must_contain?.length > 0 && (
                        <div className="text-xs bg-white rounded p-2 border">
                          <span className="font-medium">must_contain:</span>{" "}
                          {r.filters_applied.must_contain.map((m: string) => (
                            <code key={m} className="bg-gray-100 px-1 rounded mr-1">{m}</code>
                          ))}
                        </div>
                      )}
                      {r.dropped_samples?.length > 0 && (
                        <div className="text-xs bg-white rounded p-2 border">
                          <div className="font-medium mb-1">Sample dropped URLs:</div>
                          {r.dropped_samples.slice(0, 5).map((u: string, i: number) => (
                            <div key={i} className="font-mono text-xs truncate text-red-700">{u}</div>
                          ))}
                        </div>
                      )}
                      {r.kept_samples?.length > 0 && (
                        <div className="text-xs bg-white rounded p-2 border">
                          <div className="font-medium mb-1">Sample kept URLs:</div>
                          {r.kept_samples.slice(0, 5).map((u: string, i: number) => (
                            <div key={i} className="font-mono text-xs truncate text-green-700">{u}</div>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )}

              {/* Warnings */}
              {r.warnings?.length > 0 && (
                <div className="space-y-1">
                  {r.warnings.map((w: string, i: number) => (
                    <div key={i} className="flex gap-2 text-xs bg-yellow-100 text-yellow-800 rounded p-2 border border-yellow-200">
                      <AlertTriangle className="h-3 w-3 mt-0.5 flex-shrink-0" />
                      <span>{w}</span>
                    </div>
                  ))}
                </div>
              )}

              {/* Recommendations */}
              {r.recommendations?.length > 0 && (
                <div className="space-y-1">
                  <div className="text-xs font-semibold text-muted-foreground uppercase tracking-wide">Recommendations</div>
                  {r.recommendations.map((rec: string, i: number) => (
                    <div key={i} className="flex gap-2 text-xs bg-white rounded p-2 border text-foreground">
                      <Zap className="h-3 w-3 mt-0.5 flex-shrink-0 text-blue-500" />
                      <span>{rec}</span>
                    </div>
                  ))}
                </div>
              )}

              <div className="flex justify-end pt-1">
                <button onClick={() => setDiscoveryResult(null)} className="text-xs text-muted-foreground hover:text-foreground">
                  Dismiss
                </button>
              </div>
            </CardContent>
          </Card>
        );
      })()}

      {/* ── Diagnosing indicator ─────────────────────────────────────────── */}
      {diagnosing && (
        <Card className="border-purple-200 bg-purple-50">
          <CardContent className="pt-4 pb-3">
            <div className="flex items-center gap-2 text-purple-700 text-sm">
              <Loader2 className="h-4 w-4 animate-spin" />
              <span>Running diagnostics — analysing last scrape job and probing live site…</span>
              <span className="text-xs text-purple-500">(up to 20 s)</span>
            </div>
          </CardContent>
        </Card>
      )}

      {/* ── Diagnostic Report ───────────────────────────────────────────────── */}
      {diagnoseResult && !diagnosing && (() => {
        const dr = diagnoseResult;
        const p1 = dr.phase1 || {};
        const p2 = dr.phase2 || {};
        const recs: any[] = dr.recommendations || [];
        const summary = dr.summary || {};
        const detected = p2.detected || {};

        const FIELD_LABELS: Record<string, string> = {
          international_fee: "International fee",
          ielts_overall: "IELTS",
          pte_overall: "PTE",
          toefl_overall: "TOEFL",
          study_mode: "Study mode",
          degree_level: "Degree level",
          duration: "Duration",
          academic_level: "Academic level",
          intake_months: "Intakes",
        };

        const SEVERITY_COLORS = {
          critical: { border: "border-red-200", bg: "bg-red-50", badge: "bg-red-100 text-red-800", icon: <XCircle className="h-4 w-4 text-red-600 shrink-0 mt-0.5" /> },
          warning:  { border: "border-yellow-200", bg: "bg-yellow-50", badge: "bg-yellow-100 text-yellow-800", icon: <AlertTriangle className="h-4 w-4 text-yellow-600 shrink-0 mt-0.5" /> },
        };

        return (
          <Card className="border-purple-200">
            <CardHeader className="pb-3 pt-4">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <Stethoscope className="h-5 w-5 text-purple-600" />
                  <CardTitle className="text-base">Diagnostic Report</CardTitle>
                  {summary.critical_count > 0 && (
                    <span className="text-xs font-bold px-2 py-0.5 rounded-full bg-red-100 text-red-800">
                      {summary.critical_count} critical
                    </span>
                  )}
                  {summary.warning_count > 0 && (
                    <span className="text-xs font-bold px-2 py-0.5 rounded-full bg-yellow-100 text-yellow-800">
                      {summary.warning_count} warning
                    </span>
                  )}
                  {summary.auto_fix_available > 0 && (
                    <span className="text-xs font-bold px-2 py-0.5 rounded-full bg-purple-100 text-purple-800">
                      {summary.auto_fix_available} auto-fix available
                    </span>
                  )}
                  {recs.length === 0 && (
                    <span className="text-xs font-bold px-2 py-0.5 rounded-full bg-green-100 text-green-800">
                      No issues found
                    </span>
                  )}
                </div>
                <button
                  onClick={() => setDiagnoseResult(null)}
                  className="text-xs text-muted-foreground hover:text-foreground"
                >
                  Dismiss
                </button>
              </div>
              {p1.completed_at && (
                <p className="text-xs text-muted-foreground mt-1">
                  Based on scrape completed {new Date(p1.completed_at).toLocaleDateString()} ·{" "}
                  {p1.courses_analysed} courses analysed ({p1.total_found} discovered, {p1.imported} imported)
                  {p2.cloudflare_blocked && (
                    <span className="ml-2 inline-flex items-center gap-1 text-orange-600">
                      <WifiOff className="h-3 w-3" /> Live probe blocked by Cloudflare
                    </span>
                  )}
                </p>
              )}
            </CardHeader>

            <CardContent className="space-y-5 pt-0">

              {/* Phase 1 — Field Completion + Quality */}
              {p1.status === "ok" && p1.field_completion && (
                <div>
                  <div className="flex items-center gap-1 text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-2">
                    <TrendingUp className="h-3 w-3" /> Field Completion &amp; Quality
                  </div>
                  <div className="grid grid-cols-2 gap-x-6 gap-y-3">
                    {Object.entries(p1.field_completion as Record<string, any>).map(([field, stat]) => {
                      const pct = Math.round((stat.pct || 0) * 100);
                      const qPct = stat.quality_pct != null ? Math.round(stat.quality_pct * 100) : null;
                      const completionColor = pct >= 80 ? "bg-green-500" : pct >= 50 ? "bg-yellow-500" : "bg-red-500";
                      // Quality bar: only show when field has quality data and is mostly filled
                      const hasQuality = qPct != null && stat.count > 0;
                      const qualityColor = !hasQuality ? "" : qPct >= 90 ? "bg-green-400" : qPct >= 70 ? "bg-yellow-400" : "bg-orange-500";
                      const qualityBad = hasQuality && qPct < 90 && stat.quality_issues > 0;
                      return (
                        <div key={field}>
                          <div className="flex justify-between text-xs mb-0.5">
                            <span className="text-muted-foreground">{FIELD_LABELS[field] ?? field}</span>
                            <div className="flex items-center gap-2">
                              {hasQuality && qualityBad && (
                                <span className="text-orange-600 font-medium" title={stat.quality_label}>
                                  {qPct}% quality
                                </span>
                              )}
                              <span className={`font-medium ${pct >= 80 ? "text-green-700" : pct >= 50 ? "text-yellow-700" : "text-red-700"}`}>
                                {pct}% <span className="font-normal text-muted-foreground">({stat.count}/{stat.count + stat.missing})</span>
                              </span>
                            </div>
                          </div>
                          {/* Completion bar */}
                          <div className="h-1.5 rounded-full bg-gray-100 overflow-hidden mb-0.5">
                            <div className={`h-full rounded-full ${completionColor}`} style={{ width: `${pct}%` }} />
                          </div>
                          {/* Quality bar (only when filled data exists and quality differs from completion) */}
                          {hasQuality && pct > 10 && (
                            <div className="h-1 rounded-full bg-gray-100 overflow-hidden" title={`Quality: ${stat.quality_label ?? "valid values"}`}>
                              <div
                                className={`h-full rounded-full ${qualityColor}`}
                                style={{ width: `${pct}%` }}
                              >
                                <div
                                  className="h-full bg-gray-200 rounded-full float-right"
                                  style={{ width: `${Math.max(0, 100 - qPct)}%` }}
                                />
                              </div>
                            </div>
                          )}
                          {hasQuality && qualityBad && (
                            <p className="text-xs text-orange-600 mt-0.5">
                              {stat.quality_issues} filled but incorrect
                            </p>
                          )}
                        </div>
                      );
                    })}
                  </div>
                  {/* Study mode breakdown */}
                  {p1.study_mode_breakdown && (
                    <div className="mt-2 flex gap-3 text-xs text-muted-foreground">
                      <span>Study mode breakdown:</span>
                      {Object.entries(p1.study_mode_breakdown as Record<string, number>).map(([mode, n]) => (
                        <span key={mode} className="font-medium text-foreground">{mode}: {n}</span>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {/* Top 10 Broken Courses */}
              {p1.top_broken_courses && p1.top_broken_courses.length > 0 && (
                <div>
                  <div className="flex items-center gap-1 text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-2">
                    <XCircle className="h-3 w-3 text-red-500" /> Most Broken Courses
                  </div>
                  <div className="space-y-1.5">
                    {(p1.top_broken_courses as any[]).map((course: any, idx: number) => (
                      <div key={idx} className="flex items-start gap-2 rounded-lg border border-gray-100 bg-gray-50 px-3 py-2">
                        <span className="text-xs font-bold text-muted-foreground w-4 shrink-0 mt-0.5">{idx + 1}.</span>
                        <div className="flex-1 min-w-0">
                          <p className="text-xs font-medium text-foreground truncate" title={course.course_name}>
                            {course.course_name}
                          </p>
                          <div className="flex flex-wrap gap-1 mt-1">
                            {(course.issues as any[]).map((issue: any, i: number) => (
                              <span
                                key={i}
                                className={`inline-flex text-xs px-1.5 py-0.5 rounded font-medium ${
                                  issue.severity === "critical"
                                    ? "bg-red-100 text-red-800"
                                    : "bg-yellow-100 text-yellow-800"
                                }`}
                              >
                                {issue.label}
                              </span>
                            ))}
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {p1.status === "no_completed_job" && (
                <p className="text-sm text-muted-foreground">
                  No completed scrape job found. Run a scrape first, then click Diagnose.
                </p>
              )}

              {/* Phase 2 — Live Probe */}
              {p2.status === "ok" && (detected.fee_link_texts?.length > 0 || detected.english_link_texts?.length > 0 || detected.pdf_urls?.length > 0 || detected.has_tab_layout || detected.has_online_delivery) && (
                <div>
                  <div className="flex items-center gap-1 text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-2">
                    <Globe className="h-3 w-3" /> Live Site Detected
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {detected.fee_link_texts?.length > 0 && (
                      <span className="inline-flex items-center gap-1 text-xs bg-blue-50 border border-blue-200 text-blue-800 px-2 py-0.5 rounded-full">
                        <DollarSign className="h-3 w-3" /> Fee pages: {detected.fee_link_texts.slice(0, 2).map((t: string) => `"${t}"`).join(", ")}
                      </span>
                    )}
                    {detected.english_link_texts?.length > 0 && (
                      <span className="inline-flex items-center gap-1 text-xs bg-blue-50 border border-blue-200 text-blue-800 px-2 py-0.5 rounded-full">
                        <BookOpen className="h-3 w-3" /> English pages: {detected.english_link_texts.slice(0, 2).map((t: string) => `"${t}"`).join(", ")}
                      </span>
                    )}
                    {detected.pdf_urls?.length > 0 && (
                      <span className="inline-flex items-center gap-1 text-xs bg-blue-50 border border-blue-200 text-blue-800 px-2 py-0.5 rounded-full">
                        <ExternalLink className="h-3 w-3" /> {detected.pdf_urls.length} PDFs detected
                      </span>
                    )}
                    {detected.has_tab_layout && (
                      <span className="inline-flex items-center gap-1 text-xs bg-orange-50 border border-orange-200 text-orange-800 px-2 py-0.5 rounded-full">
                        <Database className="h-3 w-3" /> Tab-based layout
                      </span>
                    )}
                    {detected.has_online_delivery && (
                      <span className="inline-flex items-center gap-1 text-xs bg-blue-50 border border-blue-200 text-blue-800 px-2 py-0.5 rounded-full">
                        <Globe className="h-3 w-3" /> Online delivery detected
                      </span>
                    )}
                  </div>
                </div>
              )}

              {/* Phase 3 — Issues & Fixes */}
              {recs.length > 0 && (
                <div>
                  <div className="flex items-center gap-1 text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-2">
                    <Wand2 className="h-3 w-3" /> Issues &amp; Suggested Fixes
                  </div>
                  <div className="space-y-3">
                    {recs.map((rec: any) => {
                      const sev = SEVERITY_COLORS[rec.severity as "critical" | "warning"] ?? SEVERITY_COLORS.warning;
                      const hasFix = rec.fix && rec.fix.recipe_patch;
                      const isGuidanceOnly = rec.fix && !rec.fix.recipe_patch;
                      return (
                        <div key={rec.id} className={`rounded-lg border p-3 ${sev.border} ${sev.bg}`}>
                          <div className="flex items-start gap-2">
                            {sev.icon}
                            <div className="flex-1 min-w-0">
                              <div className="flex items-center gap-2 flex-wrap">
                                <span className="text-sm font-semibold">{rec.title}</span>
                                <span className={`text-xs px-1.5 py-0.5 rounded font-medium ${sev.badge}`}>
                                  {rec.severity}
                                </span>
                                <span className="text-xs text-muted-foreground">
                                  {Math.round((rec.confidence || 0) * 100)}% confidence
                                </span>
                              </div>
                              <p className="text-xs text-muted-foreground mt-0.5">{rec.description}</p>
                              {rec.root_cause && (
                                <p className="text-xs mt-1">
                                  <span className="font-medium">Root cause: </span>{rec.root_cause}
                                </p>
                              )}
                              {/* Impact estimate */}
                              {rec.impact_estimate && rec.impact_estimate.courses_affected > 0 && (
                                <div className="mt-2 rounded-md bg-white/70 border border-current/10 px-2.5 py-1.5 flex flex-wrap gap-x-4 gap-y-1 text-xs">
                                  <span>
                                    <span className="font-semibold text-foreground">{rec.impact_estimate.courses_affected}</span>
                                    <span className="text-muted-foreground"> courses affected</span>
                                  </span>
                                  {rec.impact_estimate.delta != null && rec.impact_estimate.delta > 0 && (
                                    <span>
                                      <span className="text-muted-foreground">Completeness </span>
                                      <span className="font-semibold text-foreground">{rec.impact_estimate.overall_completeness_before}%</span>
                                      <span className="text-muted-foreground"> → </span>
                                      <span className="font-semibold text-green-700">{rec.impact_estimate.overall_completeness_after}%</span>
                                      <span className="text-green-700 font-bold ml-1">+{rec.impact_estimate.delta}pp</span>
                                    </span>
                                  )}
                                  {rec.impact_estimate.note && (
                                    <span className="text-muted-foreground italic">{rec.impact_estimate.note}</span>
                                  )}
                                </div>
                              )}
                              {rec.fix && (
                                <div className="mt-2 flex items-center gap-2 flex-wrap">
                                  <p className="text-xs">
                                    <span className="font-medium">Suggested fix: </span>
                                    {rec.fix.description}
                                  </p>
                                  {hasFix && (
                                    <Button
                                      size="sm"
                                      variant="default"
                                      className="h-6 text-xs px-2 py-0 bg-purple-600 hover:bg-purple-700"
                                      onClick={() => applyFix(rec.fix.recipe_patch)}
                                    >
                                      <Wand2 className="h-3 w-3 mr-1" />
                                      Apply Fix
                                    </Button>
                                  )}
                                  {isGuidanceOnly && (
                                    <span className="text-xs text-muted-foreground italic">
                                      (manual — see {
                                        rec.fix.type === "browser_action" ? "Browser Actions tab" :
                                        rec.fix.type === "field_selector" ? "Field Selectors tab" :
                                        rec.fix.type === "seed_urls" ? "Discovery tab" :
                                        rec.fix.type === "campus_allowlist" ? "Campus tab" :
                                        rec.fix.type === "band_reference_url" ? "IELTS & Intake tab" :
                                        rec.fix.type === "fee_selector" ? "Field Selectors or Fee Rules tab" :
                                        rec.fix.type === "prefer_blended_over_on_campus" ? "Field Selectors tab" :
                                        "relevant tab"
                                      })
                                    </span>
                                  )}
                                </div>
                              )}
                            </div>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}

              {recs.length === 0 && p1.status === "ok" && (
                <div className="flex items-center gap-2 text-green-700 text-sm">
                  <CheckCircle2 className="h-4 w-4" />
                  <span>No extraction issues detected. The recipe looks well-configured for this university.</span>
                </div>
              )}

              {/* ── Before / After Verification ──────────────────────────────── */}
              {beforeSnapshot && p1.status === "ok" && p1.field_completion && (() => {
                const before = beforeSnapshot;
                const after = p1.field_completion as Record<string, any>;
                const allFields = Array.from(new Set([...Object.keys(before), ...Object.keys(after)]));

                // A field "improved" if completion OR quality went up meaningfully
                const fillImproved  = allFields.filter(f => (after[f]?.pct ?? 0) > (before[f]?.pct ?? 0) + 0.04);
                const qualImproved  = allFields.filter(f =>
                  after[f]?.quality_pct != null &&
                  before[f]?.quality_pct != null &&
                  (after[f].quality_pct - before[f].quality_pct) > 0.04
                );
                const anyImproved   = Array.from(new Set([...fillImproved, ...qualImproved]));
                const fillWorsened  = allFields.filter(f => (after[f]?.pct ?? 0) < (before[f]?.pct ?? 0) - 0.04);

                const verdict =
                  anyImproved.length >= allFields.length * 0.5
                    ? { label: "Successful", icon: "✅", className: "bg-green-50 border-green-300 text-green-800" }
                    : anyImproved.length > 0
                    ? { label: "Partially Successful", icon: "⚠️", className: "bg-yellow-50 border-yellow-300 text-yellow-800" }
                    : { label: "No Improvement Detected", icon: "❌", className: "bg-red-50 border-red-300 text-red-800" };

                const FIELD_LABELS2: Record<string, string> = {
                  international_fee: "International fee", ielts_overall: "IELTS",
                  pte_overall: "PTE", toefl_overall: "TOEFL", study_mode: "Study mode",
                  degree_level: "Degree level", duration: "Duration",
                  academic_level: "Academic level", intake_months: "Intakes",
                  course_location: "Location",
                };

                // Helper: delta badge
                const DeltaBadge = ({ val }: { val: number }) =>
                  val === 0
                    ? <span className="text-muted-foreground">—</span>
                    : <span className={`font-bold ${val > 0 ? "text-green-700" : "text-red-700"}`}>
                        {val > 0 ? "+" : ""}{val}pp
                      </span>;

                // Helper: cell showing fill% and optionally quality%
                const Cell = ({ stat, dim }: { stat: any; dim?: "fill" | "quality" }) => {
                  if (!stat) return <span className="text-muted-foreground">—</span>;
                  if (dim === "quality") {
                    if (stat.quality_pct == null) return <span className="text-muted-foreground text-xs italic">n/a</span>;
                    const q = Math.round(stat.quality_pct * 100);
                    return <span className={q >= 90 ? "text-green-700 font-semibold" : q >= 70 ? "text-yellow-700 font-semibold" : "text-orange-700 font-semibold"}>{q}%</span>;
                  }
                  const p = Math.round((stat.pct ?? 0) * 100);
                  return <span className={p >= 80 ? "text-green-700 font-semibold" : p >= 50 ? "text-yellow-700 font-semibold" : "text-red-700 font-semibold"}>{p}%</span>;
                };

                // Only show quality rows when at least one field has quality data
                const hasAnyQuality = allFields.some(f => after[f]?.quality_pct != null || before[f]?.quality_pct != null);

                return (
                  <div>
                    <div className="flex items-center gap-1 text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-2">
                      <TrendingUp className="h-3 w-3" /> Fix Verification — Before vs After
                    </div>

                    {/* Verdict banner */}
                    <div className={`rounded-lg border px-3 py-2.5 mb-3 flex items-center gap-2 text-sm font-semibold ${verdict.className}`}>
                      <span className="text-base">{verdict.icon}</span>
                      <span>{verdict.label}</span>
                      <span className="font-normal text-xs ml-1">
                        {anyImproved.length > 0
                          ? `${anyImproved.length} field${anyImproved.length !== 1 ? "s" : ""} improved${fillWorsened.length > 0 ? ` · ${fillWorsened.length} worsened` : ""}`
                          : "No fields improved after applying this fix"}
                      </span>
                    </div>

                    {/* Comparison table */}
                    <div className="overflow-x-auto rounded-lg border border-gray-200">
                      <table className="w-full text-xs">
                        <thead>
                          <tr className="bg-gray-50 border-b border-gray-200">
                            <th className="text-left px-3 py-2 text-muted-foreground font-medium w-32">Field</th>
                            <th className="text-center px-2 py-2 text-muted-foreground font-medium" colSpan={2}>Before Fix</th>
                            <th className="text-center px-2 py-2 text-muted-foreground font-medium" colSpan={2}>After Fix</th>
                            <th className="text-center px-2 py-2 text-muted-foreground font-medium" colSpan={2}>Change</th>
                          </tr>
                          <tr className="bg-gray-50/60 border-b border-gray-200 text-muted-foreground/70">
                            <th className="px-3 py-1" />
                            <th className="px-2 py-1 font-normal text-center">Fill</th>
                            {hasAnyQuality && <th className="px-2 py-1 font-normal text-center">Quality</th>}
                            <th className="px-2 py-1 font-normal text-center">Fill</th>
                            {hasAnyQuality && <th className="px-2 py-1 font-normal text-center">Quality</th>}
                            <th className="px-2 py-1 font-normal text-center">Fill Δ</th>
                            {hasAnyQuality && <th className="px-2 py-1 font-normal text-center">Quality Δ</th>}
                          </tr>
                        </thead>
                        <tbody>
                          {allFields.map(f => {
                            const bFill = Math.round((before[f]?.pct ?? 0) * 100);
                            const aFill = Math.round((after[f]?.pct ?? 0) * 100);
                            const fillDelta = aFill - bFill;
                            const bQual = before[f]?.quality_pct != null ? Math.round(before[f].quality_pct * 100) : null;
                            const aQual = after[f]?.quality_pct != null  ? Math.round(after[f].quality_pct * 100)  : null;
                            const qualDelta = bQual != null && aQual != null ? aQual - bQual : null;
                            const rowImproved = fillDelta > 4 || (qualDelta != null && qualDelta > 4);
                            const rowWorsened = fillDelta < -4;
                            return (
                              <tr
                                key={f}
                                className={`border-b border-gray-100 last:border-0 ${rowImproved ? "bg-green-50" : rowWorsened ? "bg-red-50" : ""}`}
                              >
                                <td className="px-3 py-2 text-muted-foreground font-medium">
                                  {FIELD_LABELS2[f] ?? f}
                                </td>
                                {/* Before fill */}
                                <td className="px-2 py-2 text-center">
                                  <Cell stat={before[f]} dim="fill" />
                                </td>
                                {/* Before quality */}
                                {hasAnyQuality && (
                                  <td className="px-2 py-2 text-center">
                                    <Cell stat={before[f]} dim="quality" />
                                  </td>
                                )}
                                {/* After fill */}
                                <td className="px-2 py-2 text-center">
                                  <Cell stat={after[f]} dim="fill" />
                                </td>
                                {/* After quality */}
                                {hasAnyQuality && (
                                  <td className="px-2 py-2 text-center">
                                    <Cell stat={after[f]} dim="quality" />
                                  </td>
                                )}
                                {/* Fill delta */}
                                <td className="px-2 py-2 text-center">
                                  <DeltaBadge val={fillDelta} />
                                </td>
                                {/* Quality delta */}
                                {hasAnyQuality && (
                                  <td className="px-2 py-2 text-center">
                                    {qualDelta != null ? <DeltaBadge val={qualDelta} /> : <span className="text-muted-foreground">—</span>}
                                  </td>
                                )}
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>

                    {/* Plain-language summary */}
                    <div className="mt-2 space-y-0.5">
                      {fillImproved.length > 0 && (
                        <p className="text-xs text-green-700">
                          ✓ Fill improved on: {fillImproved.map(f => FIELD_LABELS2[f] ?? f).join(", ")}
                        </p>
                      )}
                      {qualImproved.length > 0 && (
                        <p className="text-xs text-green-700">
                          ✓ Quality improved on: {qualImproved.map(f => FIELD_LABELS2[f] ?? f).join(", ")}
                        </p>
                      )}
                      {fillWorsened.length > 0 && (
                        <p className="text-xs text-red-700">
                          ✗ Fill dropped on: {fillWorsened.map(f => FIELD_LABELS2[f] ?? f).join(", ")}
                        </p>
                      )}
                    </div>

                    <button
                      onClick={() => setBeforeSnapshot(null)}
                      className="mt-2 text-xs text-muted-foreground hover:text-foreground"
                    >
                      Clear comparison
                    </button>
                  </div>
                );
              })()}

              {/* ── Run New Scrape shortcut ───────────────────────────────────── */}
              {p1.status === "ok" && (
                <div className="pt-1 border-t border-gray-100 flex items-center gap-3">
                  <span className="text-xs text-muted-foreground flex-1">
                    After applying fixes, save the recipe then run a fast scrape to verify improvement.
                  </span>
                  <Button
                    size="sm"
                    variant="outline"
                    className="h-7 text-xs border-purple-300 text-purple-700 hover:bg-purple-50"
                    onClick={async () => {
                      try {
                        const resp = await fetch(`/api/scrape/start`, {
                          method: "POST",
                          credentials: "include",
                          headers: { "Content-Type": "application/json" },
                          body: JSON.stringify({ university_id: Number(id), fast_mode: true }),
                        });
                        if (!resp.ok) throw new Error(await resp.text());
                        toast({ title: "Scrape started", description: "Fast scrape queued — check the Scraping Jobs page for progress." });
                      } catch (e: any) {
                        toast({ title: "Scrape failed to start", description: e.message, variant: "destructive" });
                      }
                    }}
                  >
                    <Play className="h-3 w-3 mr-1" />
                    Run New Scrape
                  </Button>
                </div>
              )}

            </CardContent>
          </Card>
        );
      })()}

      <Tabs defaultValue="discovery" className="space-y-4">
        <TabsList className="flex flex-wrap h-auto gap-1">
          <TabsTrigger value="discovery" className="flex items-center gap-1"><Globe className="h-3 w-3" /> Discovery</TabsTrigger>
          <TabsTrigger value="api" className="flex items-center gap-1"><Database className="h-3 w-3" /> JSON API</TabsTrigger>
          <TabsTrigger value="filters" className="flex items-center gap-1"><Filter className="h-3 w-3" /> URL Filters</TabsTrigger>
          <TabsTrigger value="selectors" className="flex items-center gap-1"><Code2 className="h-3 w-3" /> Field Selectors</TabsTrigger>
          <TabsTrigger value="fees" className="flex items-center gap-1"><DollarSign className="h-3 w-3" /> Fee Rules</TabsTrigger>
          <TabsTrigger value="english" className="flex items-center gap-1"><BookOpen className="h-3 w-3" /> IELTS & Intake</TabsTrigger>
          <TabsTrigger value="browser" className="flex items-center gap-1"><MousePointerClick className="h-3 w-3" /> Browser Actions</TabsTrigger>
          <TabsTrigger value="names" className="flex items-center gap-1"><Type className="h-3 w-3" /> Course Names</TabsTrigger>
          <TabsTrigger value="campus" className="flex items-center gap-1"><MapPin className="h-3 w-3" /> Campus & Location</TabsTrigger>
          <TabsTrigger value="quality" className="flex items-center gap-1"><ShieldCheck className="h-3 w-3" /> Quality</TabsTrigger>
        </TabsList>

        {/* ── 1. Discovery ───────────────────────────────────────────────── */}
        <TabsContent value="discovery">
          <Card>
            <CardHeader>
              <CardTitle className="text-base flex items-center gap-2"><Globe className="h-4 w-4" /> Discovery Strategy</CardTitle>
              <CardDescription>
                Control how the scraper finds course pages. Use <strong>json_api</strong> to fetch straight from a
                known JSON feed, bypassing browser crawling entirely.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-6">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <Label>Discovery Strategy</Label>
                  <Select value={recipe.discovery_strategy} onValueChange={v => patchRecipe({ discovery_strategy: v })}>
                    <SelectTrigger className="mt-1">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="auto">auto — let the system decide</SelectItem>
                      <SelectItem value="json_api">json_api — fetch from a JSON/REST endpoint</SelectItem>
                      <SelectItem value="bfs">bfs — breadth-first HTML crawl</SelectItem>
                      <SelectItem value="browser">browser — Playwright headless browser</SelectItem>
                      <SelectItem value="sitemap">sitemap — parse sitemap.xml</SelectItem>
                    </SelectContent>
                  </Select>
                  <p className="text-xs text-muted-foreground mt-1">
                    json_api is the fastest and most reliable when a JSON feed exists.
                  </p>
                </div>
                <div>
                  <Label>Fallback Strategy</Label>
                  <Select value={recipe.fallback_strategy} onValueChange={v => patchRecipe({ fallback_strategy: v })}>
                    <SelectTrigger className="mt-1"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="bfs">bfs — fall back to HTML crawl</SelectItem>
                      <SelectItem value="browser">browser — fall back to Playwright</SelectItem>
                      <SelectItem value="sitemap">sitemap — fall back to sitemap</SelectItem>
                      <SelectItem value="none">none — fail immediately if primary fails</SelectItem>
                    </SelectContent>
                  </Select>
                  <p className="text-xs text-muted-foreground mt-1">
                    Used when json_api returns 0 results.
                  </p>
                </div>
              </div>

              <Separator />

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <Label>Expected Minimum Courses</Label>
                  <Input
                    type="number"
                    value={recipe.expected_min_courses ?? ""}
                    onChange={e => patchRecipe({ expected_min_courses: e.target.value ? parseInt(e.target.value) : null })}
                    placeholder="e.g. 200"
                    className="mt-1 text-sm"
                  />
                  <p className="text-xs text-muted-foreground mt-1">Alert if fewer courses found.</p>
                </div>
                <div>
                  <Label>Expected Maximum Courses</Label>
                  <Input
                    type="number"
                    value={recipe.expected_max_courses ?? ""}
                    onChange={e => patchRecipe({ expected_max_courses: e.target.value ? parseInt(e.target.value) : null })}
                    placeholder="e.g. 800"
                    className="mt-1 text-sm"
                  />
                  <p className="text-xs text-muted-foreground mt-1">Sanity cap — flag if exceeded.</p>
                </div>
              </div>

              <Separator />

              <StringListEditor
                label="Discovery Seed URLs"
                values={recipe.seed_urls}
                onChange={v => patchRecipe({ seed_urls: v })}
                placeholder="https://example.com/study/undergraduate/courses"
                helpText="Course-listing pages visited first (BFS mode). Visitors follow links from these pages to find individual courses."
              />

              <StringListEditor
                label="Extra Course URLs (direct injection)"
                values={recipe.extra_course_urls}
                onChange={v => patchRecipe({ extra_course_urls: v })}
                placeholder="https://example.com/study/specific-course"
                helpText="Individual course pages injected directly after discovery — bypasses all crawling. Use for courses that no crawler can find."
              />
            </CardContent>
          </Card>
        </TabsContent>

        {/* ── 2. JSON API ─────────────────────────────────────────────────── */}
        <TabsContent value="api">
          <Card>
            <CardHeader>
              <CardTitle className="text-base flex items-center gap-2"><Database className="h-4 w-4" /> JSON / REST API Endpoint</CardTitle>
              <CardDescription>
                Configure a known JSON feed that returns the full course catalogue.
                Example: <code className="text-xs">https://courses.hud.ac.uk/json/2025-26/sort:title</code>
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-5">
              <div className="grid grid-cols-3 gap-4">
                <div className="col-span-2">
                  <Label>API Endpoint URL</Label>
                  <Input
                    value={recipe.api?.endpoint || ""}
                    onChange={e => patchApi({ endpoint: e.target.value })}
                    placeholder="https://courses.example.ac.uk/json/2025-26/sort:title"
                    className="mt-1 text-sm font-mono"
                  />
                </div>
                <div>
                  <Label>Method</Label>
                  <Select value={recipe.api?.method || "GET"} onValueChange={v => patchApi({ method: v })}>
                    <SelectTrigger className="mt-1"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="GET">GET</SelectItem>
                      <SelectItem value="POST">POST</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <Label>JSON Root Path</Label>
                  <Input
                    value={recipe.api?.root_path || ""}
                    onChange={e => patchApi({ root_path: e.target.value })}
                    placeholder="e.g. courses  or  data.items  or  results"
                    className="mt-1 text-sm font-mono"
                  />
                  <p className="text-xs text-muted-foreground mt-1">
                    Dot-separated path to the course array in the JSON response.
                  </p>
                </div>
                <div>
                  <Label>Course URL Template</Label>
                  <Input
                    value={recipe.api?.course_url_template || ""}
                    onChange={e => patchApi({ course_url_template: e.target.value })}
                    placeholder="https://courses.hud.ac.uk/2025-26/{study_mode}/{study_level}/{urltitle}"
                    className="mt-1 text-sm font-mono"
                  />
                  <p className="text-xs text-muted-foreground mt-1">
                    Python format string using JSON field names. E.g. <code>{"{urltitle}"}</code>
                  </p>
                </div>
              </div>

              {/* Test button */}
              <div className="flex items-center gap-3">
                <Button
                  type="button"
                  variant="outline"
                  onClick={testApi}
                  disabled={testing || !recipe.api?.endpoint}
                >
                  <RefreshCw className={`h-4 w-4 mr-2 ${testing ? "animate-spin" : ""}`} />
                  {testing ? "Testing…" : "Test API Endpoint"}
                </Button>
                {testResult && (
                  <div className="text-sm text-green-600 font-medium">
                    ✓ {testResult.count} courses found
                  </div>
                )}
              </div>

              {testResult && testResult.sample.length > 0 && (
                <div className="rounded-md bg-muted p-3 space-y-1">
                  <p className="text-xs font-medium text-muted-foreground mb-2">Sample item (first result):</p>
                  <pre className="text-xs font-mono overflow-auto max-h-40">
                    {JSON.stringify(testResult.sample[0], null, 2)}
                  </pre>
                </div>
              )}

              <Separator />

              <KeyValueEditor
                label="JSON Field Mapping"
                pairs={recipe.api?.fields || {}}
                onChange={fields => patchApi({ fields })}
                keyPlaceholder="Standard field (e.g. course_name)"
                valuePlaceholder="JSON key (e.g. title)"
                helpText="Map your standard scraper field names → the actual JSON key names. Standard fields: course_name, degree_level, study_mode_raw, full_time, part_time, url_slug, duration, campus, description."
              />

              <Separator />

              <KeyValueEditor
                label="Request Headers"
                pairs={recipe.api?.headers || {}}
                onChange={headers => patchApi({ headers })}
                keyPlaceholder="Header name (e.g. Authorization)"
                valuePlaceholder="Header value"
                helpText="Extra HTTP headers sent with every API request."
              />

              <Separator />

              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <Label>Pagination</Label>
                  <Switch
                    checked={!!recipe.api?.pagination}
                    onCheckedChange={v => patchApi({
                      pagination: v
                        ? { type: "offset", page_param: "page", size_param: "limit", page_size: 100, max_pages: 50 }
                        : undefined
                    })}
                  />
                </div>
                {recipe.api?.pagination && (
                  <div className="grid grid-cols-4 gap-3 pl-2">
                    <div>
                      <Label className="text-xs">Page Param</Label>
                      <Input
                        value={recipe.api.pagination.page_param}
                        onChange={e => patchApi({ pagination: { ...recipe.api!.pagination!, page_param: e.target.value } })}
                        className="mt-1 text-sm"
                      />
                    </div>
                    <div>
                      <Label className="text-xs">Size Param</Label>
                      <Input
                        value={recipe.api.pagination.size_param}
                        onChange={e => patchApi({ pagination: { ...recipe.api!.pagination!, size_param: e.target.value } })}
                        className="mt-1 text-sm"
                      />
                    </div>
                    <div>
                      <Label className="text-xs">Page Size</Label>
                      <Input
                        type="number"
                        value={recipe.api.pagination.page_size}
                        onChange={e => patchApi({ pagination: { ...recipe.api!.pagination!, page_size: parseInt(e.target.value) || 100 } })}
                        className="mt-1 text-sm"
                      />
                    </div>
                    <div>
                      <Label className="text-xs">Max Pages</Label>
                      <Input
                        type="number"
                        value={recipe.api.pagination.max_pages}
                        onChange={e => patchApi({ pagination: { ...recipe.api!.pagination!, max_pages: parseInt(e.target.value) || 50 } })}
                        className="mt-1 text-sm"
                      />
                    </div>
                  </div>
                )}
              </div>
            </CardContent>
          </Card>
        </TabsContent>

        {/* ── 3. URL Filters ─────────────────────────────────────────────── */}
        <TabsContent value="filters">
          <Card>
            <CardHeader>
              <CardTitle className="text-base flex items-center gap-2"><Filter className="h-4 w-4" /> URL Filters</CardTitle>
              <CardDescription>
                Control which discovered URLs are accepted as course pages.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-6">
              <StringListEditor
                label="Must Contain (whitelist substrings)"
                values={recipe.must_contain}
                onChange={v => patchRecipe({ must_contain: v })}
                placeholder="e.g. /2025-26/"
                helpText="Any URL that does NOT contain at least one of these substrings is dropped. Leave empty to allow all."
              />
              <Separator />
              <StringListEditor
                label="Block URL Patterns (regex blocklist)"
                values={recipe.block_url_patterns}
                onChange={v => patchRecipe({ block_url_patterns: v })}
                placeholder="e.g. /apprenticeship"
                helpText="Regex patterns — any URL matching one of these is dropped. E.g. /news, /events, /cpd."
              />
            </CardContent>
          </Card>
        </TabsContent>

        {/* ── 4. Field Selectors ─────────────────────────────────────────── */}
        <TabsContent value="selectors">
          <Card>
            <CardHeader>
              <CardTitle className="text-base flex items-center gap-2"><Code2 className="h-4 w-4" /> Field Selectors</CardTitle>
              <CardDescription>
                XPath, CSS, and Regex rules for extracting fields from individual course detail pages.
                These run after the scraper navigates to each course URL.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="flex items-center gap-3">
                <Switch
                  checked={recipe.fetch_detail_page}
                  onCheckedChange={v => patchRecipe({ fetch_detail_page: v })}
                />
                <div>
                  <Label>Fetch Detail Pages</Label>
                  <p className="text-xs text-muted-foreground">
                    When ON, the scraper visits each course URL to run these selectors.
                    Turn OFF if all data comes from the JSON API feed.
                  </p>
                </div>
              </div>

              {recipe.fetch_detail_page && (
                <>
                  <Separator />
                  <div className="space-y-4">
                    {STANDARD_FIELDS.map(field => (
                      <div key={field} className="grid grid-cols-4 gap-2 items-start">
                        <div className="pt-2">
                          <Label className="text-xs font-mono">{field}</Label>
                        </div>
                        <div>
                          <Label className="text-xs text-muted-foreground">XPath</Label>
                          <Input
                            value={recipe.selectors[field]?.xpath || ""}
                            onChange={e => patchSelector(field, { xpath: e.target.value || undefined })}
                            placeholder='//h1/text()'
                            className="mt-1 text-xs font-mono"
                          />
                        </div>
                        <div>
                          <Label className="text-xs text-muted-foreground">CSS</Label>
                          <Input
                            value={recipe.selectors[field]?.css || ""}
                            onChange={e => patchSelector(field, { css: e.target.value || undefined })}
                            placeholder='h1.course-title'
                            className="mt-1 text-xs font-mono"
                          />
                        </div>
                        <div>
                          <Label className="text-xs text-muted-foreground">Regex</Label>
                          <Input
                            value={recipe.selectors[field]?.regex || ""}
                            onChange={e => patchSelector(field, { regex: e.target.value || undefined })}
                            placeholder='Duration:\s*(\d+\s*\w+)'
                            className="mt-1 text-xs font-mono"
                          />
                        </div>
                      </div>
                    ))}
                  </div>
                </>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        {/* ── 5. Fee Rules ───────────────────────────────────────────────── */}
        <TabsContent value="fees">
          <Card>
            <CardHeader>
              <CardTitle className="text-base flex items-center gap-2"><DollarSign className="h-4 w-4" /> Fee Mapping Rules</CardTitle>
              <CardDescription>
                Band-based fee rules when fees aren't on individual course pages.
                The scraper checks the course name against keywords and applies the matching fee.
                Rules are checked top-to-bottom — put more specific bands first.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-5">
              <div className="grid grid-cols-3 gap-4">
                <div>
                  <Label>Fee Currency</Label>
                  <Select value={recipe.fee_currency} onValueChange={v => patchRecipe({ fee_currency: v })}>
                    <SelectTrigger className="mt-1"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {["AUD","GBP","NZD","USD","EUR","CAD","SGD"].map(c => (
                        <SelectItem key={c} value={c}>{c}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div>
                  <Label>Fee Year</Label>
                  <Input
                    type="number"
                    value={recipe.fee_year ?? ""}
                    onChange={e => patchRecipe({ fee_year: e.target.value ? parseInt(e.target.value) : null })}
                    placeholder="e.g. 2025"
                    className="mt-1 text-sm"
                  />
                </div>
              </div>

              <Separator />

              <FeeRulesEditor
                label="Undergraduate Fee Bands"
                rules={recipe.fee_rules_undergraduate}
                onChange={v => patchRecipe({ fee_rules_undergraduate: v })}
              />

              <Separator />

              <FeeRulesEditor
                label="Postgraduate Fee Bands"
                rules={recipe.fee_rules_postgraduate}
                onChange={v => patchRecipe({ fee_rules_postgraduate: v })}
              />

              <Separator />

              {/* ── International fee preference ── */}
              <div className="space-y-4">
                <h3 className="text-sm font-semibold flex items-center gap-2">
                  <DollarSign className="h-4 w-4 text-green-600" />
                  International Fee Preference
                </h3>
                <div className="flex items-center gap-3">
                  <Switch
                    checked={recipe.fee_prefer_international}
                    onCheckedChange={v => patchRecipe({ fee_prefer_international: v })}
                  />
                  <div>
                    <Label>Prefer International Fee</Label>
                    <p className="text-xs text-muted-foreground">
                      When both a domestic and an international fee appear on the same page,
                      always keep the international one — even if domestic was extracted first.
                      Use for universities with tab-based fee layouts (e.g. JCU).
                    </p>
                  </div>
                </div>

                <StringListEditor
                  label="Reject Domestic Fee Keywords"
                  values={recipe.fee_reject_keywords}
                  onChange={v => patchRecipe({ fee_reject_keywords: v })}
                  placeholder='e.g. "Commonwealth Supported" or "HECS"'
                  helpText="Case-insensitive. If any keyword appears in the text surrounding an extracted fee, that fee is discarded as domestic/CSP and not staged as international_fee."
                />
                {recipe.fee_reject_keywords.length === 0 && (
                  <div className="rounded-lg bg-blue-50 border border-blue-200 p-3 text-xs text-blue-700">
                    <strong>Tip for JCU:</strong> Add "Commonwealth Supported", "CSP", "HECS", "HECS-HELP",
                    "Domestic students", "Domestic fee" to reject domestic fees automatically.
                  </div>
                )}

                <StringListEditor
                  label="Fee Follow Links"
                  values={recipe.fee_follow_links}
                  onChange={v => patchRecipe({ fee_follow_links: v })}
                  placeholder='e.g. "fees and scholarships" or "international student fees"'
                  helpText="When international_fee is blank after extraction, the scraper follows any <a> links on the course page whose text matches these phrases and re-runs the fee extractor on the linked page. Useful for universities (e.g. JCU) where international fees are on a separate linked page, not the course page itself."
                />
                {recipe.fee_follow_links.length === 0 && (
                  <div className="rounded-lg bg-amber-50 border border-amber-200 p-3 text-xs text-amber-700">
                    <strong>Tip for JCU:</strong> Add "fees and scholarships", "international student fees",
                    "fees for your course" — the scraper will follow those links to find the international fee
                    when only a CSP fee appears on the course page.
                  </div>
                )}
              </div>
            </CardContent>
          </Card>
        </TabsContent>

        {/* ── 6. IELTS & Intake ──────────────────────────────────────────── */}
        <TabsContent value="english">
          <Card>
            <CardHeader>
              <CardTitle className="text-base flex items-center gap-2"><BookOpen className="h-4 w-4" /> IELTS & Intake Rules</CardTitle>
              <CardDescription>
                Regex and XPath rules for extracting English test scores and intake months from course pages.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-6">
              <div className="space-y-4">
                <h3 className="text-sm font-semibold">IELTS / English Requirements</h3>
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <Label>Overall Score Regex</Label>
                    <Input
                      value={recipe.ielts?.overall_regex || ""}
                      onChange={e => patchIelts({ overall_regex: e.target.value })}
                      placeholder={'(\\d\\.?\\d*)\\s*overall'}
                      className="mt-1 text-sm font-mono"
                    />
                    <p className="text-xs text-muted-foreground mt-1">First capture group = overall IELTS score.</p>
                  </div>
                  <div>
                    <Label>Per-Band / Component Regex</Label>
                    <Input
                      value={recipe.ielts?.band_regex || ""}
                      onChange={e => patchIelts({ band_regex: e.target.value })}
                      placeholder={'no less than\\s*(\\d\\.?\\d*)'}
                      className="mt-1 text-sm font-mono"
                    />
                    <p className="text-xs text-muted-foreground mt-1">First capture group = minimum per-band score.</p>
                  </div>
                </div>
                <div>
                  <Label>Source XPath (limit search region)</Label>
                  <Input
                    value={recipe.ielts?.source_xpath || ""}
                    onChange={e => patchIelts({ source_xpath: e.target.value })}
                    placeholder={"//div[contains(@id, 'entry')]"}
                    className="mt-1 text-sm font-mono"
                  />
                  <p className="text-xs text-muted-foreground mt-1">Optional — only search for IELTS within this element.</p>
                </div>
              </div>

              <Separator />

              {/* ── English follow links ── */}
              <div className="space-y-4">
                <h3 className="text-sm font-semibold flex items-center gap-2">
                  <Link className="h-4 w-4 text-blue-600" />
                  English Requirement Link Following
                </h3>
                <StringListEditor
                  label="Follow Link Text Patterns"
                  values={recipe.follow_links}
                  onChange={v => patchRecipe({ follow_links: v })}
                  placeholder='e.g. "minimum English language requirements"'
                  helpText="When IELTS is blank after main extraction, the scraper finds <a> tags whose text matches any of these phrases and fetches those pages to extract IELTS/PTE/TOEFL. Case-insensitive substring match."
                />
                {recipe.follow_links.length === 0 && (
                  <div className="rounded-lg bg-blue-50 border border-blue-200 p-3 text-xs text-blue-700">
                    <strong>Tip for JCU:</strong> Add "minimum English language requirements",
                    "English language requirements", "admissions policy schedule".
                  </div>
                )}
              </div>

              <Separator />

              {/* ── Band mapping ── */}
              <div className="space-y-4">
                <h3 className="text-sm font-semibold flex items-center gap-2">
                  <BookOpen className="h-4 w-4 text-purple-600" />
                  English Band Mapping
                </h3>
                <p className="text-xs text-muted-foreground">
                  For universities that show a band label (e.g. "Band 2") instead of direct IELTS scores.
                  When IELTS is still blank after all extractors run, the band label found in the course
                  page is resolved to concrete scores using this table.
                </p>
                <div>
                  <Label>Band Reference URL</Label>
                  <Input
                    value={recipe.band_reference_url}
                    onChange={e => patchRecipe({ band_reference_url: e.target.value })}
                    placeholder="https://www.university.edu/admissions-policy"
                    className="mt-1 text-sm"
                  />
                  <p className="text-xs text-muted-foreground mt-1">
                    URL of the page that defines the band scores. Stored in evidence so reviewers can verify.
                  </p>
                </div>
                <BandMappingEditor
                  mapping={recipe.band_mapping}
                  onChange={v => patchRecipe({ band_mapping: v })}
                />
              </div>

              <Separator />

              <div className="space-y-4">
                <h3 className="text-sm font-semibold">Intake / Start Date Extraction</h3>
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <Label>Start Date XPath</Label>
                    <Input
                      value={recipe.intake?.xpath || ""}
                      onChange={e => patchIntake({ xpath: e.target.value })}
                      placeholder={"//h2[contains(text(), 'Start date')]/following-sibling::p/text()"}
                      className="mt-1 text-sm font-mono"
                    />
                  </div>
                  <div>
                    <Label>Start Date Regex</Label>
                    <Input
                      value={recipe.intake?.regex || ""}
                      onChange={e => patchIntake({ regex: e.target.value })}
                      placeholder={'(January|February|March|April|May|June|July|August|September|October|November|December)'}
                      className="mt-1 text-sm font-mono"
                    />
                  </div>
                </div>
                <KeyValueEditor
                  label="Month Name Map"
                  pairs={recipe.intake?.month_map || {}}
                  onChange={month_map => patchIntake({ month_map })}
                  keyPlaceholder="Raw text (e.g. Autumn)"
                  valuePlaceholder="Canonical month (e.g. March)"
                  helpText="Map session names or abbreviations to canonical month names."
                />
              </div>
            </CardContent>
          </Card>
        </TabsContent>

        {/* ── 7. Browser Actions ─────────────────────────────────────────── */}
        <TabsContent value="browser">

          <Card>
            <CardHeader>
              <CardTitle className="text-base flex items-center gap-2">
                <MousePointerClick className="h-4 w-4" /> Browser Interaction Actions
              </CardTitle>
              <CardDescription>
                Configure interactive steps the browser executes after loading a course page —
                before HTML is captured for extraction. Use this to click tabs (e.g. "International"),
                expand sections, or wait for dynamic content to load.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-6">
              <BrowserActionsEditor
                actions={recipe.actions}
                onChange={v => patchRecipe({ actions: v })}
              />

              {recipe.actions.length > 0 && (
                <>
                  <Separator />
                  <div className="space-y-2">
                    <h3 className="text-sm font-semibold">Generated YAML Preview</h3>
                    <pre className="text-xs bg-muted rounded-lg p-3 overflow-auto max-h-48 font-mono">
{`extraction:\n  actions:\n${recipe.actions.map(a => {
  if (a.action_type === "wait_for_text") return `    - wait_for:\n        text: "${a.value}"`;
  if (a.action_type === "wait_for_selector") return `    - wait_for:\n        selector: "${a.value}"`;
  const key = a.action_type === "click_text" ? "click_text"
    : a.action_type === "click_css" ? "click_css"
    : a.action_type === "expand_text" ? "expand_text"
    : "scroll_to";
  return `    - ${key}: "${a.value}"`;
}).join("\n")}`}
                    </pre>
                    <p className="text-xs text-muted-foreground">
                      Copy this into the university's YAML file under{" "}
                      <code className="bg-muted px-1 rounded">scraper_config/unis/</code> to apply
                      permanently, or save the recipe here for operator-level control.
                    </p>
                  </div>
                </>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        {/* ── 8. Course Name Cleanup ──────────────────────────────────────── */}
        <TabsContent value="names">
          <Card>
            <CardHeader>
              <CardTitle className="text-base flex items-center gap-2"><Type className="h-4 w-4" /> Course Name Cleanup</CardTitle>
              <CardDescription>
                Fix course names that include site-name suffixes, year stamps, or other noise scraped from the page.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-5">
              <StringListEditor
                label="Strip everything after… (remove_after)"
                values={recipe.course_name_remove_after}
                onChange={v => patchRecipe({ course_name_remove_after: v })}
                placeholder='e.g. | or  - University Name'
                helpText='Remove this string and everything following it from the course name. Applied left-to-right. E.g. "|" strips " | Southern Cross University".'
              />
              <div className="flex items-center gap-3">
                <Switch
                  checked={recipe.course_name_remove_year_suffix}
                  onCheckedChange={v => patchRecipe({ course_name_remove_year_suffix: v })}
                />
                <div>
                  <Label>Remove trailing year (e.g. "Master of Science 2025" → "Master of Science")</Label>
                  <p className="text-xs text-muted-foreground">Strips a trailing 4-digit year when present.</p>
                </div>
              </div>
              <StringListEditor
                label="Regex patterns to strip (remove_patterns)"
                values={recipe.course_name_remove_patterns}
                onChange={v => patchRecipe({ course_name_remove_patterns: v })}
                placeholder='e.g. \s*\(.*?\)\s*$'
                helpText='Case-insensitive regex patterns. Each match is replaced with an empty string. E.g. strip trailing parenthetical suffixes.'
              />
            </CardContent>
          </Card>
        </TabsContent>

        {/* ── 9. Campus & Location ────────────────────────────────────────── */}
        <TabsContent value="campus">
          <div className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle className="text-base flex items-center gap-2"><MapPin className="h-4 w-4" /> Campus Rules</CardTitle>
            </CardHeader>
            <CardContent className="space-y-5">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <Label>Default City / Campus</Label>
                  <Input
                    value={recipe.campus?.default_city || ""}
                    onChange={e => patchCampus({ default_city: e.target.value })}
                    placeholder="e.g. Huddersfield"
                    className="mt-1 text-sm"
                  />
                  <p className="text-xs text-muted-foreground mt-1">Used when no campus is detected on the page.</p>
                </div>
              </div>
              <div className="flex items-center gap-3">
                <Switch
                  checked={recipe.campus?.online_only_reject ?? false}
                  onCheckedChange={v => patchCampus({ online_only_reject: v })}
                />
                <div>
                  <Label>Reject Online-Only Courses</Label>
                  <p className="text-xs text-muted-foreground">
                    Drop courses whose only delivery mode is Online or Distance.
                  </p>
                </div>
              </div>
              <StringListEditor
                label="Valid Campuses (allowlist)"
                values={recipe.campus?.valid_campuses || []}
                onChange={v => patchCampus({ valid_campuses: v })}
                placeholder="e.g. Huddersfield"
                helpText="When non-empty, courses at any other campus are dropped. Leave empty to allow all campuses."
              />
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-base flex items-center gap-2"><MapPin className="h-4 w-4" /> Location Cleanup</CardTitle>
              <CardDescription>
                Fix garbled, over-long, or contaminated location values scraped from the page.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-5">
              <KeyValueEditor
                label="Location replacements (location_replace)"
                pairs={recipe.location_replace}
                onChange={v => patchRecipe({ location_replace: v })}
                keyPlaceholder="Text to replace"
                valuePlaceholder="Replacement (blank to delete)"
                helpText='Applied before allow/reject filtering. E.g. "SCU Online" → "Online", "Teaching period" → "" (delete it).'
              />
              <StringListEditor
                label="Allowed location values (location_allowed_values)"
                values={recipe.location_allowed_values}
                onChange={v => patchRecipe({ location_allowed_values: v })}
                placeholder="e.g. Gold Coast"
                helpText="When non-empty, only locations matching one of these strings (case-insensitive substring) are kept. Non-matching values are cleared."
              />
              <StringListEditor
                label="Reject location values (location_reject_values)"
                values={recipe.location_reject_values}
                onChange={v => patchRecipe({ location_reject_values: v })}
                placeholder="e.g. How to Apply"
                helpText="If any of these strings appears in the extracted location, the location is cleared entirely. Use to strip nav/footer contamination."
              />
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-base flex items-center gap-2"><Zap className="h-4 w-4" /> Study Mode Rules</CardTitle>
              <CardDescription>
                Control how study mode is derived when the page doesn't publish it explicitly.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-5">
              <div className="flex items-center gap-3">
                <Switch
                  checked={recipe.study_mode_from_location}
                  onCheckedChange={v => patchRecipe({ study_mode_from_location: v })}
                />
                <div>
                  <Label>Derive study mode from location</Label>
                  <p className="text-xs text-muted-foreground">
                    When study mode is blank, inspect the cleaned location value. Online keywords → Online; both campus + online keywords → Blended; campus only → On Campus.
                  </p>
                </div>
              </div>
              {recipe.study_mode_from_location && (
                <StringListEditor
                  label="Online keywords (study_mode_online_keywords)"
                  values={recipe.study_mode_online_keywords}
                  onChange={v => patchRecipe({ study_mode_online_keywords: v })}
                  placeholder="e.g. online"
                  helpText='Words that indicate online delivery when found in the location. Defaults to ["online", "distance", "virtual"] when empty.'
                />
              )}
            </CardContent>
          </Card>
          </div>
        </TabsContent>

        {/* ── 10. Quality ─────────────────────────────────────────────────── */}
        <TabsContent value="quality">
          <Card>
            <CardHeader>
              <CardTitle className="text-base flex items-center gap-2"><ShieldCheck className="h-4 w-4" /> Quality Thresholds</CardTitle>
              <CardDescription>
                Control which scraped courses are auto-published vs sent to manual review.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-5">
              <div>
                <Label>Minimum Completeness % for Auto-Publish</Label>
                <div className="flex items-center gap-3 mt-1">
                  <Input
                    type="number"
                    min={0}
                    max={100}
                    value={recipe.minimum_completeness}
                    onChange={e => patchRecipe({ minimum_completeness: parseInt(e.target.value) || 85 })}
                    className="w-32 text-sm"
                  />
                  <span className="text-sm text-muted-foreground">% (default: 85)</span>
                </div>
                <p className="text-xs text-muted-foreground mt-1">
                  Courses below this completeness threshold are sent to "review" instead of being auto-published.
                </p>
              </div>
              <StringListEditor
                label="Required Fields"
                values={recipe.required_fields}
                onChange={v => patchRecipe({ required_fields: v })}
                placeholder="e.g. international_fee"
                helpText="Fields that MUST be non-empty for a course to pass quality gate. Courses missing any required field are flagged."
              />
              <div>
                <StringListEditor
                  label="Block auto-publish if… (block_publish_if)"
                  values={recipe.block_publish_if}
                  onChange={v => patchRecipe({ block_publish_if: v })}
                  placeholder="e.g. fee_missing"
                  helpText="Conditions that block auto-publish even if completeness is above threshold."
                />
                <div className="mt-2 p-3 rounded-md bg-muted text-xs text-muted-foreground space-y-1">
                  <p className="font-medium">Available conditions:</p>
                  <div className="grid grid-cols-2 gap-x-4 gap-y-0.5">
                    {["fee_missing", "fee_term_wrong", "ielts_component_missing", "invalid_location",
                      "online_only", "course_name_too_short", "course_name_too_long", "degree_level_missing"].map(c => (
                      <code key={c} className="font-mono">{c}</code>
                    ))}
                  </div>
                </div>
              </div>
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>

      {/* Save footer */}
      <div className="flex justify-end gap-3 pt-2 border-t">
        <Button variant="outline" onClick={() => navigate(`/universities/${id}`)}>Cancel</Button>
        <Button onClick={save} disabled={saving}>
          <Save className="h-4 w-4 mr-2" />
          {saving ? "Saving…" : "Save Recipe"}
        </Button>
      </div>
    </div>
  );
}

// ── Constant used in patchApi helper ──
const EMPTY_API: ApiConfig = {
  endpoint: "",
  method: "GET",
  root_path: "",
  course_url_template: "",
  fields: {},
  headers: {},
};
