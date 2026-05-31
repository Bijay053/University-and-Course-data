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
  Code2, DollarSign, BookOpen, MapPin, ShieldCheck, Zap, RefreshCw
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
  campus?: {
    default_city: string;
    valid_campuses: string[];
    online_only_reject: boolean;
  };
  minimum_completeness: number;
  required_fields: string[];
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
  minimum_completeness: 85,
  required_fields: [],
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

      let items: any[] = data;
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
          <Button onClick={save} disabled={saving}>
            <Save className="h-4 w-4 mr-2" />
            {saving ? "Saving…" : "Save Recipe"}
          </Button>
        </div>
      </div>

      <Tabs defaultValue="discovery" className="space-y-4">
        <TabsList className="flex flex-wrap h-auto gap-1">
          <TabsTrigger value="discovery" className="flex items-center gap-1"><Globe className="h-3 w-3" /> Discovery</TabsTrigger>
          <TabsTrigger value="api" className="flex items-center gap-1"><Database className="h-3 w-3" /> JSON API</TabsTrigger>
          <TabsTrigger value="filters" className="flex items-center gap-1"><Filter className="h-3 w-3" /> URL Filters</TabsTrigger>
          <TabsTrigger value="selectors" className="flex items-center gap-1"><Code2 className="h-3 w-3" /> Field Selectors</TabsTrigger>
          <TabsTrigger value="fees" className="flex items-center gap-1"><DollarSign className="h-3 w-3" /> Fee Rules</TabsTrigger>
          <TabsTrigger value="english" className="flex items-center gap-1"><BookOpen className="h-3 w-3" /> IELTS & Intake</TabsTrigger>
          <TabsTrigger value="campus" className="flex items-center gap-1"><MapPin className="h-3 w-3" /> Campus</TabsTrigger>
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

        {/* ── 7. Campus ──────────────────────────────────────────────────── */}
        <TabsContent value="campus">
          <Card>
            <CardHeader>
              <CardTitle className="text-base flex items-center gap-2"><MapPin className="h-4 w-4" /> Campus & Location Rules</CardTitle>
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
        </TabsContent>

        {/* ── 8. Quality ─────────────────────────────────────────────────── */}
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
