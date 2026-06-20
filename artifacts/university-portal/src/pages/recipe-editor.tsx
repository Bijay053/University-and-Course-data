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
  TrendingUp, ExternalLink, Play, Type, Calendar, GitMerge, ClipboardList, AlertCircle,
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

interface SearchStaxFieldMap {
  degree_level?: string;
  study_mode?: string;
  duration?: string;
  intake_dates?: string;
  category?: string;
  location?: string;
}

interface SearchStaxConfig {
  enabled: boolean;
  endpoint: string;
  token_env: string;
  filter_query: string;
  currency: string;
  url_base?: string;
  location_strip_prefixes: string[];
  exclude_part_time: boolean;
  links_only: boolean;
  field_map_as_payload: boolean;
  field_map: SearchStaxFieldMap;
  page_size?: number;
  max_pages?: number;
}

interface SsrPropListingPage {
  url: string;
  url_prefix: string;
  label: string;
}

interface SsrPropDiscovery {
  listing_pages: SsrPropListingPage[];
  prop_attr: string;
  slug_field: string;
  name_field: string;
  url_suffix: string;
  year_field: string;
  year_filter_prefix: string;
  browser_fallback_xpath: string;
  browser_wait_selector: string;
  course_url_pattern: string;
}

interface ApiConfig {
  endpoint: string;
  method: string;
  query_params: Record<string, string>;
  root_path: string;
  count_path: string;
  course_url_template: string;
  fields: Record<string, string>;
  headers: Record<string, string>;
  enabled?: boolean;
  fetch_via_browser?: boolean;
  browser_seed_url?: string;
  base_url?: string;
  normalize_relative_urls?: boolean;
  url_fields?: string[];
  title_fields?: string[];
  api_allow_url_patterns?: string[];
  api_block_url_patterns?: string[];
  additional_urls?: string[];
  page_size?: number;
  max_pages?: number;
  body?: Record<string, any> | null;
  body_pagination?: {
    current_path: string;
    size_path?: string;
    total_pages_path?: string;
    total_results_path?: string;
  };
  pagination?: {
    type: string;
    page_param: string;
    size_param: string;
    page_size: number;
    page_start: number;
    max_pages: number;
  };
}

interface ApiTestResult {
  status: string;
  http_status?: number;
  total_from_api?: number | null;
  page1_count?: number;
  page2_count?: number | null;
  sample_names?: string[];
  all_keys?: string[];
  warnings?: string[];
  error?: string;
}

interface BandSpecUI {
  ielts_overall?: number | string;
  ielts_each?: number | string;
  pte_overall?: number | string;
  toefl_overall?: number | string;
}

interface DegreeEnglishTier {
  ielts?: number | null;
  pte?: number | null;
  toefl?: number | null;
  duolingo?: number | null;
}

interface BrowserAction {
  action_type: "click_text" | "click_css" | "wait_for_text" | "wait_for_selector" | "expand_text" | "scroll_to";
  value: string;
}

interface Recipe {
  discovery_strategy: string;
  seed_urls: string[];
  render_listing_pages: string[];
  extra_course_urls: string[];
  expected_min_courses: number | null;
  expected_max_courses: number | null;
  browser_time_budget_s: number | null;
  browser_early_stop_courses: number | null;
  max_candidates: number | null;
  bfs_page_budget: number | null;
  fallback_strategy: string;
  api?: ApiConfig;
  must_contain: string[];
  allow_url_patterns: string[];
  course_detail_url_patterns: string[];
  block_url_patterns: string[];
  fetch_detail_page: boolean;
  scrape_do_static: boolean;
  scrape_do_geo: string;
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
  fee_url_suffix: string;
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
  course_english_priority: boolean;
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
  // Year & Duplicate Handling
  course_year: {
    mode: string;
    preferred_year: number | null;
    ignore_years: number[];
    duplicate_key: string;
  };
  ignore_urls_matching: string[];
  prefer_urls_matching: string[];
  fee_reject_years: number[];
  url_rewrites: { host: string; path_contains?: string; append_query: string }[];
  degree_level_defaults: Record<string, DegreeEnglishTier>;
  searchstax?: SearchStaxConfig;
  ssr_prop_discovery?: SsrPropDiscovery;
}

const EMPTY_RECIPE: Recipe = {
  discovery_strategy: "auto",
  seed_urls: [],
  render_listing_pages: [],
  extra_course_urls: [],
  expected_min_courses: null,
  expected_max_courses: null,
  browser_time_budget_s: null,
  browser_early_stop_courses: null,
  max_candidates: null,
  bfs_page_budget: null,
  fallback_strategy: "bfs",
  must_contain: [],
  allow_url_patterns: [],
  course_detail_url_patterns: [],
  block_url_patterns: [],
  fetch_detail_page: true,
  scrape_do_static: false,
  scrape_do_geo: "",
  selectors: {},
  fee_currency: "AUD",
  fee_year: null,
  fee_rules_undergraduate: [],
  fee_rules_postgraduate: [],
  fee_reject_keywords: [],
  fee_prefer_international: false,
  fee_url_suffix: "",
  fee_follow_links: [],
  minimum_completeness: 85,
  required_fields: [],
  block_publish_if: [],
  follow_links: [],
  band_reference_url: "",
  band_mapping: {},
  course_english_priority: false,
  actions: [],
  course_name_remove_after: [],
  course_name_remove_year_suffix: false,
  course_name_remove_patterns: [],
  location_replace: {},
  location_allowed_values: [],
  location_reject_values: [],
  study_mode_from_location: false,
  study_mode_online_keywords: [],
  course_year: {
    mode: "keep_all",
    preferred_year: null,
    ignore_years: [],
    duplicate_key: "none",
  },
  ignore_urls_matching: [],
  prefer_urls_matching: [],
  fee_reject_years: [],
  url_rewrites: [],
  degree_level_defaults: {},
  searchstax: undefined,
  ssr_prop_discovery: undefined,
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
  label, values, onChange, placeholder, helpText, validateRegex,
}: {
  label: string;
  values: string[];
  onChange: (v: string[]) => void;
  placeholder?: string;
  helpText?: React.ReactNode;
  validateRegex?: boolean;
}) {
  const [draft, setDraft] = useState("");
  const [bulkOpen, setBulkOpen] = useState(false);
  const [bulkText, setBulkText] = useState("");
  const [regexError, setRegexError] = useState<string | null>(null);

  const checkRegex = (v: string): string | null => {
    if (!validateRegex || !v.trim()) return null;
    try {
      new RegExp(v.trim());
      return null;
    } catch (e) {
      return e instanceof Error ? e.message : "Invalid regex";
    }
  };

  const handleDraftChange = (v: string) => {
    setDraft(v);
    setRegexError(checkRegex(v));
  };

  const add = () => {
    const v = draft.trim();
    if (!v || values.includes(v)) return;
    const err = checkRegex(v);
    if (err) { setRegexError(err); return; }
    onChange([...values, v]);
    setDraft("");
    setRegexError(null);
  };

  const addBulk = () => {
    const lines = bulkText
      .split(/[\n\r,]+/)
      .map(s => s.trim())
      .filter(s => s && !values.includes(s));
    const unique = [...new Set(lines)];
    if (unique.length > 0) onChange([...values, ...unique]);
    setBulkText("");
    setBulkOpen(false);
  };

  return (
    <div className="space-y-2">
      <Label>{label}</Label>
      {helpText && <p className="text-xs text-muted-foreground">{helpText}</p>}
      <div className="flex gap-2">
        <Input
          value={draft}
          onChange={e => handleDraftChange(e.target.value)}
          placeholder={placeholder}
          onKeyDown={e => { if (e.key === "Enter") { e.preventDefault(); add(); }}}
          className={`text-sm${regexError ? " border-destructive focus-visible:ring-destructive" : ""}`}
        />
        <Button type="button" variant="outline" size="sm" onClick={add} title={regexError ? regexError : "Add"} disabled={!!regexError}>
          {regexError ? <AlertCircle className="h-3 w-3 text-destructive" /> : <Plus className="h-3 w-3" />}
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => setBulkOpen(o => !o)}
          title="Paste multiple URLs at once"
          className={bulkOpen ? "bg-muted" : ""}
        >
          <ClipboardList className="h-3 w-3" />
        </Button>
      </div>
      {regexError && (
        <p className="flex items-center gap-1 text-xs text-destructive">
          <AlertCircle className="h-3 w-3 shrink-0" />
          {regexError}
        </p>
      )}
      {bulkOpen && (
        <div className="space-y-2 rounded-md border border-dashed p-3 bg-muted/30">
          <p className="text-xs text-muted-foreground font-medium">Paste multiple URLs — one per line</p>
          <Textarea
            value={bulkText}
            onChange={e => setBulkText(e.target.value)}
            placeholder={"https://example.com/courses/page-1\nhttps://example.com/courses/page-2\nhttps://example.com/courses/page-3"}
            className="text-xs font-mono min-h-[100px] resize-y"
            autoFocus
          />
          <div className="flex gap-2">
            <Button type="button" size="sm" onClick={addBulk} disabled={!bulkText.trim()}>
              Add All
            </Button>
            <Button type="button" size="sm" variant="ghost" onClick={() => { setBulkText(""); setBulkOpen(false); }}>
              Cancel
            </Button>
          </div>
        </div>
      )}
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

// Inline regex-validated pattern row used by the API provider URL pattern lists.
// Unlike StringListEditor (draft+add model), these rows edit values in-place.
function RegexPatternRow({
  value,
  onChange,
  onRemove,
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  onRemove: () => void;
  placeholder?: string;
}) {
  const [regexError, setRegexError] = useState<string | null>(null);

  const check = (v: string): string | null => {
    if (!v.trim()) return null;
    try { new RegExp(v.trim()); return null; }
    catch (e) { return e instanceof Error ? e.message : "Invalid regex"; }
  };

  const handleChange = (v: string) => {
    setRegexError(check(v));
    onChange(v);
  };

  return (
    <div className="space-y-0.5">
      <div className="flex gap-2 mt-1">
        <Input
          value={value}
          onChange={e => handleChange(e.target.value)}
          placeholder={placeholder}
          className={`text-sm font-mono flex-1${regexError ? " border-destructive focus-visible:ring-destructive" : ""}`}
        />
        <Button type="button" variant="ghost" size="sm" onClick={onRemove}>✕</Button>
      </div>
      {regexError && (
        <p className="flex items-center gap-1 text-xs text-destructive pl-0.5">
          <AlertCircle className="h-3 w-3 shrink-0" />
          {regexError}
        </p>
      )}
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

// ── Degree-Level English Defaults Editor ──────────────────────────────────

const DEGREE_TIERS = ["undergraduate", "postgraduate", "doctorate"] as const;

function DegreeLevelDefaultsEditor({
  defaults,
  onChange,
}: {
  defaults: Record<string, DegreeEnglishTier>;
  onChange: (d: Record<string, DegreeEnglishTier>) => void;
}) {
  const patch = (tier: string, field: keyof DegreeEnglishTier, raw: string) => {
    const parsed = raw === "" ? null : parseFloat(raw);
    const current = defaults[tier] || {};
    const next: DegreeEnglishTier = { ...current, [field]: raw === "" || isNaN(parsed as number) ? null : parsed };
    const hasValues = Object.values(next).some(v => v != null);
    const nextDefaults = { ...defaults };
    if (hasValues) {
      nextDefaults[tier] = next;
    } else {
      delete nextDefaults[tier];
    }
    onChange(nextDefaults);
  };

  const val = (tier: string, field: keyof DegreeEnglishTier): string => {
    const v = defaults[tier]?.[field];
    return v != null ? String(v) : "";
  };

  const activeCount = Object.keys(defaults).length;

  return (
    <div className="space-y-2">
      <div className="rounded-lg border overflow-hidden">
        <table className="w-full text-xs">
          <thead className="bg-muted/50">
            <tr>
              <th className="px-3 py-2 text-left font-medium w-36">Tier</th>
              <th className="px-3 py-2 text-left font-medium">IELTS Overall</th>
              <th className="px-3 py-2 text-left font-medium">PTE Overall</th>
              <th className="px-3 py-2 text-left font-medium">TOEFL iBT</th>
              <th className="px-3 py-2 text-left font-medium">Duolingo</th>
            </tr>
          </thead>
          <tbody>
            {DEGREE_TIERS.map(tier => (
              <tr key={tier} className="border-t">
                <td className="px-3 py-2 font-mono font-semibold text-blue-700 capitalize">{tier}</td>
                {(["ielts", "pte", "toefl", "duolingo"] as const).map(field => (
                  <td key={field} className="px-2 py-1.5">
                    <Input
                      type="number"
                      step={field === "ielts" ? "0.5" : "1"}
                      value={val(tier, field)}
                      onChange={e => patch(tier, field, e.target.value)}
                      placeholder="—"
                      className="h-7 text-xs w-20"
                    />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
        {activeCount > 0 && (
          <div className="bg-muted/30 px-3 py-2 text-xs text-muted-foreground border-t">
            <strong>Active:</strong>{" "}
            {Object.entries(defaults)
              .map(([tier, s]) =>
                `${tier}: IELTS ${s.ielts ?? "—"} / PTE ${s.pte ?? "—"} / TOEFL ${s.toefl ?? "—"}`
              )
              .join(" · ")}
          </div>
        )}
      </div>
      {activeCount === 0 && (
        <p className="text-xs text-muted-foreground italic">All tiers will use the flat institution default (default_ielts / default_pte / default_toefl).</p>
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
  const [yamlSlug, setYamlSlug] = useState<string | null>(null);
  const [recipe, setRecipe] = useState<Recipe>(EMPTY_RECIPE);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<ApiTestResult | null>(null);
  const [testingDiscovery, setTestingDiscovery] = useState(false);
  const [discoveryResult, setDiscoveryResult] = useState<any | null>(null);
  const [showDropped, setShowDropped] = useState(false);
  const [diagnosing, setDiagnosing] = useState(false);
  const [diagnoseResult, setDiagnoseResult] = useState<any | null>(null);
  const [beforeSnapshot, setBeforeSnapshot] = useState<Record<string, any> | null>(null);

  // ── Filter Simulator ──
  type FilterSimRow = { url: string; passed: boolean; drop_reason: string | null; matching_allow_pattern: string | null; blocking_block_pattern: string | null };
  type FilterSimSummary = { total: number; kept_count: number; dropped_count: number; drop_pct: number };
  const [filterSimUrls, setFilterSimUrls] = useState("");
  const [filterSimLoading, setFilterSimLoading] = useState(false);
  const [loadingFilterUrls, setLoadingFilterUrls] = useState(false);
  const [filterSimResults, setFilterSimResults] = useState<{ results: FilterSimRow[]; summary: FilterSimSummary } | null>(null);
  const [filterSimError, setFilterSimError] = useState<string | null>(null);
  const [filterSimAllowPats, setFilterSimAllowPats] = useState<string[]>([]);

  // ── Listing-page link fetcher ──
  const [listingFetchUrl, setListingFetchUrl] = useState("");
  const [fetchingListing, setFetchingListing] = useState(false);
  const [listingFetchResult, setListingFetchResult] = useState<{
    links: string[]; method: string; total_raw: number; needs_browser: boolean; error?: string;
  } | null>(null);
  const [selectedListingLinks, setSelectedListingLinks] = useState<Set<string>>(new Set());

  // Local raw-text state for year textareas — avoids the "can't type partial year"
  // problem that occurs when the controlled value is derived from number[] and the
  // onChange filter strips any value < 2001 (so "202" disappears mid-keystroke).
  const [ignoreYearsText, setIgnoreYearsText] = useState("");
  const [feeRejectYearsText, setFeeRejectYearsText] = useState("");

  // ── Load ──
  useEffect(() => {
    if (!id) return;
    fetch(`/api/universities/${id}/recipe`, { credentials: "include" })
      .then(r => r.json())
      .then(data => {
        setUniName(data.university_name || "");
        setScrapeUrl(data.scrape_url || "");
        if (data.yaml_slug) setYamlSlug(data.yaml_slug);
        if (data.recipe && Object.keys(data.recipe).length > 0) {
          const loaded: Recipe = { ...EMPTY_RECIPE, ...data.recipe };
          setRecipe(loaded);
          // Sync raw-text mirrors from the loaded recipe
          setIgnoreYearsText((loaded.course_year?.ignore_years || []).join("\n"));
          setFeeRejectYearsText((loaded.fee_reject_years || []).join("\n"));
        }
      })
      .catch(() => toast({ title: "Failed to load recipe", variant: "destructive" }))
      .finally(() => setLoading(false));
  }, [id]);

  // ── Filter Simulator handlers ──
  const loadFilterUrls = useCallback(async () => {
    if (!id) return;
    setLoadingFilterUrls(true);
    setFilterSimError(null);
    try {
      const resp = await fetch(`/api/universities/${id}/filter-impact`, { credentials: "include" });
      const data = await resp.json();
      const urls: string[] = [...(data.kept_samples || []), ...(data.dropped_samples || [])];
      if (!urls.length) {
        setFilterSimError("No historical course URLs found — run a scrape first, or paste URLs below manually.");
        return;
      }
      setFilterSimUrls(urls.join("\n"));
      const configPats: string[] = data.filter_config?.allow_url_patterns || [];
      setFilterSimAllowPats(configPats);
      // Auto-sync into the recipe editor when the recipe field is empty but the
      // saved config has patterns — so the editor reflects the active config.
      // Use the functional updater form so we can read the latest recipe state
      // without adding `recipe` to the useCallback dep array.
      if (configPats.length > 0) {
        setRecipe(prev =>
          prev.allow_url_patterns.length === 0
            ? { ...prev, allow_url_patterns: configPats }
            : prev
        );
      }
    } catch (e: any) {
      setFilterSimError(String(e));
    } finally {
      setLoadingFilterUrls(false);
    }
  }, [id]);

  const runFilterSim = useCallback(async () => {
    const urls = filterSimUrls.split("\n").map((u) => u.trim()).filter(Boolean);
    if (!urls.length) return;
    setFilterSimLoading(true);
    setFilterSimError(null);
    setFilterSimResults(null);
    try {
      const resp = await fetch("/api/scrape/test-url-filter", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          urls,
          allow_url_patterns: recipe.allow_url_patterns.length > 0 ? recipe.allow_url_patterns : filterSimAllowPats,
          must_contain: recipe.must_contain,
          block_url_patterns: recipe.block_url_patterns,
        }),
      });
      const data = await resp.json();
      if (!data?.ok) { setFilterSimError(data?.error || "Unknown error"); return; }
      setFilterSimResults(data);
    } catch (e: any) {
      setFilterSimError(String(e));
    } finally {
      setFilterSimLoading(false);
    }
  }, [filterSimUrls, filterSimAllowPats, recipe.allow_url_patterns, recipe.must_contain, recipe.block_url_patterns]);

  // ── Listing-page link fetcher handler ──
  const fetchListingLinks = useCallback(async () => {
    const url = listingFetchUrl.trim();
    if (!url) return;
    setFetchingListing(true);
    setListingFetchResult(null);
    setSelectedListingLinks(new Set());
    try {
      const resp = await fetch("/api/scrape/fetch-listing-links", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url, uni_id: id ? Number(id) : undefined }),
      });
      const data = await resp.json();
      setListingFetchResult(data);
      if (data.links?.length) {
        setSelectedListingLinks(new Set(data.links as string[]));
      }
    } catch (e: any) {
      setListingFetchResult({ links: [], method: "none", total_raw: 0, needs_browser: false, error: String(e) });
    } finally {
      setFetchingListing(false);
    }
  }, [listingFetchUrl, id]);

  const addSelectedListingLinks = useCallback(() => {
    if (!selectedListingLinks.size) return;
    const existing = new Set(recipe.extra_course_urls);
    const toAdd = [...selectedListingLinks].filter(l => !existing.has(l));
    if (!toAdd.length) {
      toast({ title: "All links already in Extra Course URLs" });
      return;
    }
    patchRecipe({ extra_course_urls: [...recipe.extra_course_urls, ...toAdd] });
    toast({ title: `Added ${toAdd.length} course URL${toAdd.length !== 1 ? "s" : ""}` });
    setListingFetchResult(null);
    setListingFetchUrl("");
    setSelectedListingLinks(new Set());
  }, [selectedListingLinks, recipe.extra_course_urls]);

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
      const data = await resp.json();
      if (data.yaml_slug) setYamlSlug(data.yaml_slug);
      const yamlMsg = data.yaml_slug
        ? `Saved to database and synced to ${data.yaml_slug}.yaml`
        : data.yaml_write_error
          ? `Saved to database (YAML write failed: ${data.yaml_write_error})`
          : "Saved to database (no YAML file found for this university)";
      toast({ title: "Recipe saved", description: yamlMsg });
    } catch (e: any) {
      toast({ title: "Save failed", description: e.message, variant: "destructive" });
    } finally {
      setSaving(false);
    }
  }, [id, recipe]);

  // ── Test JSON API (via backend to avoid CORS) ──
  const testApi = useCallback(async () => {
    const api = recipe.api;
    if (!api?.endpoint) {
      toast({ title: "No endpoint", description: "Enter an API endpoint URL first.", variant: "destructive" });
      return;
    }
    setTesting(true);
    setTestResult(null);
    try {
      const resp = await fetch(`/api/universities/${id}/recipe/test-api`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api }),
      });
      if (!resp.ok) throw new Error(await resp.text());
      const data: ApiTestResult = await resp.json();
      setTestResult(data);
      if (data.status === "ok") {
        toast({ title: `API OK — ${data.page1_count} courses on page 1${data.total_from_api != null ? `, ${data.total_from_api} total` : ""}` });
      } else {
        toast({ title: `API test: ${data.status}`, variant: "destructive" });
      }
    } catch (e: any) {
      toast({ title: "API test failed", description: e.message, variant: "destructive" });
    } finally {
      setTesting(false);
    }
  }, [id, recipe.api]);

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
          query_params: recipe.api.query_params,
          pagination: recipe.api.pagination,
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
  const EMPTY_SEARCHSTAX: SearchStaxConfig = {
    enabled: true, endpoint: "", token_env: "", filter_query: "sectionType_s:course",
    currency: "GBP", url_base: "", location_strip_prefixes: [],
    exclude_part_time: false, links_only: false, field_map_as_payload: false,
    field_map: {}, page_size: undefined, max_pages: undefined,
  };
  const patchSearchStax = (patch: Partial<SearchStaxConfig>) =>
    setRecipe(r => ({ ...r, searchstax: { ...EMPTY_SEARCHSTAX, ...(r.searchstax || {}), ...patch } }));
  const patchSearchStaxFieldMap = (patch: Partial<SearchStaxFieldMap>) =>
    setRecipe(r => ({ ...r, searchstax: { ...EMPTY_SEARCHSTAX, ...(r.searchstax || {}), field_map: { ...(r.searchstax?.field_map || {}), ...patch } } }));

  const EMPTY_SSR_PROP: SsrPropDiscovery = {
    listing_pages: [],
    prop_attr: ":courses-data",
    slug_field: "slug",
    name_field: "title",
    url_suffix: "/{year}/",
    year_field: "",
    year_filter_prefix: "{short_year}/",
    browser_fallback_xpath: "",
    browser_wait_selector: "",
    course_url_pattern: "",
  };
  const patchSsrProp = (patch: Partial<SsrPropDiscovery>) =>
    setRecipe(r => ({ ...r, ssr_prop_discovery: { ...EMPTY_SSR_PROP, ...(r.ssr_prop_discovery || {}), ...patch } }));
  const addSsrListingPage = () =>
    patchSsrProp({ listing_pages: [...(recipe.ssr_prop_discovery?.listing_pages ?? []), { url: "", url_prefix: "", label: "" }] });
  const removeSsrListingPage = (i: number) =>
    patchSsrProp({ listing_pages: (recipe.ssr_prop_discovery?.listing_pages ?? []).filter((_, j) => j !== i) });
  const patchSsrListingPage = (i: number, patch: Partial<SsrPropListingPage>) => {
    const pages = [...(recipe.ssr_prop_discovery?.listing_pages ?? [])];
    pages[i] = { ...pages[i], ...patch };
    patchSsrProp({ listing_pages: pages });
  };

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
          <p className="text-muted-foreground text-sm flex items-center gap-2 flex-wrap">
            {uniName} · <span className="font-mono text-xs">{scrapeUrl}</span>
            {yamlSlug && (
              <a
                href={`/settings/scraper-configs?slug=${yamlSlug}`}
                className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full bg-green-100 text-green-800 hover:bg-green-200 dark:bg-green-900/30 dark:text-green-300 font-mono"
                title="Open raw YAML editor for this university"
              >
                <Code2 className="h-3 w-3" />
                {yamlSlug}.yaml
              </a>
            )}
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
                          <div className="font-medium mb-1">Sample dropped courses ({r.dropped_samples.length} total):</div>
                          {r.dropped_samples.slice(0, 8).map((u: string, i: number) => {
                            const seg = u.replace(/\/$/, "").split("/").filter(Boolean).pop() ?? u;
                            const name = seg.replace(/[-_]/g, " ").replace(/\b\w/g, (c: string) => c.toUpperCase());
                            return (
                              <div key={i} className="flex items-baseline gap-2 py-0.5 border-b border-gray-50 last:border-0">
                                <span className="text-red-700 font-medium shrink-0 min-w-0">{name}</span>
                                <span className="font-mono text-[10px] text-gray-400 truncate">{u}</span>
                              </div>
                            );
                          })}
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
          <TabsTrigger value="searchstax" className="flex items-center gap-1"><Zap className="h-3 w-3" /> SearchStax</TabsTrigger>
          <TabsTrigger value="ssr_prop" className="flex items-center gap-1"><Code2 className="h-3 w-3" /> SSR Prop</TabsTrigger>
          <TabsTrigger value="api" className="flex items-center gap-1"><Database className="h-3 w-3" /> JSON API</TabsTrigger>
          <TabsTrigger value="filters" className="flex items-center gap-1"><Filter className="h-3 w-3" /> URL Filters</TabsTrigger>
          <TabsTrigger value="selectors" className="flex items-center gap-1"><Code2 className="h-3 w-3" /> Field Selectors</TabsTrigger>
          <TabsTrigger value="fees" className="flex items-center gap-1"><DollarSign className="h-3 w-3" /> Fee Rules</TabsTrigger>
          <TabsTrigger value="english" className="flex items-center gap-1"><BookOpen className="h-3 w-3" /> IELTS & Intake</TabsTrigger>
          <TabsTrigger value="browser" className="flex items-center gap-1"><MousePointerClick className="h-3 w-3" /> Browser Actions</TabsTrigger>
          <TabsTrigger value="names" className="flex items-center gap-1"><Type className="h-3 w-3" /> Course Names</TabsTrigger>
          <TabsTrigger value="campus" className="flex items-center gap-1"><MapPin className="h-3 w-3" /> Campus & Location</TabsTrigger>
          <TabsTrigger value="year" className="flex items-center gap-1"><Calendar className="h-3 w-3" /> Year & Duplicates</TabsTrigger>
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

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <Label>Max Discovery Candidates</Label>
                  <Input
                    type="number"
                    value={recipe.max_candidates ?? ""}
                    onChange={e => patchRecipe({ max_candidates: e.target.value ? parseInt(e.target.value) : null })}
                    placeholder="default: 200"
                    className="mt-1 text-sm"
                  />
                  <p className="text-xs text-muted-foreground mt-1">
                    Max URLs collected during BFS crawl. Increase (e.g. 500) when discovery stops early and misses categories — e.g. Swinburne has 20+ category pages.
                  </p>
                </div>
                <div>
                  <Label>Max BFS Pages</Label>
                  <Input
                    type="number"
                    value={recipe.bfs_page_budget ?? ""}
                    onChange={e => patchRecipe({ bfs_page_budget: e.target.value ? parseInt(e.target.value) : null })}
                    placeholder="default: 25"
                    className="mt-1 text-sm"
                  />
                  <p className="text-xs text-muted-foreground mt-1">
                    Max pages crawled during BFS discovery. Increase (e.g. 60) when the log shows discovery stopping before all category pages are visited.
                  </p>
                </div>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <Label>Browser Discovery Timeout (seconds)</Label>
                  <Input
                    type="number"
                    value={recipe.browser_time_budget_s ?? ""}
                    onChange={e => patchRecipe({ browser_time_budget_s: e.target.value ? parseInt(e.target.value) : null })}
                    placeholder="default: 90"
                    className="mt-1 text-sm"
                  />
                  <p className="text-xs text-muted-foreground mt-1">
                    Max seconds for browser-based discovery. Increase (e.g. 180) for slow JS-heavy sites like SCU.
                  </p>
                </div>
                <div>
                  <Label>Browser Early Stop (courses)</Label>
                  <Input
                    type="number"
                    value={recipe.browser_early_stop_courses ?? ""}
                    onChange={e => patchRecipe({ browser_early_stop_courses: e.target.value ? parseInt(e.target.value) : null })}
                    placeholder="default: 100"
                    className="mt-1 text-sm"
                  />
                  <p className="text-xs text-muted-foreground mt-1">
                    Stop browser discovery once this many course links are found. Raise if the university has more courses.
                  </p>
                </div>
              </div>

              <Separator />

              <StringListEditor
                label="Discovery Seed URLs"
                values={recipe.seed_urls}
                onChange={v => patchRecipe({ seed_urls: v })}
                placeholder="https://example.com/study/undergraduate/courses"
                helpText="Course-listing pages visited first during discovery (BFS and browser mode). These are queued at highest priority so the crawler goes there before anywhere else. Use for JS-heavy sites like SCU — add the undergraduate/postgraduate listing pages, not just the homepage."
              />

              <StringListEditor
                label="JS-Rendered Listing Pages"
                values={recipe.render_listing_pages}
                onChange={v => patchRecipe({ render_listing_pages: v })}
                placeholder="https://example.ac.uk/study/courses?page=2"
                helpText="Use when the course listing is JS-rendered — static fetch returns a near-empty shell (e.g. 'Search results update instantly…'). Each URL is fetched via Scrape.do headless Chrome so JavaScript executes and course card links appear. Add one URL per pagination page. Funnelback sites use ?&start_rank=41 increments; React/Vue search pages use ?page=N. Pair with allow_url_patterns (URL Filter tab) to prevent BFS from following non-course links discovered from these pages."
              />

              <StringListEditor
                label="Extra Course URLs (direct injection)"
                values={recipe.extra_course_urls}
                onChange={v => patchRecipe({ extra_course_urls: v })}
                placeholder="https://example.com/study/specific-course"
                helpText="Individual course pages injected directly after discovery — bypasses all crawling. Use for courses that no crawler can find."
              />

              {/* ── Listing-page link fetcher ─────────────────────────────── */}
              <div className="rounded-lg border border-dashed bg-muted/30 p-4 space-y-3">
                <div>
                  <p className="text-sm font-medium flex items-center gap-2">
                    <Link className="h-4 w-4 text-muted-foreground" />
                    Fetch courses from a search or listing page
                  </p>
                  <p className="text-xs text-muted-foreground mt-1">
                    Paste any course-listing or search-results URL. The system fetches the page and extracts all course links automatically — no manual copying needed.
                  </p>
                </div>
                <div className="flex gap-2">
                  <Input
                    className="text-xs font-mono"
                    placeholder="https://www.example.ac.uk/study/search?level=undergraduate"
                    value={listingFetchUrl}
                    onChange={e => { setListingFetchUrl(e.target.value); setListingFetchResult(null); }}
                    onKeyDown={e => e.key === "Enter" && fetchListingLinks()}
                  />
                  <Button
                    type="button"
                    variant="secondary"
                    size="sm"
                    onClick={fetchListingLinks}
                    disabled={fetchingListing || !listingFetchUrl.trim()}
                    className="shrink-0"
                  >
                    {fetchingListing
                      ? <><Loader2 className="h-3.5 w-3.5 animate-spin mr-1" /> Fetching…</>
                      : <><Play className="h-3.5 w-3.5 mr-1" /> Fetch</>}
                  </Button>
                </div>

                {listingFetchResult && (
                  <div className="space-y-2">
                    {listingFetchResult.error && !listingFetchResult.links.length && (
                      <div className="rounded-md bg-destructive/10 border border-destructive/20 p-3 text-xs text-destructive">
                        {listingFetchResult.error}
                      </div>
                    )}

                    {listingFetchResult.needs_browser && !listingFetchResult.links.length && (
                      <div className="rounded-md bg-amber-50 border border-amber-200 p-3 space-y-2">
                        <p className="text-xs font-medium text-amber-800 flex items-center gap-1.5">
                          <AlertTriangle className="h-3.5 w-3.5" />
                          No course links found — this looks like a JavaScript-rendered page
                        </p>
                        <p className="text-xs text-amber-700">
                          The page returned {listingFetchResult.total_raw} links total but none matched course URL patterns.
                          JS-rendered pages need a browser to load their content. Try either:
                        </p>
                        <ul className="text-xs text-amber-700 list-disc pl-4 space-y-1">
                          <li>Add the URL to <strong>Discovery Seed URLs</strong> above and set Discovery Strategy to <strong>Browser</strong></li>
                          <li>Use the sitemap — check <code className="bg-amber-100 px-1 rounded">{new URL(listingFetchUrl.trim()).origin}/sitemap.xml</code></li>
                        </ul>
                      </div>
                    )}

                    {listingFetchResult.links.length > 0 && (
                      <div className="space-y-2">
                        <div className="flex items-center justify-between">
                          <p className="text-xs text-muted-foreground">
                            Found <strong>{listingFetchResult.links.length}</strong> course link{listingFetchResult.links.length !== 1 ? "s" : ""}
                            {" "}via {listingFetchResult.method === "static" ? "direct HTTP" : listingFetchResult.method === "static_proxy" ? "proxy" : listingFetchResult.method}
                            {" "}({listingFetchResult.total_raw} total links on page)
                          </p>
                          <div className="flex gap-1.5">
                            <Button type="button" variant="ghost" size="sm" className="h-6 text-xs px-2"
                              onClick={() => setSelectedListingLinks(new Set(listingFetchResult.links))}>
                              All
                            </Button>
                            <Button type="button" variant="ghost" size="sm" className="h-6 text-xs px-2"
                              onClick={() => setSelectedListingLinks(new Set())}>
                              None
                            </Button>
                          </div>
                        </div>
                        <div className="max-h-48 overflow-y-auto rounded-md border bg-background divide-y text-xs">
                          {listingFetchResult.links.map(link => (
                            <label key={link} className="flex items-center gap-2 px-3 py-1.5 hover:bg-muted/40 cursor-pointer">
                              <input
                                type="checkbox"
                                className="h-3 w-3 accent-primary"
                                checked={selectedListingLinks.has(link)}
                                onChange={e => {
                                  const next = new Set(selectedListingLinks);
                                  e.target.checked ? next.add(link) : next.delete(link);
                                  setSelectedListingLinks(next);
                                }}
                              />
                              <span className="font-mono truncate text-muted-foreground">{link}</span>
                            </label>
                          ))}
                        </div>
                        <Button
                          type="button"
                          size="sm"
                          className="w-full"
                          disabled={!selectedListingLinks.size}
                          onClick={addSelectedListingLinks}
                        >
                          <Plus className="h-3.5 w-3.5 mr-1" />
                          Add {selectedListingLinks.size > 0 ? selectedListingLinks.size : ""} selected to Extra Course URLs
                        </Button>
                      </div>
                    )}
                  </div>
                )}
              </div>
            </CardContent>
          </Card>
        </TabsContent>

        {/* ── 1b. SearchStax Solr Provider ────────────────────────────────── */}
        <TabsContent value="searchstax">
          <Card>
            <CardHeader>
              <div className="flex items-start justify-between">
                <div>
                  <CardTitle className="text-base flex items-center gap-2"><Zap className="h-4 w-4" /> SearchStax Solr Provider</CardTitle>
                  <CardDescription className="mt-1">
                    Use when the university site is a React SPA that queries a Solr/SearchStax core client-side
                    (e.g. Huddersfield, Wolverhampton). Find the endpoint in DevTools → Network → filter XHR → look
                    for <code className="text-xs">searchcloud-*.searchstax.com/.../emselect</code>.
                  </CardDescription>
                </div>
                <div className="flex items-center gap-2 shrink-0 ml-4">
                  <span className="text-xs text-muted-foreground">{recipe.searchstax ? "Configured" : "Not configured"}</span>
                  <Switch
                    checked={!!recipe.searchstax}
                    onCheckedChange={v => v
                      ? patchSearchStax({})
                      : setRecipe(r => ({ ...r, searchstax: undefined }))
                    }
                  />
                </div>
              </div>
            </CardHeader>
            {recipe.searchstax && (
              <CardContent className="space-y-6">
                {/* Core connection fields */}
                <div className="grid grid-cols-1 gap-4">
                  <div>
                    <Label>Solr Endpoint URL</Label>
                    <Input
                      value={recipe.searchstax.endpoint}
                      onChange={e => patchSearchStax({ endpoint: e.target.value })}
                      placeholder="https://searchcloud-1-eu-west-2.searchstax.com/29847/<core-id>/emselect"
                      className="mt-1 text-sm font-mono"
                    />
                    <p className="text-xs text-muted-foreground mt-1">Copy from DevTools request URL. Never commit tokens here.</p>
                  </div>
                </div>

                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <Label>Token Env Var Name</Label>
                    <Input
                      value={recipe.searchstax.token_env}
                      onChange={e => patchSearchStax({ token_env: e.target.value })}
                      placeholder="MY_UNI_SEARCHSTAX_TOKEN"
                      className="mt-1 text-sm font-mono"
                    />
                    <p className="text-xs text-muted-foreground mt-1">Name of the Replit secret that holds the bearer token.</p>
                  </div>
                  <div>
                    <Label>Filter Query (fq)</Label>
                    <Input
                      value={recipe.searchstax.filter_query}
                      onChange={e => patchSearchStax({ filter_query: e.target.value })}
                      placeholder="sectionType_s:course"
                      className="mt-1 text-sm font-mono"
                    />
                    <p className="text-xs text-muted-foreground mt-1">Solr <code>fq</code> parameter — copy from DevTools request.</p>
                  </div>
                </div>

                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <Label>Currency</Label>
                    <Select value={recipe.searchstax.currency} onValueChange={v => patchSearchStax({ currency: v })}>
                      <SelectTrigger className="mt-1"><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="GBP">GBP — British Pounds</SelectItem>
                        <SelectItem value="AUD">AUD — Australian Dollars</SelectItem>
                        <SelectItem value="NZD">NZD — New Zealand Dollars</SelectItem>
                        <SelectItem value="USD">USD — US Dollars</SelectItem>
                        <SelectItem value="EUR">EUR — Euros</SelectItem>
                        <SelectItem value="CAD">CAD — Canadian Dollars</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  <div>
                    <Label>URL Base</Label>
                    <Input
                      value={recipe.searchstax.url_base || ""}
                      onChange={e => patchSearchStax({ url_base: e.target.value || undefined })}
                      placeholder="https://www.example.ac.uk/courses"
                      className="mt-1 text-sm font-mono"
                    />
                    <p className="text-xs text-muted-foreground mt-1">
                      Prepend to bare course codes in <code>url_t</code> (e.g. WLV SITS codes like "WR006J01UMU").
                      Leave blank when Solr returns full URLs.
                    </p>
                  </div>
                </div>

                <Separator />

                {/* Toggles */}
                <div className="grid grid-cols-3 gap-4">
                  <div className="flex items-center justify-between rounded-lg border p-3">
                    <div>
                      <p className="text-sm font-medium">Exclude Part-time</p>
                      <p className="text-xs text-muted-foreground mt-0.5">Drop Part-time from mixed-mode courses; skip Part-time-only courses.</p>
                    </div>
                    <Switch
                      checked={recipe.searchstax.exclude_part_time}
                      onCheckedChange={v => patchSearchStax({ exclude_part_time: v })}
                    />
                  </div>
                  <div className="flex items-center justify-between rounded-lg border p-3">
                    <div>
                      <p className="text-sm font-medium">Field Map as Payload</p>
                      <p className="text-xs text-muted-foreground mt-0.5">Use Solr doc fields as course data directly (no HTML fetch per course).</p>
                    </div>
                    <Switch
                      checked={recipe.searchstax.field_map_as_payload}
                      onCheckedChange={v => patchSearchStax({ field_map_as_payload: v })}
                    />
                  </div>
                  <div className="flex items-center justify-between rounded-lg border p-3">
                    <div>
                      <p className="text-sm font-medium">Links Only</p>
                      <p className="text-xs text-muted-foreground mt-0.5">Only use Solr for discovery; still fetch each course HTML page.</p>
                    </div>
                    <Switch
                      checked={recipe.searchstax.links_only}
                      onCheckedChange={v => patchSearchStax({ links_only: v })}
                    />
                  </div>
                </div>

                <Separator />

                {/* Location strip prefixes */}
                <StringListEditor
                  label="Location Strip Prefixes"
                  values={recipe.searchstax.location_strip_prefixes}
                  onChange={v => patchSearchStax({ location_strip_prefixes: v })}
                  placeholder='University: '
                  helpText='Strip these prefixes from raw Solr location values. Example: "University: City Campus" → "City Campus". Add a trailing space if the prefix includes one.'
                />

                <Separator />

                {/* Field map */}
                <div>
                  <Label className="text-sm font-semibold">Solr Field Name Mapping</Label>
                  <p className="text-xs text-muted-foreground mt-1 mb-3">
                    Override only the Solr field names that differ from the HUD defaults.
                    Leave blank to use defaults (<code>title_t</code>, <code>url_t</code>, <code>award_s</code>).
                  </p>
                  <div className="grid grid-cols-2 gap-3">
                    {([ 
                      { key: "degree_level", label: "Degree Level", placeholder: "level_s" },
                      { key: "study_mode",   label: "Study Mode",   placeholder: "multi_mode_ss" },
                      { key: "duration",     label: "Duration",     placeholder: "multi_duration_ss" },
                      { key: "intake_dates", label: "Intake Dates", placeholder: "multi_course_start_date_ss" },
                      { key: "category",     label: "Category",     placeholder: "subject_area_ss" },
                      { key: "location",     label: "Location",     placeholder: "multi_location_ss" },
                    ] as const).map(({ key, label, placeholder }) => (
                      <div key={key}>
                        <Label className="text-xs">{label}</Label>
                        <Input
                          value={(recipe.searchstax!.field_map as any)[key] || ""}
                          onChange={e => patchSearchStaxFieldMap({ [key]: e.target.value || undefined })}
                          placeholder={placeholder}
                          className="mt-1 text-sm font-mono"
                        />
                      </div>
                    ))}
                  </div>
                </div>

                <Separator />

                {/* Pagination */}
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <Label>Page Size</Label>
                    <Input
                      type="number"
                      value={recipe.searchstax.page_size ?? ""}
                      onChange={e => patchSearchStax({ page_size: e.target.value ? parseInt(e.target.value) : undefined })}
                      placeholder="default: 200"
                      className="mt-1 text-sm"
                    />
                    <p className="text-xs text-muted-foreground mt-1">Solr rows per page. Only change if the default misses courses.</p>
                  </div>
                  <div>
                    <Label>Max Pages</Label>
                    <Input
                      type="number"
                      value={recipe.searchstax.max_pages ?? ""}
                      onChange={e => patchSearchStax({ max_pages: e.target.value ? parseInt(e.target.value) : undefined })}
                      placeholder="default: 20"
                      className="mt-1 text-sm"
                    />
                    <p className="text-xs text-muted-foreground mt-1">Safety cap on Solr pagination. Raise for universities with &gt;4000 courses.</p>
                  </div>
                </div>
              </CardContent>
            )}
          </Card>
        </TabsContent>

        {/* ── 1c. SSR Prop Discovery ──────────────────────────────────────── */}
        <TabsContent value="ssr_prop">
          <Card>
            <CardHeader>
              <div className="flex items-start justify-between">
                <div>
                  <CardTitle className="text-base flex items-center gap-2">
                    <Code2 className="h-4 w-4" /> SSR Prop Discovery
                  </CardTitle>
                  <CardDescription className="mt-1">
                    For universities whose listing pages embed the full course catalogue as a JSON array
                    inside a server-rendered HTML attribute — no browser or Playwright needed.
                    <br />
                    <span className="font-medium">How to detect:</span> View Page Source → search for a
                    known course slug. If a JSON array appears inside an HTML attribute (e.g.{" "}
                    <code className="text-xs">:courses-data</code>,{" "}
                    <code className="text-xs">data-courses</code>), use this tier.
                    Lancaster University is the reference implementation (538 courses, 0 Gemini cost).
                  </CardDescription>
                </div>
                <div className="flex items-center gap-2 shrink-0 ml-4">
                  <span className="text-xs text-muted-foreground">
                    {recipe.ssr_prop_discovery ? "Configured" : "Not configured"}
                  </span>
                  <Switch
                    checked={!!recipe.ssr_prop_discovery}
                    onCheckedChange={v =>
                      v
                        ? patchSsrProp({})
                        : setRecipe(r => ({ ...r, ssr_prop_discovery: undefined }))
                    }
                  />
                </div>
              </div>
            </CardHeader>
            {recipe.ssr_prop_discovery && (
              <CardContent className="space-y-6">

                {/* Listing pages */}
                <div className="space-y-3">
                  <div className="flex items-center justify-between">
                    <div>
                      <Label>Listing Pages</Label>
                      <p className="text-xs text-muted-foreground mt-0.5">
                        One entry per catalogue page. Each page is fetched with plain HTTP and the JSON
                        prop is extracted. Course URLs are built as{" "}
                        <code className="text-xs">{"{url_prefix}/{slug}{url_suffix}"}</code>.
                      </p>
                    </div>
                    <Button type="button" variant="outline" size="sm" onClick={addSsrListingPage}>
                      <Plus className="h-3 w-3 mr-1" /> Add Page
                    </Button>
                  </div>

                  {recipe.ssr_prop_discovery.listing_pages.length === 0 && (
                    <p className="text-xs text-muted-foreground italic">
                      No listing pages yet — click "Add Page" to add the undergraduate/postgraduate
                      listing URLs.
                    </p>
                  )}

                  {recipe.ssr_prop_discovery.listing_pages.map((page, i) => (
                    <Card key={i} className="border-dashed">
                      <CardContent className="pt-4 space-y-3">
                        <div className="flex items-center justify-between">
                          <span className="text-xs font-semibold text-muted-foreground">
                            Page {i + 1}
                          </span>
                          <button
                            onClick={() => removeSsrListingPage(i)}
                            className="text-destructive hover:opacity-70"
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </button>
                        </div>
                        <div>
                          <Label className="text-xs">Listing Page URL</Label>
                          <Input
                            value={page.url}
                            onChange={e => patchSsrListingPage(i, { url: e.target.value })}
                            placeholder="https://www.myuni.edu.au/courses/undergraduate/"
                            className="mt-1 text-sm font-mono"
                          />
                          <p className="text-xs text-muted-foreground mt-1">
                            The page fetched via httpx to extract the JSON prop.
                          </p>
                        </div>
                        <div>
                          <Label className="text-xs">URL Prefix (base for course URLs)</Label>
                          <Input
                            value={page.url_prefix}
                            onChange={e => patchSsrListingPage(i, { url_prefix: e.target.value })}
                            placeholder="https://www.myuni.edu.au/courses/undergraduate/"
                            className="mt-1 text-sm font-mono"
                          />
                          <p className="text-xs text-muted-foreground mt-1">
                            Prepended to each slug: <code className="text-xs">{"{url_prefix}/{slug}{url_suffix}"}</code>.
                            Usually the same as the listing page URL.
                          </p>
                        </div>
                        <div>
                          <Label className="text-xs">Label <span className="font-normal text-muted-foreground">(for logs)</span></Label>
                          <Input
                            value={page.label}
                            onChange={e => patchSsrListingPage(i, { label: e.target.value })}
                            placeholder="Undergraduate"
                            className="mt-1 text-sm"
                          />
                        </div>
                      </CardContent>
                    </Card>
                  ))}
                </div>

                <Separator />

                {/* Prop config */}
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <Label>HTML Prop Attribute</Label>
                    <Input
                      value={recipe.ssr_prop_discovery.prop_attr}
                      onChange={e => patchSsrProp({ prop_attr: e.target.value })}
                      placeholder=":courses-data"
                      className="mt-1 text-sm font-mono"
                    />
                    <p className="text-xs text-muted-foreground mt-1">
                      HTML attribute whose value is the JSON array.
                      Vue: <code className="text-xs">:courses-data</code> ·
                      React: <code className="text-xs">data-courses</code>
                    </p>
                  </div>
                  <div>
                    <Label>URL Suffix</Label>
                    <Input
                      value={recipe.ssr_prop_discovery.url_suffix}
                      onChange={e => patchSsrProp({ url_suffix: e.target.value })}
                      placeholder="/{year}/"
                      className="mt-1 text-sm font-mono"
                    />
                    <p className="text-xs text-muted-foreground mt-1">
                      Appended after slug.
                      Tokens: <code className="text-xs">{"{year}"}</code> = 2026,{" "}
                      <code className="text-xs">{"{short_year}"}</code> = 26.
                    </p>
                  </div>
                </div>

                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <Label>Slug Field</Label>
                    <Input
                      value={recipe.ssr_prop_discovery.slug_field}
                      onChange={e => patchSsrProp({ slug_field: e.target.value })}
                      placeholder="slug"
                      className="mt-1 text-sm font-mono"
                    />
                    <p className="text-xs text-muted-foreground mt-1">
                      JSON field whose value is the URL slug.
                    </p>
                  </div>
                  <div>
                    <Label>Name Field</Label>
                    <Input
                      value={recipe.ssr_prop_discovery.name_field}
                      onChange={e => patchSsrProp({ name_field: e.target.value })}
                      placeholder="title"
                      className="mt-1 text-sm font-mono"
                    />
                    <p className="text-xs text-muted-foreground mt-1">
                      JSON field whose value is the course name.
                    </p>
                  </div>
                </div>

                <Separator />

                {/* Year filter */}
                <div>
                  <Label className="text-sm font-medium">Year Filter <span className="font-normal text-muted-foreground">(optional)</span></Label>
                  <p className="text-xs text-muted-foreground mt-0.5 mb-3">
                    When set, only JSON objects whose year field starts with the prefix are included.
                    Leave blank to include all objects regardless of year.
                  </p>
                  <div className="grid grid-cols-2 gap-4">
                    <div>
                      <Label className="text-xs">Year Field</Label>
                      <Input
                        value={recipe.ssr_prop_discovery.year_field}
                        onChange={e => patchSsrProp({ year_field: e.target.value })}
                        placeholder="entryYear"
                        className="mt-1 text-sm font-mono"
                      />
                      <p className="text-xs text-muted-foreground mt-1">
                        JSON field to check (e.g. <code className="text-xs">entryYear</code>).
                        Leave blank to skip year filtering.
                      </p>
                    </div>
                    <div>
                      <Label className="text-xs">Year Filter Prefix</Label>
                      <Input
                        value={recipe.ssr_prop_discovery.year_filter_prefix}
                        onChange={e => patchSsrProp({ year_filter_prefix: e.target.value })}
                        placeholder="{short_year}/"
                        className="mt-1 text-sm font-mono"
                      />
                      <p className="text-xs text-muted-foreground mt-1">
                        Lancaster: <code className="text-xs">{"{short_year}/"}</code> (format "26/27") ·
                        Plain year: <code className="text-xs">{"{year}"}</code> (format "2026")
                      </p>
                    </div>
                  </div>
                </div>

                <Separator />

                {/* Browser fallback */}
                <div>
                  <Label className="text-sm font-medium">Browser Fallback <span className="font-normal text-muted-foreground">(optional)</span></Label>
                  <p className="text-xs text-muted-foreground mt-0.5 mb-3">
                    Triggered automatically if the SSR prop is not found in the raw HTML.
                    Launches Playwright, waits for the selector, then evaluates the XPath to extract hrefs.
                  </p>
                  <div className="grid grid-cols-1 gap-4">
                    <div>
                      <Label className="text-xs">Browser Fallback XPath</Label>
                      <Input
                        value={recipe.ssr_prop_discovery.browser_fallback_xpath}
                        onChange={e => patchSsrProp({ browser_fallback_xpath: e.target.value })}
                        placeholder='//nav[contains(@class, "a-z")]//li/a/@href'
                        className="mt-1 text-sm font-mono"
                      />
                      <p className="text-xs text-muted-foreground mt-1">
                        XPath returning course hrefs from the rendered page.
                        Lancaster: <code className="text-xs">{"//nav[contains(@class, 'a-z')]//li/a/@href"}</code>
                      </p>
                    </div>
                    <div className="grid grid-cols-2 gap-4">
                      <div>
                        <Label className="text-xs">Browser Wait Selector</Label>
                        <Input
                          value={recipe.ssr_prop_discovery.browser_wait_selector}
                          onChange={e => patchSsrProp({ browser_wait_selector: e.target.value })}
                          placeholder="nav.a-z"
                          className="mt-1 text-sm font-mono"
                        />
                        <p className="text-xs text-muted-foreground mt-1">
                          CSS selector to wait for before evaluating XPath.
                        </p>
                      </div>
                      <div>
                        <Label className="text-xs">Course URL Pattern <span className="font-normal text-muted-foreground">(regex)</span></Label>
                        <Input
                          value={recipe.ssr_prop_discovery.course_url_pattern}
                          onChange={e => patchSsrProp({ course_url_pattern: e.target.value })}
                          placeholder='/courses/[a-z0-9-]+/20\d{2}/?$'
                          className="mt-1 text-sm font-mono"
                        />
                        <p className="text-xs text-muted-foreground mt-1">
                          Filters hrefs from the browser fallback. Leave blank to keep all.
                        </p>
                      </div>
                    </div>
                  </div>
                </div>

                {/* Lancaster quick-fill */}
                <div className="rounded-lg border border-blue-200 bg-blue-50 p-4">
                  <div className="flex items-center justify-between mb-2">
                    <div>
                      <div className="text-sm font-medium text-blue-800">Lancaster University preset</div>
                      <div className="text-xs text-blue-600 mt-0.5">
                        Fills all fields with Lancaster's verified config (538 courses, 0 Gemini cost).
                        Edit the listing page URLs for your university.
                      </div>
                    </div>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      className="border-blue-300 text-blue-700 hover:bg-blue-100 shrink-0 ml-4"
                      onClick={() => patchSsrProp({
                        listing_pages: [
                          { url: "https://www.lancaster.ac.uk/study/undergraduate/courses/", url_prefix: "https://www.lancaster.ac.uk/study/undergraduate/courses/", label: "Undergraduate" },
                          { url: "https://www.lancaster.ac.uk/study/postgraduate/postgraduate-courses/", url_prefix: "https://www.lancaster.ac.uk/study/postgraduate/postgraduate-courses/", label: "Postgraduate" },
                        ],
                        prop_attr: ":courses-data",
                        slug_field: "slug",
                        name_field: "title",
                        url_suffix: "/{year}/",
                        year_field: "entryYear",
                        year_filter_prefix: "{short_year}/",
                        browser_fallback_xpath: "//nav[contains(@class, 'a-z')]//li/a/@href",
                        browser_wait_selector: "nav.a-z",
                        course_url_pattern: "/study/(undergraduate/courses|postgraduate/postgraduate-courses)/[^/]+/20\\d{2}/?$",
                      })}
                    >
                      Load Preset
                    </Button>
                  </div>
                </div>

              </CardContent>
            )}
          </Card>
        </TabsContent>

        {/* ── 2. JSON API ─────────────────────────────────────────────────── */}
        <TabsContent value="api">
          <Card>
            <CardHeader>
              <div className="flex items-start justify-between">
                <div>
                  <CardTitle className="text-base flex items-center gap-2"><Database className="h-4 w-4" /> JSON / REST API Endpoint</CardTitle>
                  <CardDescription className="mt-1">
                    Configure a known JSON feed that returns the full course catalogue.
                    Example: <code className="text-xs">https://courses.hud.ac.uk/json/2025-26/sort:title</code>
                  </CardDescription>
                </div>
                <div className="flex items-center gap-2 shrink-0 ml-4">
                  <span className="text-xs text-muted-foreground">{recipe.api?.enabled === false ? "Disabled" : "Enabled"}</span>
                  <Switch
                    checked={recipe.api?.enabled !== false}
                    onCheckedChange={v => patchApi({ enabled: v ? undefined : false })}
                  />
                </div>
              </div>
            </CardHeader>
            <CardContent className="space-y-5">
              <div className="grid grid-cols-3 gap-4">
                <div className="col-span-2">
                  <Label>API Endpoint URL</Label>
                  <Input
                    value={recipe.api?.endpoint || ""}
                    onChange={e => patchApi({ endpoint: e.target.value })}
                    onPaste={e => {
                      const text = e.clipboardData.getData("text/plain");
                      if (text) {
                        e.preventDefault();
                        patchApi({ endpoint: text.trim() });
                      }
                    }}
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
                    placeholder="e.g. Results  or  data.courses  or  Items"
                    className="mt-1 text-sm font-mono"
                  />
                  <p className="text-xs text-muted-foreground mt-1">
                    Dot-separated path to the course array in the JSON response.
                  </p>
                </div>
                <div>
                  <Label>Total Count Path <span className="text-muted-foreground font-normal">(optional)</span></Label>
                  <Input
                    value={recipe.api?.count_path || ""}
                    onChange={e => patchApi({ count_path: e.target.value })}
                    placeholder="e.g. TotalCount  or  meta.total"
                    className="mt-1 text-sm font-mono"
                  />
                  <p className="text-xs text-muted-foreground mt-1">
                    Path to total record count in response — shown in Test API results.
                  </p>
                </div>
              </div>

              <div className="grid grid-cols-1 gap-4">
                <div>
                  <Label>Course URL Template <span className="text-muted-foreground font-normal">(optional)</span></Label>
                  <Input
                    value={recipe.api?.course_url_template || ""}
                    onChange={e => patchApi({ course_url_template: e.target.value })}
                    placeholder="https://uni.edu/courses/{Url}  or  https://uni.edu/{slug}"
                    className="mt-1 text-sm font-mono"
                  />
                  <p className="text-xs text-muted-foreground mt-1">
                    Python format string using JSON field names. E.g. <code>{"{Url}"}</code> or <code>{"{slug}"}</code>. Leave blank if items already contain a URL field.
                  </p>
                </div>
              </div>

              <Separator />

              <KeyValueEditor
                label="Query Parameters"
                pairs={recipe.api?.query_params || {}}
                onChange={query_params => patchApi({ query_params })}
                keyPlaceholder="Param name (e.g. category)"
                valuePlaceholder="Value (e.g. Course)"
                helpText="Static query parameters sent with every request. For Sitecore SXA: add s, itemid, category, v, etc. The pagination page number is added automatically — do not add it here."
              />

              <Separator />

              <KeyValueEditor
                label="Authorization / Request Headers"
                pairs={recipe.api?.headers || {}}
                onChange={headers => patchApi({ headers })}
                keyPlaceholder="Header name (e.g. Authorization)"
                valuePlaceholder="Header value (e.g. Token abc123)"
                helpText="HTTP headers sent with every API request. For token auth: key = Authorization, value = Token <your-token>."
              />

              <Separator />

              {/* Browser mode — needed for session-bound APIs */}
              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <div>
                    <Label>Browser Mode</Label>
                    <p className="text-xs text-muted-foreground mt-0.5">
                      Use a Playwright browser to call the API so session cookies are inherited. Required for Incapsula/Imperva-protected endpoints (e.g. Waikato).
                    </p>
                  </div>
                  <Switch
                    checked={!!recipe.api?.fetch_via_browser}
                    onCheckedChange={v => patchApi({ fetch_via_browser: v || undefined })}
                  />
                </div>
                {recipe.api?.fetch_via_browser && (
                  <div className="pl-2">
                    <Label className="text-xs">Browser Seed URL <span className="text-muted-foreground font-normal">(optional)</span></Label>
                    <Input
                      value={recipe.api?.browser_seed_url || ""}
                      onChange={e => patchApi({ browser_seed_url: e.target.value || undefined })}
                      placeholder="https://uni.edu/int/study/qualifications/"
                      className="mt-1 text-sm font-mono"
                    />
                    <p className="text-xs text-muted-foreground mt-1">
                      Page to navigate before calling the API — triggers the server's session-cookie handshake. Defaults to the university homepage.
                    </p>
                  </div>
                )}
              </div>

              <Separator />

              {/* Test button */}
              <div className="space-y-3">
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
                  {testResult && testResult.status === "ok" && (
                    <div className="text-sm text-green-600 font-medium">
                      ✓ HTTP {testResult.http_status} · {testResult.page1_count} courses on page 1
                      {testResult.total_from_api != null && ` · ${testResult.total_from_api} total`}
                    </div>
                  )}
                  {testResult && testResult.status !== "ok" && (
                    <div className="text-sm text-destructive font-medium">
                      ✗ {testResult.status}
                    </div>
                  )}
                </div>

                {testResult && testResult.status === "ok" && (
                  <div className="rounded-md border bg-muted/50 p-3 space-y-3">
                    {/* Stats row */}
                    <div className="grid grid-cols-3 gap-3 text-sm">
                      <div className="rounded bg-background p-2 text-center">
                        <div className="text-lg font-bold text-green-600">{testResult.http_status}</div>
                        <div className="text-xs text-muted-foreground">HTTP Status</div>
                      </div>
                      <div className="rounded bg-background p-2 text-center">
                        <div className="text-lg font-bold">{testResult.page1_count ?? "—"}</div>
                        <div className="text-xs text-muted-foreground">Page 1 courses</div>
                      </div>
                      <div className="rounded bg-background p-2 text-center">
                        <div className="text-lg font-bold">
                          {testResult.total_from_api != null ? testResult.total_from_api
                            : testResult.page2_count != null ? `${testResult.page2_count} p2`
                            : "—"}
                        </div>
                        <div className="text-xs text-muted-foreground">
                          {testResult.total_from_api != null ? "Total (from API)" : "Page 2 courses"}
                        </div>
                      </div>
                    </div>

                    {/* Sample course names */}
                    {testResult.sample_names && testResult.sample_names.length > 0 && (
                      <div>
                        <p className="text-xs font-medium text-muted-foreground mb-1">Sample courses:</p>
                        <ul className="text-xs space-y-0.5">
                          {testResult.sample_names.map((name, i) => (
                            <li key={i} className="text-foreground">• {name}</li>
                          ))}
                        </ul>
                      </div>
                    )}

                    {/* JSON keys for field mapping */}
                    {testResult.all_keys && testResult.all_keys.length > 0 && (
                      <div>
                        <p className="text-xs font-medium text-muted-foreground mb-1">JSON keys (use in Field Mapping):</p>
                        <p className="text-xs font-mono text-muted-foreground">
                          {testResult.all_keys.join(", ")}
                        </p>
                      </div>
                    )}

                    {/* Warnings */}
                    {testResult.warnings && testResult.warnings.length > 0 && (
                      <div>
                        {testResult.warnings.map((w, i) => (
                          <p key={i} className="text-xs text-amber-600">⚠ {w}</p>
                        ))}
                      </div>
                    )}
                  </div>
                )}

                {testResult && testResult.status !== "ok" && (
                  <div className="rounded-md border border-destructive/30 bg-destructive/5 p-3">
                    <p className="text-xs font-medium text-destructive mb-1">Test failed: {testResult.status}</p>
                    {testResult.warnings && testResult.warnings.map((w, i) => (
                      <p key={i} className="text-xs text-muted-foreground">{w}</p>
                    ))}
                    {testResult.http_status && testResult.http_status !== 200 && (
                      <p className="text-xs text-muted-foreground mt-1">
                        HTTP {testResult.http_status} — check endpoint URL, query parameters, or request headers.
                      </p>
                    )}
                  </div>
                )}
              </div>

              <Separator />

              {/* POST Body — shown when method is POST */}
              {recipe.api?.method === "POST" && (
                <>
                  <div className="space-y-2">
                    <Label>POST Body (JSON)</Label>
                    <p className="text-xs text-muted-foreground">
                      JSON body template sent with every POST request. For Elastic App Search, Algolia, and Solr APIs that accept <code>application/json</code> bodies instead of query params.
                      The pagination fields inside it are updated automatically per page.
                    </p>
                    <textarea
                      className="w-full min-h-[160px] rounded-md border border-input bg-background px-3 py-2 text-sm font-mono ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring resize-y"
                      placeholder={'{\n  "query": "",\n  "page": { "current": 1, "size": 100 }\n}'}
                      value={recipe.api?.body != null ? JSON.stringify(recipe.api.body, null, 2) : ""}
                      onChange={e => {
                        const raw = e.target.value.trim();
                        if (!raw) { patchApi({ body: null }); return; }
                        try { patchApi({ body: JSON.parse(raw) }); } catch { /* keep as-is while typing */ }
                      }}
                    />
                  </div>

                  <div className="space-y-3">
                    <div className="flex items-center justify-between">
                      <div>
                        <Label>Body Pagination</Label>
                        <p className="text-xs text-muted-foreground mt-0.5">
                          Configure how the page number is updated inside the JSON body on each paginated request (Elastic App Search style).
                        </p>
                      </div>
                      <Switch
                        checked={!!recipe.api?.body_pagination?.current_path}
                        onCheckedChange={v => patchApi({
                          body_pagination: v
                            ? { current_path: "page.current", size_path: "page.size", total_pages_path: "" }
                            : undefined
                        })}
                      />
                    </div>
                    {recipe.api?.body_pagination?.current_path !== undefined && (
                      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 pl-2">
                        <div>
                          <Label className="text-xs">Current Page Path</Label>
                          <Input
                            value={recipe.api.body_pagination?.current_path || ""}
                            onChange={e => patchApi({ body_pagination: { ...recipe.api!.body_pagination!, current_path: e.target.value } })}
                            placeholder="page.current"
                            className="mt-1 text-sm font-mono"
                          />
                          <p className="text-xs text-muted-foreground mt-1">Dot-path in body to set page number (e.g. <code>page.current</code>)</p>
                        </div>
                        <div>
                          <Label className="text-xs">Page Size Path <span className="text-muted-foreground">(opt)</span></Label>
                          <Input
                            value={recipe.api.body_pagination?.size_path || ""}
                            onChange={e => patchApi({ body_pagination: { ...recipe.api!.body_pagination!, size_path: e.target.value } })}
                            placeholder="page.size"
                            className="mt-1 text-sm font-mono"
                          />
                          <p className="text-xs text-muted-foreground mt-1">Dot-path to set page size (e.g. <code>page.size</code>)</p>
                        </div>
                        <div>
                          <Label className="text-xs">Total Pages Path <span className="text-muted-foreground">(opt)</span></Label>
                          <Input
                            value={recipe.api.body_pagination?.total_pages_path || ""}
                            onChange={e => patchApi({ body_pagination: { ...recipe.api!.body_pagination!, total_pages_path: e.target.value } })}
                            placeholder="meta.page.total_pages"
                            className="mt-1 text-sm font-mono"
                          />
                          <p className="text-xs text-muted-foreground mt-1">Dot-path in response for stop signal (e.g. <code>meta.page.total_pages</code>)</p>
                        </div>
                      </div>
                    )}
                  </div>

                  {/* Page size + max pages for body-pagination mode */}
                  <div className="grid grid-cols-2 gap-3 pl-2">
                    <div>
                      <Label className="text-xs">Page Size</Label>
                      <Input
                        type="number"
                        value={recipe.api?.page_size || ""}
                        onChange={e => patchApi({ page_size: parseInt(e.target.value) || undefined })}
                        placeholder="100"
                        className="mt-1 text-sm"
                      />
                      <p className="text-xs text-muted-foreground mt-1">Items per page (injected into body via Body Pagination paths above)</p>
                    </div>
                    <div>
                      <Label className="text-xs">Max Pages</Label>
                      <Input
                        type="number"
                        value={recipe.api?.max_pages || ""}
                        onChange={e => patchApi({ max_pages: parseInt(e.target.value) || undefined })}
                        placeholder="20"
                        className="mt-1 text-sm"
                      />
                      <p className="text-xs text-muted-foreground mt-1">Hard ceiling on pagination rounds to prevent runaway loops</p>
                    </div>
                  </div>

                  <Separator />
                </>
              )}

              {/* URL normalization — needed when API returns relative URLs */}
              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <div>
                    <Label>Relative URL Normalisation</Label>
                    <p className="text-xs text-muted-foreground mt-0.5">
                      Prepend a base URL to relative paths returned by the API (e.g. <code>/study/qualifications/ba-arts/</code> → full URL).
                    </p>
                  </div>
                  <Switch
                    checked={recipe.api?.normalize_relative_urls !== false && (!!recipe.api?.base_url || !!recipe.api?.normalize_relative_urls)}
                    onCheckedChange={v => patchApi({ normalize_relative_urls: v ? true : false, base_url: v ? recipe.api?.base_url || "" : undefined })}
                  />
                </div>
                {recipe.api?.normalize_relative_urls !== false && recipe.api?.base_url !== undefined && (
                  <div className="pl-2">
                    <Label className="text-xs">Base URL</Label>
                    <Input
                      value={recipe.api?.base_url || ""}
                      onChange={e => patchApi({ base_url: e.target.value || undefined })}
                      placeholder="https://www.uni.edu"
                      className="mt-1 text-sm font-mono"
                    />
                    <p className="text-xs text-muted-foreground mt-1">Origin prepended to relative paths (e.g. <code>https://www.waikato.ac.nz</code>)</p>
                  </div>
                )}
              </div>

              <Separator />

              {/* URL filters within API results */}
              <div className="space-y-4">
                <div>
                  <Label>API URL Allow Patterns <span className="text-muted-foreground font-normal text-xs">(optional)</span></Label>
                  <p className="text-xs text-muted-foreground mt-0.5">
                    Regex whitelist applied to URLs extracted from API results — only matching URLs are kept. Leave blank to keep all.
                  </p>
                  {(recipe.api?.api_allow_url_patterns || [""]).map((pat, i) => (
                    <RegexPatternRow
                      key={i}
                      value={pat}
                      placeholder="/study/qualifications/"
                      onChange={v => {
                        const arr = [...(recipe.api?.api_allow_url_patterns || [""])];
                        arr[i] = v;
                        patchApi({ api_allow_url_patterns: arr.filter(Boolean) });
                      }}
                      onRemove={() => {
                        const arr = (recipe.api?.api_allow_url_patterns || []).filter((_, j) => j !== i);
                        patchApi({ api_allow_url_patterns: arr });
                      }}
                    />
                  ))}
                  <Button type="button" variant="outline" size="sm" className="mt-2"
                    onClick={() => patchApi({ api_allow_url_patterns: [...(recipe.api?.api_allow_url_patterns || []), ""] })}>
                    + Add pattern
                  </Button>
                </div>
                <div>
                  <Label>API URL Block Patterns <span className="text-muted-foreground font-normal text-xs">(optional)</span></Label>
                  <p className="text-xs text-muted-foreground mt-0.5">
                    Regex blocklist applied to URLs extracted from API results — matching URLs are dropped.
                  </p>
                  {(recipe.api?.api_block_url_patterns || [""]).map((pat, i) => (
                    <RegexPatternRow
                      key={i}
                      value={pat}
                      placeholder="^\\.pdf$"
                      onChange={v => {
                        const arr = [...(recipe.api?.api_block_url_patterns || [""])];
                        arr[i] = v;
                        patchApi({ api_block_url_patterns: arr.filter(Boolean) });
                      }}
                      onRemove={() => {
                        const arr = (recipe.api?.api_block_url_patterns || []).filter((_, j) => j !== i);
                        patchApi({ api_block_url_patterns: arr });
                      }}
                    />
                  ))}
                  <Button type="button" variant="outline" size="sm" className="mt-2"
                    onClick={() => patchApi({ api_block_url_patterns: [...(recipe.api?.api_block_url_patterns || []), ""] })}>
                    + Add pattern
                  </Button>
                </div>
              </div>

              <Separator />

              {/* Additional URLs — for UG+PG split endpoints */}
              <div className="space-y-2">
                <Label>Additional API URLs <span className="text-muted-foreground font-normal text-xs">(optional)</span></Label>
                <p className="text-xs text-muted-foreground">
                  Extra endpoints called with the same settings and merged. Use when a university splits its catalogue into separate UG / PG endpoints.
                </p>
                {(recipe.api?.additional_urls || [""]).map((url, i) => (
                  <div key={i} className="flex gap-2">
                    <Input
                      value={url}
                      onChange={e => {
                        const arr = [...(recipe.api?.additional_urls || [""])];
                        arr[i] = e.target.value;
                        patchApi({ additional_urls: arr.filter(Boolean) });
                      }}
                      placeholder="https://api.uni.edu/courses/pg"
                      className="text-sm font-mono flex-1"
                    />
                    <Button type="button" variant="ghost" size="sm" onClick={() => {
                      const arr = (recipe.api?.additional_urls || []).filter((_, j) => j !== i);
                      patchApi({ additional_urls: arr });
                    }}>✕</Button>
                  </div>
                ))}
                <Button type="button" variant="outline" size="sm"
                  onClick={() => patchApi({ additional_urls: [...(recipe.api?.additional_urls || []), ""] })}>
                  + Add URL
                </Button>
              </div>

              <Separator />

              <KeyValueEditor
                label="JSON Field Mapping"
                pairs={recipe.api?.fields || {}}
                onChange={fields => patchApi({ fields })}
                keyPlaceholder="Standard field (e.g. course_name)"
                valuePlaceholder="JSON key (e.g. Title)"
                helpText="Map standard scraper field names → the actual JSON key names. Standard fields: course_name, degree_level, study_mode_raw, full_time, part_time, url_slug, duration, campus, description. Tip: run Test API above to see available JSON keys."
              />

              <Separator />

              {/* URL / Title field extraction */}
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <Label>URL Fields <span className="text-muted-foreground font-normal">(priority order)</span></Label>
                  <Input
                    value={(recipe.api?.url_fields || []).join(", ")}
                    onChange={e => patchApi({ url_fields: e.target.value.split(",").map(s => s.trim()).filter(Boolean) })}
                    placeholder="url, course_url, page_url, link"
                    className="mt-1 text-sm font-mono"
                  />
                  <p className="text-xs text-muted-foreground mt-1">
                    JSON keys tried in order to extract the course page URL from each API item.
                  </p>
                </div>
                <div>
                  <Label>Title Fields <span className="text-muted-foreground font-normal">(priority order)</span></Label>
                  <Input
                    value={(recipe.api?.title_fields || []).join(", ")}
                    onChange={e => patchApi({ title_fields: e.target.value.split(",").map(s => s.trim()).filter(Boolean) })}
                    placeholder="title, name, course_name"
                    className="mt-1 text-sm font-mono"
                  />
                  <p className="text-xs text-muted-foreground mt-1">
                    JSON keys tried in order to extract the course name from each API item.
                  </p>
                </div>
              </div>

              <Separator />

              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <div>
                    <Label>Query-String Pagination</Label>
                    <p className="text-xs text-muted-foreground mt-0.5">
                      Enable to page through results using query parameters. For POST APIs with body-embedded pagination, use Body Pagination above instead.
                    </p>
                  </div>
                  <Switch
                    checked={!!recipe.api?.pagination}
                    onCheckedChange={v => patchApi({
                      pagination: v
                        ? { type: "offset", page_param: "p", size_param: "", page_size: 20, page_start: 1, max_pages: 50 }
                        : undefined
                    })}
                  />
                </div>
                {recipe.api?.pagination && (
                  <div className="grid grid-cols-2 sm:grid-cols-5 gap-3 pl-2">
                    <div>
                      <Label className="text-xs">Page Param</Label>
                      <Input
                        value={recipe.api.pagination.page_param}
                        onChange={e => patchApi({ pagination: { ...recipe.api!.pagination!, page_param: e.target.value } })}
                        placeholder="p"
                        className="mt-1 text-sm font-mono"
                      />
                    </div>
                    <div>
                      <Label className="text-xs">First Page No.</Label>
                      <Select
                        value={String(recipe.api.pagination.page_start ?? 1)}
                        onValueChange={v => patchApi({ pagination: { ...recipe.api!.pagination!, page_start: parseInt(v) } })}
                      >
                        <SelectTrigger className="mt-1 text-sm"><SelectValue /></SelectTrigger>
                        <SelectContent>
                          <SelectItem value="1">1 (Sitecore, most)</SelectItem>
                          <SelectItem value="0">0 (zero-indexed)</SelectItem>
                        </SelectContent>
                      </Select>
                    </div>
                    <div>
                      <Label className="text-xs">Size Param <span className="text-muted-foreground">(opt)</span></Label>
                      <Input
                        value={recipe.api.pagination.size_param}
                        onChange={e => patchApi({ pagination: { ...recipe.api!.pagination!, size_param: e.target.value } })}
                        placeholder="limit"
                        className="mt-1 text-sm font-mono"
                      />
                    </div>
                    <div>
                      <Label className="text-xs">Page Size <span className="text-muted-foreground">(opt)</span></Label>
                      <Input
                        type="number"
                        value={recipe.api.pagination.page_size || ""}
                        onChange={e => patchApi({ pagination: { ...recipe.api!.pagination!, page_size: parseInt(e.target.value) || 20 } })}
                        placeholder="20"
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
                label="Course Detail URL Patterns (extraction allow-list)"
                values={recipe.course_detail_url_patterns}
                onChange={v => patchRecipe({ course_detail_url_patterns: v })}
                placeholder="e.g. /study/undergraduate/courses/[^/]+/20\d{2}/?$"
                helpText={
                  <span>
                    Regex allow-list applied <strong>at the extraction gate</strong>. Any URL matching one of these patterns is sent directly to the extractor, bypassing the global block-list entirely.{" "}
                    <strong>Critical for universities</strong> whose course URLs contain globally-blocked prefixes (e.g. <code className="text-xs">/study/undergraduate</code>, <code className="text-xs">/study/postgraduate</code>).{" "}
                    Lancaster example: <code className="text-xs">/study/undergraduate/courses/[^/]+/20\d{"{2}"}/?$</code>
                  </span>
                }
              />
              <Separator />
              <StringListEditor
                label="Block URL Patterns (regex blocklist)"
                values={recipe.block_url_patterns}
                onChange={v => patchRecipe({ block_url_patterns: v })}
                placeholder="e.g. /apprenticeship"
                helpText="Regex patterns — any URL matching one of these is dropped. E.g. /news, /events, /cpd."
                validateRegex
              />
              <Separator />
              <StringListEditor
                label="Allow URL Patterns (regex inclusion filter)"
                values={recipe.allow_url_patterns}
                onChange={v => patchRecipe({ allow_url_patterns: v })}
                placeholder="e.g. /courses/[^/?]+"
                validateRegex
                helpText={
                  <span>
                    Regex patterns — a URL is only accepted if it matches <strong>at least one</strong> of these patterns.
                    Applied before block_url_patterns and must_contain. Leave empty to allow all discovered URLs through.{" "}
                    <strong>Common trap:</strong> use <code className="text-xs">[^/?]+</code> (no slash or query-string) rather than <code className="text-xs">[^?]+</code> (allows any path depth) to avoid matching listing pages.
                    Pair with <em>Render Listing Pages</em> (Discovery tab) for JS-rendered listings — e.g.{" "}
                    <code className="text-xs">/courses/find-a-course/[^/]+/[^/?]+</code> ensures BFS only follows leaf course pages, not category hubs.
                    Serialized as <code className="text-xs">discovery.allow_url_patterns</code> in the YAML.
                  </span>
                }
              />
              <Separator />

              {/* ── Filter Simulator ─────────────────────────────────────── */}
              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <div>
                    <Label className="font-semibold text-sm">Filter Simulator</Label>
                    <p className="text-xs text-muted-foreground mt-0.5">
                      Test your current filters against real URLs to see exactly which pages are kept or dropped, and why.
                    </p>
                  </div>
                </div>

                {(recipe.allow_url_patterns.length > 0 || filterSimAllowPats.length > 0) && (
                  <div className="rounded-md border border-blue-200 bg-blue-50 p-2.5 space-y-1">
                    <p className="text-xs font-semibold text-blue-700">
                      Allow URL Patterns — applied first
                      {recipe.allow_url_patterns.length === 0 && filterSimAllowPats.length > 0 && (
                        <span className="font-normal text-blue-500 ml-1">(from saved config — add patterns above to override)</span>
                      )}
                    </p>
                    {(recipe.allow_url_patterns.length > 0 ? recipe.allow_url_patterns : filterSimAllowPats).map((p, i) => (
                      <code key={i} className="block font-mono text-xs bg-blue-100 px-1.5 py-0.5 rounded text-blue-900">{p}</code>
                    ))}
                    <p className="text-[11px] text-blue-600">URLs must match at least one of these patterns, then block_url_patterns and must_contain are applied.</p>
                  </div>
                )}

                <div className="space-y-1.5">
                  <div className="flex items-center justify-between">
                    <Label className="text-xs text-muted-foreground">URLs to test (one per line)</Label>
                    <Button
                      variant="outline"
                      size="sm"
                      className="h-7 text-xs"
                      onClick={loadFilterUrls}
                      disabled={loadingFilterUrls}
                    >
                      {loadingFilterUrls ? <Loader2 className="h-3 w-3 animate-spin mr-1" /> : <Database className="h-3 w-3 mr-1" />}
                      Load from last scrape
                    </Button>
                  </div>
                  <Textarea
                    value={filterSimUrls}
                    onChange={e => { setFilterSimUrls(e.target.value); setFilterSimResults(null); }}
                    placeholder={"https://www.swinburne.edu.au/courses/find-a-course/business/bachelor-of-business\nhttps://www.swinburne.edu.au/courses/find-a-course\nhttps://www.swinburne.edu.au/courses/find-a-course/engineering/bachelor-of-engineering"}
                    className="font-mono text-xs h-28 resize-none"
                  />
                  <div className="flex items-center gap-2">
                    <Button
                      size="sm"
                      onClick={runFilterSim}
                      disabled={filterSimLoading || !filterSimUrls.trim()}
                    >
                      {filterSimLoading ? <Loader2 className="h-3 w-3 animate-spin mr-1" /> : <Play className="h-3 w-3 mr-1" />}
                      Test Filter
                    </Button>
                    {filterSimResults && (
                      <span className="text-xs text-muted-foreground">
                        {filterSimResults.summary.kept_count} kept · {filterSimResults.summary.dropped_count} dropped ({filterSimResults.summary.drop_pct}%)
                      </span>
                    )}
                  </div>
                  {filterSimError && (
                    <p className="text-xs text-destructive">{filterSimError}</p>
                  )}
                </div>

                {filterSimResults && (
                  <div className="space-y-3">
                    {/* Impact summary */}
                    <div className="grid grid-cols-3 gap-2">
                      <div className="rounded-lg border bg-gray-50 p-2.5 text-center">
                        <div className="text-xl font-bold">{filterSimResults.summary.total}</div>
                        <div className="text-xs text-muted-foreground">Total URLs</div>
                      </div>
                      <div className="rounded-lg border bg-green-50 p-2.5 text-center">
                        <div className="text-xl font-bold text-green-700">{filterSimResults.summary.kept_count}</div>
                        <div className="text-xs text-green-600">Kept ✓</div>
                      </div>
                      <div className={`rounded-lg border p-2.5 text-center ${filterSimResults.summary.drop_pct >= 50 ? "bg-red-50 border-red-300" : "bg-amber-50"}`}>
                        <div className={`text-xl font-bold ${filterSimResults.summary.drop_pct >= 50 ? "text-red-700" : "text-amber-700"}`}>
                          {filterSimResults.summary.dropped_count}
                        </div>
                        <div className={`text-xs ${filterSimResults.summary.drop_pct >= 50 ? "text-red-600" : "text-amber-600"}`}>
                          Dropped ({filterSimResults.summary.drop_pct}%)
                        </div>
                      </div>
                    </div>

                    {/* High-drop warning with per-rule breakdown */}
                    {filterSimResults.summary.drop_pct >= 50 && (() => {
                      const ruleCounts = new Map<string, number>();
                      filterSimResults.results.filter(r => !r.passed).forEach(r => {
                        const rule = r.drop_reason || "unknown";
                        ruleCounts.set(rule, (ruleCounts.get(rule) ?? 0) + 1);
                      });
                      return (
                        <div className="rounded-lg border border-red-300 bg-red-50 p-3 space-y-2">
                          <div className="flex items-center gap-1.5 font-semibold text-red-800 text-sm">
                            <AlertTriangle className="h-4 w-4 shrink-0" />
                            This filter removes {filterSimResults.summary.drop_pct}% of URLs — check your rules
                          </div>
                          <div className="space-y-1">
                            <p className="text-xs font-semibold text-red-700">Dropped by rule</p>
                            {[...ruleCounts.entries()].sort(([,a],[,b]) => b - a).map(([rule, count]) => (
                              <div key={rule} className="flex items-center gap-2 bg-white border border-red-200 rounded px-2 py-1">
                                <code className="font-mono text-xs text-red-800 flex-1 min-w-0 truncate" title={rule}>{rule}</code>
                                <span className="text-xs font-bold text-red-700 shrink-0">{count} URLs</span>
                              </div>
                            ))}
                          </div>
                        </div>
                      );
                    })()}

                    {/* Per-URL results table */}
                    <div className="rounded-md border overflow-hidden">
                      <div className="bg-muted/60 px-3 py-1.5 flex items-center justify-between">
                        <span className="text-xs font-semibold text-muted-foreground uppercase tracking-wide">URL Filter Results</span>
                        <span className="text-xs text-muted-foreground">{filterSimResults.results.length} URLs tested</span>
                      </div>
                      <div className="divide-y max-h-[360px] overflow-y-auto">
                        {filterSimResults.results.map((r, i) => {
                          const path = r.url.replace(/^https?:\/\/[^/]+/, "") || r.url;
                          return (
                            <div key={i} className={`flex items-start gap-2 px-3 py-2 text-xs ${r.passed ? "bg-green-50" : "bg-red-50"}`}>
                              <span className="shrink-0 mt-0.5">{r.passed ? "✅" : "❌"}</span>
                              <div className="flex-1 min-w-0">
                                <div className="font-mono truncate text-gray-800" title={r.url}>{path}</div>
                                {r.passed && r.matching_allow_pattern && (
                                  <div className="text-[11px] text-green-600 mt-0.5">
                                    allow: <code className="font-mono bg-green-100 px-0.5 rounded">{r.matching_allow_pattern}</code>
                                  </div>
                                )}
                                {!r.passed && r.drop_reason && (
                                  <div className="text-[11px] text-red-600 mt-0.5">{r.drop_reason}</div>
                                )}
                              </div>
                              <span className={`shrink-0 text-[11px] font-semibold ${r.passed ? "text-green-600" : "text-red-600"}`}>
                                {r.passed ? "Kept" : "Dropped"}
                              </span>
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  </div>
                )}
              </div>

              <Separator />
              {/* ── URL Rewrites ──────────────────────────────────────────── */}
              <div>
                <Label className="font-semibold text-sm">URL Rewrites — Append Query Parameters</Label>
                <p className="text-xs text-muted-foreground mt-0.5 mb-3">
                  Append query parameters to every course URL before the scraper fetches it. Use this to switch a
                  university site into its international-student view without editing code — e.g. adding{" "}
                  <code className="font-mono bg-muted px-1 rounded">international=true</code> for JCU shows the
                  International tab (fees, IELTS, intakes) instead of the domestic default.
                  Each rule fires when the URL hostname matches <em>and</em> (if set) the path contains the given substring.
                </p>
                <div className="space-y-2">
                  {(recipe.url_rewrites ?? []).map((rw, i) => (
                    <div key={i} className="flex gap-2 items-end p-3 rounded-md border bg-muted/40">
                      <div className="grid grid-cols-3 gap-3 flex-1">
                        <div>
                          <Label className="text-xs mb-1 block">Hostname</Label>
                          <Input
                            value={rw.host}
                            onChange={e => {
                              const next = [...(recipe.url_rewrites ?? [])];
                              next[i] = { ...next[i], host: e.target.value };
                              patchRecipe({ url_rewrites: next });
                            }}
                            placeholder="www.jcu.edu.au"
                            className="text-sm h-8"
                          />
                        </div>
                        <div>
                          <Label className="text-xs mb-1 block">Path Contains <span className="text-muted-foreground">(optional)</span></Label>
                          <Input
                            value={rw.path_contains ?? ""}
                            onChange={e => {
                              const next = [...(recipe.url_rewrites ?? [])];
                              next[i] = { ...next[i], path_contains: e.target.value || undefined };
                              patchRecipe({ url_rewrites: next });
                            }}
                            placeholder="/courses/"
                            className="text-sm h-8"
                          />
                        </div>
                        <div>
                          <Label className="text-xs mb-1 block">Append Query</Label>
                          <Input
                            value={rw.append_query}
                            onChange={e => {
                              const next = [...(recipe.url_rewrites ?? [])];
                              next[i] = { ...next[i], append_query: e.target.value };
                              patchRecipe({ url_rewrites: next });
                            }}
                            placeholder="international=true"
                            className="text-sm h-8 font-mono"
                          />
                        </div>
                      </div>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-8 w-8 text-destructive hover:text-destructive shrink-0"
                        onClick={() => {
                          const next = (recipe.url_rewrites ?? []).filter((_, j) => j !== i);
                          patchRecipe({ url_rewrites: next });
                        }}
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </div>
                  ))}
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  className="mt-2"
                  onClick={() => patchRecipe({ url_rewrites: [...(recipe.url_rewrites ?? []), { host: "", append_query: "" }] })}
                >
                  <Plus className="h-3 w-3 mr-1" /> Add URL Rewrite Rule
                </Button>
                {(recipe.url_rewrites ?? []).length > 0 && (
                  <p className="text-xs text-muted-foreground mt-2">
                    <strong>Examples:</strong>{" "}
                    <code className="font-mono bg-muted px-1 rounded">international=true</code> (JCU, UNE),{" "}
                    <code className="font-mono bg-muted px-1 rounded">type=International</code> (ACU),{" "}
                    <code className="font-mono bg-muted px-1 rounded">audience=INTERNATIONAL</code> (CQU),{" "}
                    <code className="font-mono bg-muted px-1 rounded">studentType=international</code> (UniSQ).
                    Keys already present in the URL are never overwritten.
                  </p>
                )}
              </div>
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

              <Separator />

              {/* ── Scrape.do Proxy ─────────────────────────────────────── */}
              <div className="space-y-3">
                <div>
                  <Label className="font-semibold text-sm">Scrape.do Proxy</Label>
                  <p className="text-xs text-muted-foreground mt-0.5">
                    Route per-course fetches through Scrape.do's residential proxy to bypass geo-detection or Cloudflare blocks.
                    Requires the <code className="text-xs">SCRAPE_DO_TOKEN</code> secret.
                  </p>
                </div>
                <div className="flex items-center gap-3">
                  <Switch
                    checked={recipe.scrape_do_static}
                    onCheckedChange={v => patchRecipe({ scrape_do_static: v })}
                  />
                  <div>
                    <Label className="text-sm">Static proxy (no JS rendering)</Label>
                    <p className="text-xs text-muted-foreground">
                      ~$0.0005/call. Use when pages return geo-targeted content for US IPs (200 OK but wrong content).
                      Lancaster pattern: "Our Use of Cookies" course names.
                    </p>
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-3 items-end">
                  <div>
                    <Label className="text-xs">Exit-node country code (ISO 3166-1 alpha-2)</Label>
                    <Input
                      value={recipe.scrape_do_geo}
                      onChange={e => patchRecipe({ scrape_do_geo: e.target.value.toUpperCase().slice(0, 2) })}
                      placeholder="e.g. NP (Nepal), GB, AU"
                      maxLength={2}
                      className="mt-1 text-xs font-mono uppercase w-28"
                    />
                  </div>
                  <p className="text-xs text-muted-foreground pb-1">
                    Pins the proxy exit-node to a specific country. <strong>NP</strong> (Nepal) = neutral non-US/non-UK, gets standard international fees.
                    Leave empty for auto.
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

                {/* ── Fee URL suffix ── */}
                <div className="space-y-1.5">
                  <Label>Course URL Suffix for Fee Fetch</Label>
                  <Input
                    value={recipe.fee_url_suffix}
                    onChange={e => patchRecipe({ fee_url_suffix: e.target.value })}
                    placeholder="e.g. ?international"
                    className="font-mono text-sm"
                  />
                  <p className="text-xs text-muted-foreground">
                    Appended verbatim to every course URL before the scraper fetches it — use when
                    international fees are only visible on a specific URL variant (e.g.{" "}
                    <code className="font-mono bg-muted px-0.5 rounded">?international</code> on
                    JCU). The suffix is added only if not already present. Leave blank if not needed.
                  </p>
                  {recipe.fee_url_suffix && (
                    <div className="rounded-md bg-green-50 border border-green-200 p-2.5 text-xs text-green-700">
                      Scraper will fetch each course page as{" "}
                      <code className="font-mono bg-green-100 px-0.5 rounded">
                        /courses/&lt;slug&gt;{recipe.fee_url_suffix}
                      </code>
                    </div>
                  )}
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
              {/* ── Course-page English priority ── */}
              <div className="rounded-lg border bg-muted/30 p-4 space-y-3">
                <div className="flex items-start gap-3">
                  <Switch
                    checked={recipe.course_english_priority}
                    onCheckedChange={v => patchRecipe({ course_english_priority: v })}
                    className="mt-0.5"
                  />
                  <div>
                    <Label className="font-semibold">Course Page English Priority</Label>
                    <p className="text-xs text-muted-foreground mt-0.5">
                      When <strong>ON</strong>: IELTS/PTE/TOEFL extracted from the individual course
                      page are never overwritten by the university-wide central page cache — the
                      central page is used only as a true last-resort fallback for blank fields.
                    </p>
                    <p className="text-xs text-muted-foreground mt-0.5">
                      When <strong>OFF</strong> (default): the central page can override low-confidence
                      per-course values (AI fallback, Gemini). Enable for universities like JCU where
                      each course has a distinct IELTS band that differs from the institution-wide value.
                    </p>
                  </div>
                </div>
                {recipe.course_english_priority && (
                  <div className="rounded-md bg-blue-50 border border-blue-200 p-2 text-xs text-blue-700">
                    Central page English (e.g. IELTS 5.5) will only fill fields that are <em>completely blank</em> after
                    course-page extraction. Per-course values are protected.
                  </div>
                )}
              </div>

              <Separator />

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

              {/* ── Degree-level English defaults ── */}
              <div className="space-y-4">
                <h3 className="text-sm font-semibold flex items-center gap-2">
                  <TrendingUp className="h-4 w-4 text-emerald-600" />
                  Degree-Level English Defaults
                </h3>
                <p className="text-xs text-muted-foreground">
                  Per-tier IELTS / PTE / TOEFL fallbacks for universities where UG and PG entry
                  requirements differ (e.g. Waikato: UG IELTS 6.0, PG IELTS 6.5). When a{" "}
                  <code className="bg-muted px-1 rounded">postgraduate</code> tier is configured,
                  PG courses automatically skip the flat central-page values so the correct
                  tier-specific defaults apply. Leave a row blank to inherit the institution-wide
                  flat defaults instead.
                </p>
                <DegreeLevelDefaultsEditor
                  defaults={recipe.degree_level_defaults}
                  onChange={v => patchRecipe({ degree_level_defaults: v })}
                />
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

              <Separator />
              <div className="space-y-2">
                <h3 className="text-sm font-semibold">Generated YAML Preview</h3>
                <p className="text-xs text-muted-foreground">
                  Live snapshot of all non-default settings across every tab. Copy into{" "}
                  <code className="bg-muted px-1 rounded">scraper_config/unis/&lt;slug&gt;.yaml</code>{" "}
                  to bake these settings into the YAML file permanently.
                </p>
                <pre className="text-xs bg-muted rounded-lg p-3 overflow-auto max-h-72 font-mono whitespace-pre">
                  {buildYamlPreview(recipe)}
                </pre>
              </div>
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

        {/* ── 10. Year & Duplicate Handling ───────────────────────────────── */}
        <TabsContent value="year">
          {(() => {
            const cy = recipe.course_year;
            const patchCY = (patch: Partial<typeof cy>) =>
              patchRecipe({ course_year: { ...cy, ...patch } });
            const isActive = cy.mode !== "keep_all" || cy.ignore_years.length > 0 || recipe.ignore_urls_matching.length > 0;
            return (
              <div className="space-y-4">
                {/* Info banner */}
                <Card className={isActive ? "border-blue-200 bg-blue-50" : "border-gray-100"}>
                  <CardHeader className="pb-2 pt-4">
                    <CardTitle className="text-base flex items-center gap-2">
                      <Calendar className="h-4 w-4 text-blue-600" />
                      Year &amp; Duplicate Handling
                      {isActive && <span className="ml-2 text-[11px] font-normal px-2 py-0.5 rounded-full bg-blue-100 text-blue-700">Active</span>}
                    </CardTitle>
                    <CardDescription>
                      When a university lists the same course for multiple academic years (e.g.
                      <code className="text-xs bg-gray-100 px-1 mx-1 rounded">/2026/bachelor-it</code> and
                      <code className="text-xs bg-gray-100 px-1 mx-1 rounded">/2027/bachelor-it</code>),
                      the scraper stages both as separate courses. Use these rules to keep only the preferred year.
                    </CardDescription>
                  </CardHeader>
                </Card>

                {/* ── Course Year Mode ── */}
                <Card>
                  <CardHeader className="pb-3 pt-4">
                    <CardTitle className="text-sm flex items-center gap-2"><Calendar className="h-4 w-4" /> Course Year Mode</CardTitle>
                    <CardDescription>
                      When the same course URL slug appears under multiple year paths, which version should be kept?
                    </CardDescription>
                  </CardHeader>
                  <CardContent className="space-y-5">
                    <div className="grid grid-cols-2 gap-4">
                      <div>
                        <Label>Year Mode</Label>
                        <Select value={cy.mode} onValueChange={v => patchCY({ mode: v })}>
                          <SelectTrigger className="mt-1"><SelectValue /></SelectTrigger>
                          <SelectContent>
                            <SelectItem value="keep_all">Keep All (no dedup)</SelectItem>
                            <SelectItem value="keep_preferred_year">Keep Preferred Year</SelectItem>
                            <SelectItem value="keep_latest">Keep Latest Year</SelectItem>
                            <SelectItem value="keep_current">Keep Current Calendar Year</SelectItem>
                          </SelectContent>
                        </Select>
                        <p className="text-[11px] text-muted-foreground mt-1">
                          {cy.mode === "keep_preferred_year" && "Keeps only the preferred_year version. Falls back to latest if preferred isn't found."}
                          {cy.mode === "keep_latest" && "Always keeps the highest year number found for each course slug."}
                          {cy.mode === "keep_current" && `Keeps the version closest to the current calendar year (${new Date().getFullYear()}).`}
                          {cy.mode === "keep_all" && "No deduplication — both year versions are staged."}
                        </p>
                      </div>
                      <div>
                        <Label>Preferred Year</Label>
                        <Input
                          type="number"
                          value={cy.preferred_year ?? ""}
                          onChange={e => patchCY({ preferred_year: e.target.value ? parseInt(e.target.value) : null })}
                          placeholder={`e.g. ${new Date().getFullYear()}`}
                          className="mt-1 text-sm"
                          disabled={cy.mode !== "keep_preferred_year"}
                        />
                        <p className="text-[11px] text-muted-foreground mt-1">
                          Year to keep when mode=keep_preferred_year.
                        </p>
                      </div>
                    </div>

                    <div>
                      <Label>Duplicate Key</Label>
                      <Select value={cy.duplicate_key} onValueChange={v => patchCY({ duplicate_key: v })}>
                        <SelectTrigger className="mt-1"><SelectValue /></SelectTrigger>
                        <SelectContent>
                          <SelectItem value="none">None (no dedup)</SelectItem>
                          <SelectItem value="slug_without_year">URL slug without year segment</SelectItem>
                          <SelectItem value="name">Course name</SelectItem>
                          <SelectItem value="cricos_code">CRICOS code</SelectItem>
                          <SelectItem value="course_code">Course code</SelectItem>
                        </SelectContent>
                      </Select>
                      <p className="text-[11px] text-muted-foreground mt-1">
                        How to identify that two URLs are the same course. <strong>slug_without_year</strong> strips the 4-digit year segment from the URL path and groups URLs by the resulting base slug — the most common case for SCU-style sites.
                      </p>
                    </div>

                    <div>
                      <Label className="flex items-center gap-1.5">
                        Ignore Years
                        <span className="text-[10px] text-muted-foreground font-normal">(one per line)</span>
                      </Label>
                      <Textarea
                        value={ignoreYearsText}
                        onChange={e => setIgnoreYearsText(e.target.value)}
                        onBlur={() => {
                          const yrs = ignoreYearsText.split("\n").map(s => parseInt(s.trim())).filter(n => !isNaN(n) && n > 2000 && n < 2100);
                          patchCY({ ignore_years: yrs });
                          setIgnoreYearsText(yrs.join("\n"));
                        }}
                        placeholder={"2027\n2028"}
                        className="mt-1 font-mono text-sm h-20"
                      />
                      <p className="text-[11px] text-muted-foreground mt-1">
                        URLs containing any of these year values in their path are dropped before extraction (e.g. <code>/2027/</code>). Applied even when mode=keep_all.
                      </p>
                    </div>

                    {cy.mode !== "keep_all" && cy.preferred_year && cy.ignore_years.length > 0 && cy.duplicate_key !== "none" && (
                      <div className="rounded-lg bg-green-50 border border-green-200 p-3 text-xs text-green-800">
                        <div className="font-semibold mb-1">✅ Active rule for this university:</div>
                        <pre className="font-mono text-[11px] whitespace-pre-wrap">{`course_year:
  mode: ${cy.mode}
  preferred_year: ${cy.preferred_year}
  ignore_years: [${cy.ignore_years.join(", ")}]
  duplicate_key: ${cy.duplicate_key}`}</pre>
                      </div>
                    )}
                  </CardContent>
                </Card>

                {/* ── URL Pattern Filtering ── */}
                <Card>
                  <CardHeader className="pb-3 pt-4">
                    <CardTitle className="text-sm flex items-center gap-2"><Filter className="h-4 w-4" /> URL Year Patterns</CardTitle>
                    <CardDescription>
                      Drop or prefer URLs matching specific substrings (applied before deduplication).
                    </CardDescription>
                  </CardHeader>
                  <CardContent className="space-y-4">
                    <StringListEditor
                      label="Ignore URLs Matching"
                      values={recipe.ignore_urls_matching}
                      onChange={v => patchRecipe({ ignore_urls_matching: v })}
                      placeholder='e.g. "/2027/"'
                      helpText="Any discovered URL containing this substring is dropped. Use for year-path filtering (e.g. /2027/ to drop all 2027 course URLs)."
                    />
                    <StringListEditor
                      label="Prefer URLs Matching"
                      values={recipe.prefer_urls_matching}
                      onChange={v => patchRecipe({ prefer_urls_matching: v })}
                      placeholder='e.g. "/2026/"'
                      helpText="When deduplicating by slug, prefer the version whose URL matches this substring. Not required when preferred_year is set."
                    />
                  </CardContent>
                </Card>

                {/* ── Fee Year Rules ── */}
                <Card>
                  <CardHeader className="pb-3 pt-4">
                    <CardTitle className="text-sm flex items-center gap-2"><DollarSign className="h-4 w-4" /> Fee Year Rules</CardTitle>
                    <CardDescription>
                      Control which year's fee data is used when multiple year pages are found.
                    </CardDescription>
                  </CardHeader>
                  <CardContent className="space-y-4">
                    <div className="grid grid-cols-2 gap-4">
                      <div>
                        <Label>Fee Preferred Year</Label>
                        <Input
                          type="number"
                          value={recipe.fee_year ?? ""}
                          onChange={e => patchRecipe({ fee_year: e.target.value ? parseInt(e.target.value) : null })}
                          placeholder={`e.g. ${new Date().getFullYear()}`}
                          className="mt-1 text-sm"
                        />
                        <p className="text-[11px] text-muted-foreground mt-1">Use fees extracted from this year's page only. Matches the existing fee_year field.</p>
                      </div>
                    </div>
                    <div>
                      <Label className="flex items-center gap-1.5">
                        Reject Fee Years
                        <span className="text-[10px] text-muted-foreground font-normal">(one per line)</span>
                      </Label>
                      <Textarea
                        value={feeRejectYearsText}
                        onChange={e => setFeeRejectYearsText(e.target.value)}
                        onBlur={() => {
                          const yrs = feeRejectYearsText.split("\n").map(s => parseInt(s.trim())).filter(n => !isNaN(n) && n > 2000 && n < 2100);
                          patchRecipe({ fee_reject_years: yrs });
                          setFeeRejectYearsText(yrs.join("\n"));
                        }}
                        placeholder={"2027\n2028"}
                        className="mt-1 font-mono text-sm h-20"
                      />
                      <p className="text-[11px] text-muted-foreground mt-1">
                        If a fee is extracted from a page URL containing one of these year values, it is discarded and not staged as international_fee.
                      </p>
                    </div>
                  </CardContent>
                </Card>

                {/* ── SCU quick-apply tip ── */}
                <Card className="border-amber-200 bg-amber-50">
                  <CardContent className="pt-4 pb-3">
                    <div className="flex items-start gap-3">
                      <GitMerge className="h-4 w-4 text-amber-600 mt-0.5 shrink-0" />
                      <div className="text-xs text-amber-800 space-y-1">
                        <p className="font-semibold">Typical fix for universities with year-segment URLs (e.g. SCU)</p>
                        <p>Set <strong>Mode = Keep Preferred Year</strong>, <strong>Preferred Year = {new Date().getFullYear()}</strong>, <strong>Ignore Years = [{new Date().getFullYear() + 1}]</strong>, <strong>Duplicate Key = URL slug without year</strong>.</p>
                        <p>Then add <code className="bg-amber-100 px-1 rounded">/{new Date().getFullYear() + 1}/</code> to <strong>Ignore URLs Matching</strong> as a belt-and-braces guard.</p>
                      </div>
                    </div>
                  </CardContent>
                </Card>
              </div>
            );
          })()}
        </TabsContent>

        {/* ── 11. Quality ─────────────────────────────────────────────────── */}
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

// ── YAML preview generator ──────────────────────────────────────────────────

function _yq(s: string) { return `"${String(s).replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`; }

function buildYamlPreview(recipe: Recipe): string {
  const lines: string[] = [];

  // ── Discovery ─────────────────────────────────────────────────────────────
  const discLines: string[] = [];

  // SearchStax Solr provider
  if (recipe.searchstax) {
    const ss = recipe.searchstax;
    discLines.push("  searchstax:");
    if (ss.endpoint) discLines.push(`    endpoint: ${_yq(ss.endpoint)}`);
    if (ss.token_env) discLines.push(`    token_env: ${_yq(ss.token_env)}`);
    if (ss.filter_query) discLines.push(`    filter_query: ${_yq(ss.filter_query)}`);
    if (ss.currency && ss.currency !== "GBP") discLines.push(`    currency: ${_yq(ss.currency)}`);
    else if (ss.currency === "GBP") discLines.push(`    currency: "GBP"`);
    if (ss.url_base) discLines.push(`    url_base: ${_yq(ss.url_base)}`);
    if (ss.location_strip_prefixes.length > 0) {
      discLines.push("    location_strip_prefixes:");
      ss.location_strip_prefixes.forEach(p => discLines.push(`      - ${_yq(p)}`));
    }
    if (ss.exclude_part_time) discLines.push("    exclude_part_time: true");
    if (!ss.links_only) discLines.push("    links_only: false");
    if (ss.field_map_as_payload) discLines.push("    field_map_as_payload: true");
    const fm = ss.field_map;
    const fmEntries = Object.entries(fm).filter(([, v]) => v);
    if (fmEntries.length > 0) {
      discLines.push("    field_map:");
      fmEntries.forEach(([k, v]) => discLines.push(`      ${k}: ${_yq(v as string)}`));
    }
    if (ss.page_size != null) discLines.push(`    page_size: ${ss.page_size}`);
    if (ss.max_pages != null) discLines.push(`    max_pages: ${ss.max_pages}`);
  }

  // SSR Prop Discovery
  if (recipe.ssr_prop_discovery) {
    const sp = recipe.ssr_prop_discovery;
    discLines.push("  ssr_prop_discovery:");
    if (sp.listing_pages.length > 0) {
      discLines.push("    listing_pages:");
      sp.listing_pages.forEach(p => {
        if (p.url) discLines.push(`      - url: ${_yq(p.url)}`);
        if (p.url_prefix) discLines.push(`        url_prefix: ${_yq(p.url_prefix)}`);
        if (p.label) discLines.push(`        label: ${_yq(p.label)}`);
      });
    }
    if (sp.prop_attr && sp.prop_attr !== ":courses-data") discLines.push(`    prop_attr: ${_yq(sp.prop_attr)}`);
    else if (sp.prop_attr === ":courses-data") discLines.push(`    prop_attr: ":courses-data"`);
    if (sp.slug_field && sp.slug_field !== "slug") discLines.push(`    slug_field: ${_yq(sp.slug_field)}`);
    if (sp.name_field && sp.name_field !== "title") discLines.push(`    name_field: ${_yq(sp.name_field)}`);
    if (sp.url_suffix && sp.url_suffix !== "/{year}/") discLines.push(`    url_suffix: ${_yq(sp.url_suffix)}`);
    if (sp.year_field) discLines.push(`    year_field: ${_yq(sp.year_field)}`);
    if (sp.year_filter_prefix && sp.year_filter_prefix !== "{short_year}/") discLines.push(`    year_filter_prefix: ${_yq(sp.year_filter_prefix)}`);
    else if (sp.year_filter_prefix === "{short_year}/") discLines.push(`    year_filter_prefix: "{short_year}/"`);
    if (sp.browser_fallback_xpath) discLines.push(`    browser_fallback_xpath: ${_yq(sp.browser_fallback_xpath)}`);
    if (sp.browser_wait_selector) discLines.push(`    browser_wait_selector: ${_yq(sp.browser_wait_selector)}`);
    if (sp.course_url_pattern) discLines.push(`    course_url_pattern: ${_yq(sp.course_url_pattern)}`);
  }

  if (recipe.seed_urls.length > 0) {
    discLines.push("  seed_urls:");
    recipe.seed_urls.forEach(u => discLines.push(`    - ${_yq(u)}`));
  }
  if (recipe.render_listing_pages.length > 0) {
    discLines.push("  render_listing_pages:");
    recipe.render_listing_pages.forEach(u => discLines.push(`    - ${_yq(u)}`));
  }
  if (recipe.course_detail_url_patterns.length > 0) {
    discLines.push("  course_detail_url_patterns:");
    recipe.course_detail_url_patterns.forEach(p => discLines.push(`    - ${_yq(p)}`));
  }
  if (recipe.block_url_patterns.length > 0) {
    discLines.push("  block_url_patterns:");
    recipe.block_url_patterns.forEach(p => discLines.push(`    - ${_yq(p)}`));
  }
  if (recipe.allow_url_patterns.length > 0) {
    discLines.push("  allow_url_patterns:");
    recipe.allow_url_patterns.forEach(p => discLines.push(`    - ${_yq(p)}`));
  }
  if (recipe.must_contain.length > 0) {
    discLines.push("  must_contain:");
    recipe.must_contain.forEach(p => discLines.push(`    - ${_yq(p)}`));
  }
  if (discLines.length > 0) {
    lines.push("discovery:");
    lines.push(...discLines);
    lines.push("");
  }

  // ── Extraction ────────────────────────────────────────────────────────────
  const extLines: string[] = [];

  // Course name cleanup
  if (
    recipe.course_name_remove_after.length > 0 ||
    recipe.course_name_remove_year_suffix ||
    recipe.course_name_remove_patterns.length > 0
  ) {
    extLines.push("  course_name:");
    if (recipe.course_name_remove_after.length > 0) {
      extLines.push("    remove_after:");
      recipe.course_name_remove_after.forEach(s => extLines.push(`      - ${_yq(s)}`));
    }
    if (recipe.course_name_remove_year_suffix) extLines.push("    remove_year_suffix: true");
    if (recipe.course_name_remove_patterns.length > 0) {
      extLines.push("    remove_patterns:");
      recipe.course_name_remove_patterns.forEach(p => extLines.push(`      - ${_yq(p)}`));
    }
  }

  // Fees
  const hasFees =
    recipe.fee_rules_undergraduate.length > 0 ||
    recipe.fee_rules_postgraduate.length > 0 ||
    recipe.fee_currency !== "AUD" ||
    recipe.fee_year != null ||
    recipe.fee_prefer_international ||
    recipe.fee_url_suffix.trim() !== "" ||
    recipe.fee_reject_keywords.length > 0 ||
    recipe.fee_follow_links.length > 0;
  if (hasFees) {
    extLines.push("  fees:");
    if (recipe.fee_currency !== "AUD") extLines.push(`    default_currency: ${_yq(recipe.fee_currency)}`);
    if (recipe.fee_year != null) extLines.push(`    fee_year: ${recipe.fee_year}`);
    if (recipe.fee_prefer_international) extLines.push("    prefer_international: true");
    if (recipe.fee_url_suffix.trim()) extLines.push(`    fee_url_suffix: ${_yq(recipe.fee_url_suffix.trim())}`);
    if (recipe.fee_reject_keywords.length > 0) {
      extLines.push("    reject_keywords:");
      recipe.fee_reject_keywords.forEach(k => extLines.push(`      - ${_yq(k)}`));
    }
    if (recipe.fee_follow_links.length > 0) {
      extLines.push("    follow_links:");
      recipe.fee_follow_links.forEach(u => extLines.push(`      - ${_yq(u)}`));
    }
    if (recipe.fee_rules_undergraduate.length > 0) {
      extLines.push("    rules_undergraduate:");
      recipe.fee_rules_undergraduate.forEach(r => {
        extLines.push(`      - amount: ${r.amount}`);
        if (r.keywords.length > 0)
          extLines.push(`        keywords: [${r.keywords.map(k => _yq(k)).join(", ")}]`);
      });
    }
    if (recipe.fee_rules_postgraduate.length > 0) {
      extLines.push("    rules_postgraduate:");
      recipe.fee_rules_postgraduate.forEach(r => {
        extLines.push(`      - amount: ${r.amount}`);
        if (r.keywords.length > 0)
          extLines.push(`        keywords: [${r.keywords.map(k => _yq(k)).join(", ")}]`);
      });
    }
  }

  // English / IELTS
  const hasEnglish =
    !!recipe.ielts?.overall_regex ||
    !!recipe.ielts?.band_regex ||
    !!recipe.ielts?.source_xpath ||
    Object.keys(recipe.band_mapping).length > 0 ||
    Object.keys(recipe.degree_level_defaults).length > 0 ||
    recipe.follow_links.length > 0 ||
    recipe.course_english_priority;
  if (hasEnglish) {
    extLines.push("  english:");
    if (recipe.course_english_priority) extLines.push("    course_english_priority: true");
    if (recipe.ielts?.overall_regex) extLines.push(`    overall_regex: ${_yq(recipe.ielts.overall_regex)}`);
    if (recipe.ielts?.band_regex) extLines.push(`    band_regex: ${_yq(recipe.ielts.band_regex)}`);
    if (recipe.ielts?.source_xpath) extLines.push(`    source_xpath: ${_yq(recipe.ielts.source_xpath)}`);
    if (recipe.follow_links.length > 0) {
      extLines.push("    follow_links:");
      recipe.follow_links.forEach(u => extLines.push(`      - ${_yq(u)}`));
    }
    if (Object.keys(recipe.degree_level_defaults).length > 0) {
      extLines.push("    degree_level_defaults:");
      Object.entries(recipe.degree_level_defaults).forEach(([tier, spec]) => {
        extLines.push(`      ${tier}:`);
        if (spec.ielts != null) extLines.push(`        ielts: ${spec.ielts}`);
        if (spec.pte != null) extLines.push(`        pte: ${spec.pte}`);
        if (spec.toefl != null) extLines.push(`        toefl: ${spec.toefl}`);
        if (spec.duolingo != null) extLines.push(`        duolingo: ${spec.duolingo}`);
      });
    }
    if (Object.keys(recipe.band_mapping).length > 0) {
      extLines.push("    band_mapping:");
      Object.entries(recipe.band_mapping).forEach(([overall, spec]) => {
        extLines.push(`      ${_yq(overall)}:`);
        if (spec.ielts_overall) extLines.push(`        ielts_overall: ${spec.ielts_overall}`);
        if (spec.ielts_each) extLines.push(`        ielts_each: ${spec.ielts_each}`);
        if (spec.pte_overall) extLines.push(`        pte_overall: ${spec.pte_overall}`);
        if (spec.toefl_overall) extLines.push(`        toefl_overall: ${spec.toefl_overall}`);
      });
    }
  }

  // Location rules
  const hasLocation =
    Object.keys(recipe.location_replace).length > 0 ||
    recipe.location_allowed_values.length > 0 ||
    recipe.location_reject_values.length > 0;
  if (hasLocation) {
    extLines.push("  location:");
    if (Object.keys(recipe.location_replace).length > 0) {
      extLines.push("    replace:");
      Object.entries(recipe.location_replace).forEach(([k, v]) => {
        extLines.push(`      ${_yq(k)}: ${_yq(v)}`);
      });
    }
    if (recipe.location_allowed_values.length > 0) {
      extLines.push("    allowed_values:");
      recipe.location_allowed_values.forEach(v => extLines.push(`      - ${_yq(v)}`));
    }
    if (recipe.location_reject_values.length > 0) {
      extLines.push("    reject_values:");
      recipe.location_reject_values.forEach(v => extLines.push(`      - ${_yq(v)}`));
    }
  }

  // Study mode
  if (recipe.study_mode_from_location || recipe.study_mode_online_keywords.length > 0) {
    extLines.push("  study_mode:");
    if (recipe.study_mode_from_location) extLines.push("    from_location: true");
    if (recipe.study_mode_online_keywords.length > 0) {
      extLines.push("    online_keywords:");
      recipe.study_mode_online_keywords.forEach(k => extLines.push(`      - ${_yq(k)}`));
    }
  }

  // Browser actions
  if (recipe.actions.length > 0) {
    extLines.push("  actions:");
    recipe.actions.forEach(a => {
      if (a.action_type === "wait_for_text") {
        extLines.push(`    - wait_for:`);
        extLines.push(`        text: ${_yq(a.value)}`);
      } else if (a.action_type === "wait_for_selector") {
        extLines.push(`    - wait_for:`);
        extLines.push(`        selector: ${_yq(a.value)}`);
      } else {
        const key = a.action_type === "click_text" ? "click_text"
          : a.action_type === "click_css" ? "click_css"
          : a.action_type === "expand_text" ? "expand_text"
          : "scroll_to";
        extLines.push(`    - ${key}: ${_yq(a.value)}`);
      }
    });
  }

  // Quality gates
  if (
    recipe.minimum_completeness !== 85 ||
    recipe.required_fields.length > 0 ||
    recipe.block_publish_if.length > 0
  ) {
    extLines.push("  quality:");
    if (recipe.minimum_completeness !== 85)
      extLines.push(`    minimum_completeness: ${recipe.minimum_completeness}`);
    if (recipe.required_fields.length > 0) {
      extLines.push("    required_fields:");
      recipe.required_fields.forEach(f => extLines.push(`      - ${f}`));
    }
    if (recipe.block_publish_if.length > 0) {
      extLines.push("    block_publish_if:");
      recipe.block_publish_if.forEach(c => extLines.push(`      - ${c}`));
    }
  }

  if (recipe.scrape_do_static) extLines.push("  scrape_do_static: true");
  if (recipe.scrape_do_geo.trim()) extLines.push(`  scrape_do_geo: ${recipe.scrape_do_geo.trim().toUpperCase()}`);

  if (extLines.length > 0) {
    lines.push("extraction:");
    lines.push(...extLines);
  }

  return lines.length > 0 ? lines.join("\n") : "# No non-default settings configured yet.";
}

// ── Constant used in patchApi helper ──
const EMPTY_API: ApiConfig = {
  endpoint: "",
  method: "GET",
  query_params: {},
  root_path: "",
  count_path: "",
  course_url_template: "",
  fields: {},
  headers: {},
  url_fields: [],
  title_fields: [],
  body: null,
};
