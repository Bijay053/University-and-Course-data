import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useToast } from "@/hooks/use-toast";
import { SettingsTabs } from "@/components/settings-tabs";
import { Plus, Save, Trash2, Sparkles, Search, RefreshCw, X, Play, Loader2, CheckCircle2, AlertCircle, Clock, GitCompare, Code, History, RotateCcw, Download, Clipboard, Check, Wand2, Undo2, Bot, ShieldAlert, TriangleAlert, Info, ChevronDown, ChevronUp } from "lucide-react";
import { cn } from "@/lib/utils";
import { fetchWithAuth } from "@/lib/api";
import { CountrySelect } from "@/components/country-select";

const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");

const SAMPLE_YAML = `# University Full Name
# Hostname: www.example.edu.au
# Country: Australia  |  Currency: AUD
#
# Bug history / rationale:
#   (add notes here as you discover site-specific quirks)

# ── WHEN TO EDIT THIS FILE ────────────────────────────────────────────────────
# Edit when a scrape of THIS university produces wrong or missing data.
# Safe pattern:
#   1. Run scrape → inspect staged rows in the portal
#   2. Find your symptom in the Quick Reference table at the bottom
#   3. Uncomment the relevant field → re-run this university only
#
# Do NOT touch scraper_config/defaults.yaml — that affects every university.

# ── DISCOVERY ─────────────────────────────────────────────────────────────────

discovery:

  # ── API discovery (use ONE of the three options below) ────────────────────

  # Option A — Autonomous XHR capture (AI finds the API endpoint for you).
  # Set true when BFS finds < 10 courses and you do not know the API URL.
  # The engine opens the course listing page in Playwright, intercepts all JSON
  # calls, picks the best candidate (SearchStax / Algolia / Solr / REST), and
  # immediately fetches courses from it.  The endpoint is then saved to auto_config
  # so future scrapes skip the XHR capture entirely.
  # [AUTO_API] log lines will show what the AI captured and how many URLs it added.
  auto_api_discovery: false

  # Option B — YAML-driven API (use after auto_api_discovery found the endpoint,
  # OR when you already know the URL from DevTools).
  # Runs before BFS/browser; if it returns ≥1 link those tiers are skipped.
  # generic_search_api:
  #   enabled: true
  #   method: GET
  #   url: "https://searchcloud-1.searchstax.com/29847/<core-id>/emselect"
  #   headers:
  #     authorization: "Token <your-public-search-key>"  # never commit session tokens
  #   params:
  #     q: "*"
  #     rows: "250"
  #     model: "coursefinder-ug"
  #   root_path: "response.docs"        # dot-path to the course array in the JSON
  #   url_fields: [url, course_url, link, path]
  #   title_fields: [title, name, course_name]
  #   normalize_relative_urls: true
  #   base_url: "https://www.example.edu.au"
  #   page_size: 250
  #   page_size_param: "rows"
  #   offset_param: "start"
  #   max_pages: 20

  # Option C — SearchStax Solr provider (Huddersfield-style, full provider).
  # Use when the site is a JS SPA that queries Solr client-side.
  # searchstax:
  #   enabled: true
  #   endpoint: "https://searchcloud-1-eu-west-2.searchstax.com/29847/<core-id>/emselect"
  #   token_env: "HUD_SEARCHSTAX_TOKEN"    # env var name — never commit literal tokens
  #   filter_query: "sectionType_s:course"
  #   currency: "GBP"

  # ── BFS / Sitemap / Browser ────────────────────────────────────────────────

  # Always merge sitemap results with BFS (for JS SPAs or deep-faculty sites):
  # always_sitemap_supplement: true

  # Probe extra subdomains when BFS finds fewer than 5 candidates:
  # fallback_subdomains:
  #   - handbook.{domain}
  #   - study.{domain}

  # Drop URLs matching these regex patterns:
  # block_url_patterns:
  #   - /news/
  #   - /events/

  # Keep ONLY URLs matching at least one pattern (cuts Gemini cost significantly):
  # allow_url_patterns:
  #   - /courses/
  #   - /programs/
  #
  # IMPORTANT: allow_url_patterns is an INCLUSION filter — if no discovered URL
  # matches at least one pattern, the scraper stages 0 courses:
  #   Discovered 175 raw candidate course link(s)
  #   URL filter dropped 175 / 175 URLs (100%)
  #   Found: 0
  # Always test your regex against a sample of real discovered URLs before enabling.
  # Prefer must_contain (below) when a simple substring is enough.

  # Simpler and usually safer than allow_url_patterns.
  # Use when a unique URL path segment reliably identifies course pages.
  # Unlike allow_url_patterns, no regex knowledge needed — just a substring.
  # must_contain:
  #   - /courses/

  # Override auto-detected sitemap:
  # sitemap_url: https://www.example.edu.au/custom-sitemap.xml

  # Raise BFS page budget for sites with many listing pages (default 25 full):
  # bfs_page_budget: 80

  # Enable Playwright browser discovery in addition to BFS (for Cloudflare sites):
  # always_browser_discover: true

  # Use stealth Playwright stack (for hosts where regular headless fails Cloudflare):
  # use_stealth_browser: true

  # Fallback to Wayback Machine when all live-site discovery fails:
  # use_wayback: true

  # Surgical fallback — inject specific course URLs directly, bypassing all discovery:
  # extra_course_urls:
  #   - https://www.example.edu.au/courses/some-hidden-course


# ── EXTRACTION ────────────────────────────────────────────────────────────────

extraction:

  # ── Per-course browser controls (now YAML-configurable, no code changes needed) ──

  # Skip ALL per-course Playwright fetches for this university.
  # Use when static HTML already contains all required fields AND browser always times out:
  # skip_per_course_browser: true

  # Override the Playwright wait strategy for per-course fetches.
  # 'networkidle'      — wait for XHR/fetch to settle (use when fees load via AJAX).
  # 'domcontentloaded' — use when analytics widgets prevent networkidle from ever firing.
  # browser_wait_strategy: networkidle

  # Extra settle delay (ms) after domcontentloaded fires (only with 'domcontentloaded'):
  # browser_dcl_settle_ms: 4000

  # ── Fees ──────────────────────────────────────────────────────────────────
  fees:
    default_currency: "AUD"   # NZD for New Zealand, GBP for UK

    # University-wide fee schedule page (used when fees are not per-course):
    # central_page: https://www.example.edu.au/fees

    # University-wide fee schedule PDF:
    # fees_pdf_url: https://www.example.edu.au/fees-schedule.pdf

    # Mark all courses as having a central fee page (staging gate won't reject them):
    # force_central_fee_stage: true
    #
    # NOTE: This only lets courses pass staging when no per-course fee is found.
    # It does NOT copy the central fee to every course record.
    # If the central page publishes only broad tuition buckets (e.g. "UG: $18k/yr"),
    # leave international_fee blank rather than forcing a possibly wrong amount.
    # Use reject_keywords (below) to discard domestic rates from the central page.

    # Per-unit fee multiplier (null = auto-extract credit points from course page):
    # credit_points_per_unit: 6

    # Prefer Year-1 fee over total-course fee when both are present:
    # prefer_year_one_over_total: true

    # Column-aware PDF parser (for PDFs with multi-line course names in fee tables):
    # pdf_parser: "columnar"

    # Per-course fee keyword rejection — discard an extracted fee when its evidence
    # snippet contains any listed keyword (e.g. to avoid staging domestic rates):
    # reject_keywords:
    #   - "Kentucky residents"    # precise domestic marker — safe
    #   - "In-state"              # precise domestic marker — safe
    #   - "Commonwealth Supported"
    #   - "CSP"
    #   - "HECS"
    #
    # IMPORTANT: Avoid broad words like "Full-time" or "credit hours" because
    # they can also appear beside valid international fees (e.g.
    # "Full-time international student: $28,000/year") and will silently discard
    # the fee you actually want. Use the most specific domestic phrase possible.

  # ── English requirements ───────────────────────────────────────────────────
  english:
    # University-wide English requirements page:
    # central_page: https://www.example.edu.au/english-requirements

    # Stop Gemini vision from hallucinating IELTS scores from decorative images:
    # trust_vision_ocr: false

    # Institutional defaults applied when no per-course value is found:
    # default_ielts: 6.5
    # default_pte: 58
    # default_toefl: 80

    # Drop test names the university doesn't actually accept (suppress false positives):
    # test_blocklist:
    #   - pte
    #   - kite

  # ── Intake ────────────────────────────────────────────────────────────────
  intake:
    # For research degrees with rolling enrolment (PhD/MPhil):
    # rolling_enrollment_label: "Rolling"
    # rolling_enrollment_markers:
    #   - "enrolment shall be continuous"
    #   - "rolling admission"
    #   - "applications accepted year-round"
    #
    # IMPORTANT: Only use phrases that specifically mean continuous/rolling intake.
    # Do NOT use generic page text like "Apply Now", "Admission Requirements",
    # or "accepted to university" — those appear on normal fixed-intake pages too
    # and will stamp "Rolling" on every course that has no detected intake dates.

  # ── Filters ───────────────────────────────────────────────────────────────
  filters:
    domestic_only:
      enabled: false    # true = drop courses without international student data
    online_only:
      enabled: true     # false for distance-education-heavy universities (e.g. CSU)

  # ── URL rewrites — switch site to international view before fetching ─────────
  # url_rewrites:
  #   - host: www.example.edu.au
  #     append_query: "international=true"

  # ── Text cleaning ──────────────────────────────────────────────────────────
  text_cleaning:
    location:
      # strip_patterns:
      #   - '\\bDelivery\\s*method\\b'
    duration:
      # reject_sentence_patterns:
      #   - 'up to \\d+ years to complete'
    # global_substring_blocklist:
    #   - "Apply Now"
    #   - "Find out more"

  # ── Course name ────────────────────────────────────────────────────────────
  course_name:
    # strip_title_suffixes:
    #   - " : the University of Western Australia"

  # ── Concurrency ────────────────────────────────────────────────────────────
  # Lower for Cloudflare-heavy sites that rate-limit aggressively:
  # max_parallel_fetch: 2

  # Fallback location written when the Location panel is occasionally missing:
  # default_course_location: "Sydney"

# ── QUICK REFERENCE — symptom → YAML field ────────────────────────────────────
# Symptom                                        Fix
# ─────────────────────────────────────────────────────────────────────────────
# BFS finds 0–9 courses (JS SPA, hidden API)     auto_api_discovery: true
# You already know the API endpoint URL           generic_search_api (Option B above)
# Site uses SearchStax Solr directly             searchstax (Option C above)
# Discovery finds nav/news pages, not courses    must_contain / block_url_patterns
# Sitemap not auto-discovered                    sitemap_url
# BFS finds < 5 courses (different subdomain)    fallback_subdomains
# Cloudflare blocks plain-HTTP BFS               always_browser_discover: true
# Cloudflare blocks headless Playwright too      use_stealth_browser: true
# Per-course browser always times out (0 bytes)  skip_per_course_browser: true
# Fees load after page load via AJAX             browser_wait_strategy: networkidle
# Analytics widget prevents networkidle          browser_wait_strategy: domcontentloaded
# All courses staged as no_international_fee     fees.force_central_fee_stage: true
# Fee PDF has multi-line course names            fees.pdf_parser: "columnar"
# Page shows Year-1 fee; we want annual total    fees.prefer_year_one_over_total: true
# IELTS hallucinated from decorative images      english.trust_vision_ocr: false
# PTE/TOEFL on pages that do not list it         english.test_blocklist
# Duration shows max-candidature time            text_cleaning.duration.reject_sentence_patterns
# Location panel blank on Cloudflare-heavy site  default_course_location
# Location string has CMS junk suffix            text_cleaning.location.strip_patterns
# Course title ends with " : University of X"   course_name.strip_title_suffixes
# PhD shows no intake months                     intake.rolling_enrollment_label
# Domestic-only courses are being staged         filters.domestic_only.enabled: true
# Site uses per-unit fees                        fees.credit_points_per_unit
# International view needs a query parameter     url_rewrites
# ─────────────────────────────────────────────────────────────────────────────
# NOT YAML-fixable (escalate to engineering):
#   Cloudflare WAF blocks even stealth browser   → new extraction route needed
#   Fees only behind a JS calculator (no HTML)   → new XHR extractor needed
#   English requirements behind a login wall     → manual data entry
#   CRICOS 0% even though page shows CRICOS text → regex fix in cricos_code.py
`;

function downloadSampleYaml() {
  const blob = new Blob([SAMPLE_YAML], { type: "text/yaml" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "sample-scraper-config.yaml";
  a.click();
  URL.revokeObjectURL(url);
}

interface ConfigEntry {
  slug: string;
  title: string;
  yaml: string;
  university_id: number | null;
  university_name: string | null;
}

interface GenerateForm {
  university_name: string;
  website_url: string;
  country: string;
  notes: string;
}

type JobStatus = "queued" | "running" | "done" | "awaiting_approval" | "failed" | "cancelled";

interface TriggerState {
  jobId: string;
  status: JobStatus;
  universityId?: number;
  universityName?: string;
  error?: string;
  imported?: number;
  totalFound?: number;
}

interface HistoryEntry {
  id: number;
  slug: string;
  yaml_content: string;
  saved_by: string | null;
  saved_at: string | null;
}

type EditorView = "editor" | "diff" | "history";

interface DiagnosisIssue {
  severity: "critical" | "warning" | "info";
  title: string;
  detail: string;
}

interface DiagnosisResult {
  university_found: boolean;
  university_name: string;
  university_id: number | null;
  last_job: {
    job_id: string;
    status: string;
    total_found: number;
    imported: number;
    errors: number;
    created_at: string | null;
    raw_discovered: number;
    after_filter: number;
    filter_drop_count: number;
  } | null;
  issues: DiagnosisIssue[];
  changes: string[];
  summary: string;
  yaml: string;
  has_changes: boolean;
}

const TERMINAL_STATUSES: JobStatus[] = ["done", "awaiting_approval", "failed", "cancelled"];

function JobStatusBadge({ state, compact = false }: { state: TriggerState; compact?: boolean }) {
  const { status, imported, totalFound, error } = state;
  if (status === "queued") {
    return (
      <span className={cn("inline-flex items-center gap-1 text-amber-600", compact ? "text-[10px]" : "text-xs")}>
        <Clock className={compact ? "w-3 h-3" : "w-3.5 h-3.5"} />
        {!compact && "Queued"}
      </span>
    );
  }
  if (status === "running") {
    return (
      <span className={cn("inline-flex items-center gap-1 text-blue-600", compact ? "text-[10px]" : "text-xs")}>
        <Loader2 className={cn("animate-spin", compact ? "w-3 h-3" : "w-3.5 h-3.5")} />
        {!compact && (totalFound ? `Running… ${imported ?? 0}/${totalFound}` : "Running…")}
      </span>
    );
  }
  if (status === "done" || status === "awaiting_approval") {
    return (
      <span className={cn("inline-flex items-center gap-1 text-green-600", compact ? "text-[10px]" : "text-xs")}>
        <CheckCircle2 className={compact ? "w-3 h-3" : "w-3.5 h-3.5"} />
        {!compact && (status === "awaiting_approval" ? "Awaiting approval" : `Done${imported != null ? ` — ${imported} staged` : ""}`)}
      </span>
    );
  }
  if (status === "failed") {
    return (
      <span className={cn("inline-flex items-center gap-1 text-red-600", compact ? "text-[10px]" : "text-xs")} title={error}>
        <AlertCircle className={compact ? "w-3 h-3" : "w-3.5 h-3.5"} />
        {!compact && "Failed"}
      </span>
    );
  }
  return null;
}

// ── Diff engine ───────────────────────────────────────────────────────────────

type DiffOp = "equal" | "insert" | "delete";

interface DiffLine {
  op: DiffOp;
  text: string;
  oldLineNo: number | null;
  newLineNo: number | null;
}

function lcs(a: string[], b: string[]): number[][] {
  const m = a.length;
  const n = b.length;
  const dp: number[][] = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(0));
  for (let i = 1; i <= m; i++) {
    for (let j = 1; j <= n; j++) {
      dp[i][j] = a[i - 1] === b[j - 1] ? dp[i - 1][j - 1] + 1 : Math.max(dp[i - 1][j], dp[i][j - 1]);
    }
  }
  return dp;
}

function computeDiff(oldText: string, newText: string): DiffLine[] {
  const oldLines = oldText === "" ? [] : oldText.split("\n");
  const newLines = newText === "" ? [] : newText.split("\n");

  const dp = lcs(oldLines, newLines);

  const result: DiffLine[] = [];
  let i = oldLines.length;
  let j = newLines.length;
  const ops: Array<[DiffOp, string]> = [];

  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && oldLines[i - 1] === newLines[j - 1]) {
      ops.push(["equal", oldLines[i - 1]]);
      i--;
      j--;
    } else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
      ops.push(["insert", newLines[j - 1]]);
      j--;
    } else {
      ops.push(["delete", oldLines[i - 1]]);
      i--;
    }
  }

  ops.reverse();

  let oldNo = 1;
  let newNo = 1;
  for (const [op, text] of ops) {
    if (op === "equal") {
      result.push({ op, text, oldLineNo: oldNo++, newLineNo: newNo++ });
    } else if (op === "delete") {
      result.push({ op, text, oldLineNo: oldNo++, newLineNo: null });
    } else {
      result.push({ op, text, oldLineNo: null, newLineNo: newNo++ });
    }
  }

  return result;
}

const CONTEXT_LINES = 3;

function collapseDiff(lines: DiffLine[]): Array<DiffLine | { type: "hunk"; count: number }> {
  const changed = new Set<number>();
  lines.forEach((l, idx) => {
    if (l.op !== "equal") {
      for (let k = Math.max(0, idx - CONTEXT_LINES); k <= Math.min(lines.length - 1, idx + CONTEXT_LINES); k++) {
        changed.add(k);
      }
    }
  });

  const result: Array<DiffLine | { type: "hunk"; count: number }> = [];
  let skipCount = 0;

  for (let idx = 0; idx < lines.length; idx++) {
    if (changed.has(idx)) {
      if (skipCount > 0) {
        result.push({ type: "hunk", count: skipCount });
        skipCount = 0;
      }
      result.push(lines[idx]);
    } else {
      skipCount++;
    }
  }

  if (skipCount > 0) result.push({ type: "hunk", count: skipCount });

  return result;
}

// ── Diff viewer component ─────────────────────────────────────────────────────

function DiffViewer({ oldYaml, newYaml, oldLabel = "saved", newLabel = "current edit" }: { oldYaml: string; newYaml: string; oldLabel?: string; newLabel?: string }) {
  const diffLines = computeDiff(oldYaml, newYaml);
  const hasChanges = diffLines.some(l => l.op !== "equal");

  if (!hasChanges) {
    return (
      <div className="flex-1 flex items-center justify-center text-muted-foreground text-sm">
        No changes — {oldLabel} matches {newLabel}.
      </div>
    );
  }

  const collapsed = collapseDiff(diffLines);

  const added = diffLines.filter(l => l.op === "insert").length;
  const removed = diffLines.filter(l => l.op === "delete").length;

  return (
    <div className="flex-1 overflow-auto font-mono text-xs">
      <div className="sticky top-0 bg-muted/80 border-b px-4 py-1.5 text-xs flex gap-3 z-10 backdrop-blur-sm">
        <span className="text-muted-foreground mr-1">{oldLabel} → {newLabel}</span>
        <span className="text-green-600 dark:text-green-400">+{added} added</span>
        <span className="text-red-600 dark:text-red-400">−{removed} removed</span>
      </div>

      <table className="w-full border-collapse">
        <colgroup>
          <col className="w-10" />
          <col className="w-10" />
          <col />
        </colgroup>
        <tbody>
          {collapsed.map((item, idx) => {
            if ("type" in item) {
              return (
                <tr key={idx} className="bg-blue-50 dark:bg-blue-950/30">
                  <td colSpan={3} className="px-4 py-0.5 text-blue-500 dark:text-blue-400 select-none">
                    @@ {item.count} unchanged line{item.count !== 1 ? "s" : ""} hidden
                  </td>
                </tr>
              );
            }

            const { op, text, oldLineNo, newLineNo } = item;
            const rowCls =
              op === "insert"
                ? "bg-green-50 dark:bg-green-950/30"
                : op === "delete"
                  ? "bg-red-50 dark:bg-red-950/30"
                  : "";
            const gutterCls =
              op === "insert"
                ? "text-green-600 dark:text-green-400 bg-green-100 dark:bg-green-900/40"
                : op === "delete"
                  ? "text-red-600 dark:text-red-400 bg-red-100 dark:bg-red-900/40"
                  : "text-muted-foreground";
            const prefix = op === "insert" ? "+" : op === "delete" ? "−" : " ";

            return (
              <tr key={idx} className={rowCls}>
                <td className={cn("px-2 text-right select-none border-r", gutterCls)}>
                  {oldLineNo ?? ""}
                </td>
                <td className={cn("px-2 text-right select-none border-r", gutterCls)}>
                  {newLineNo ?? ""}
                </td>
                <td className="px-3 py-px whitespace-pre-wrap break-all">
                  <span
                    className={
                      op === "insert"
                        ? "text-green-700 dark:text-green-300"
                        : op === "delete"
                          ? "text-red-700 dark:text-red-300"
                          : ""
                    }
                  >
                    {prefix} {text}
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ── localStorage draft helpers ────────────────────────────────────────────────

const DRAFT_TTL_MS = 7 * 24 * 60 * 60 * 1000; // 7 days
const DRAFT_PREFIX = "scraper-draft:";

interface DraftEntry { yaml: string; savedAt: string; }

function isDraftEntry(v: unknown): v is DraftEntry {
  return (
    typeof v === "object" &&
    v !== null &&
    typeof (v as Record<string, unknown>).yaml === "string" &&
    typeof (v as Record<string, unknown>).savedAt === "string"
  );
}

function writeDraft(slug: string, yaml: string): void {
  try {
    const entry: DraftEntry = { yaml, savedAt: new Date().toISOString() };
    localStorage.setItem(`${DRAFT_PREFIX}${slug}`, JSON.stringify(entry));
  } catch { /* storage quota */ }
}

function readDraft(slug: string): string | null {
  try {
    const raw = localStorage.getItem(`${DRAFT_PREFIX}${slug}`);
    if (raw === null) return null;
    try {
      const parsed: unknown = JSON.parse(raw);
      if (isDraftEntry(parsed)) {
        const age = Date.now() - new Date(parsed.savedAt).getTime();
        if (isNaN(age) || age > DRAFT_TTL_MS) {
          localStorage.removeItem(`${DRAFT_PREFIX}${slug}`);
          return null; // expired or invalid date
        }
        return parsed.yaml;
      }
      // Valid JSON but not a DraftEntry (e.g. a legacy bare string like "foo",
      // a number, or a plain object without the right shape) — treat as current
      return raw;
    } catch {
      return raw; // not valid JSON — treat as current bare string
    }
  } catch { return null; }
}

function removeDraft(slug: string): void {
  try { localStorage.removeItem(`${DRAFT_PREFIX}${slug}`); } catch { /* ignore */ }
}

function pruneOldDrafts(): void {
  try {
    const toDelete: string[] = [];
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (!key?.startsWith(DRAFT_PREFIX)) continue;
      const raw = localStorage.getItem(key);
      if (!raw) continue;
      try {
        const parsed: unknown = JSON.parse(raw);
        if (isDraftEntry(parsed)) {
          const age = Date.now() - new Date(parsed.savedAt).getTime();
          if (isNaN(age) || age > DRAFT_TTL_MS) toDelete.push(key);
        }
        // Non-DraftEntry JSON or bare string — leave it for readDraft to handle
      } catch { /* not valid JSON — keep */ }
    }
    toDelete.forEach(k => localStorage.removeItem(k));
  } catch { /* ignore */ }
}

// ── YAML key diff helpers ─────────────────────────────────────────────────────

function extractYamlKeys(yaml: string): Record<string, string> {
  const result: Record<string, string> = {};
  const lines = yaml.split("\n");
  const stack: { indent: number; path: string }[] = [];

  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#") || trimmed.startsWith("-")) continue;

    const indent = line.length - line.trimStart().length;
    const match = trimmed.match(/^([a-zA-Z_][a-zA-Z0-9_-]*):\s*(.*?)(?:\s*#.*)?$/);
    if (!match) continue;

    const key = match[1];
    let value = match[2].trim();

    while (stack.length > 0 && stack[stack.length - 1].indent >= indent) {
      stack.pop();
    }

    const path = stack.length > 0 ? `${stack[stack.length - 1].path}.${key}` : key;

    if (path.split(".").length <= 3) {
      if (value.startsWith('"') && value.endsWith('"')) value = value.slice(1, -1);
      if (value.startsWith("'") && value.endsWith("'")) value = value.slice(1, -1);
      if (value && value !== "|" && value !== ">" && !value.startsWith("&")) {
        result[path] = value;
      }
      stack.push({ indent, path });
    }
  }

  return result;
}

interface KeyChange {
  path: string;
  changeType: "added" | "removed" | "modified";
  oldValue?: string;
  newValue?: string;
}

function computeKeyChanges(oldYaml: string, newYaml: string): KeyChange[] {
  const oldKeys = extractYamlKeys(oldYaml);
  const newKeys = extractYamlKeys(newYaml);
  const changes: KeyChange[] = [];
  const allPaths = new Set([...Object.keys(oldKeys), ...Object.keys(newKeys)]);

  for (const path of allPaths) {
    const inOld = path in oldKeys;
    const inNew = path in newKeys;
    if (!inOld) {
      changes.push({ path, changeType: "added", newValue: newKeys[path] });
    } else if (!inNew) {
      changes.push({ path, changeType: "removed", oldValue: oldKeys[path] });
    } else if (oldKeys[path] !== newKeys[path]) {
      changes.push({ path, changeType: "modified", oldValue: oldKeys[path], newValue: newKeys[path] });
    }
  }

  const order = { modified: 0, added: 1, removed: 2 };
  changes.sort((a, b) => {
    const diff = order[a.changeType] - order[b.changeType];
    return diff !== 0 ? diff : a.path.localeCompare(b.path);
  });

  return changes;
}

function ChangedKeysPanel({ oldYaml, newYaml }: { oldYaml: string; newYaml: string }) {
  const changes = useMemo(() => computeKeyChanges(oldYaml, newYaml), [oldYaml, newYaml]);

  if (changes.length === 0) return null;

  return (
    <div className="border-b bg-muted/20 flex-shrink-0">
      <div className="px-4 py-2 flex items-center gap-2 border-b bg-muted/30">
        <GitCompare className="w-3.5 h-3.5 text-muted-foreground flex-shrink-0" />
        <span className="text-xs font-medium text-muted-foreground">
          Changed keys
        </span>
        <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-300 font-medium ml-auto">
          {changes.length} {changes.length === 1 ? "key" : "keys"}
        </span>
      </div>
      <div className="max-h-40 overflow-y-auto px-4 py-2 flex flex-col gap-1">
        {changes.map((c) => (
          <div key={c.path} className="flex items-baseline gap-2 text-xs font-mono leading-relaxed">
            <span
              className={cn(
                "flex-shrink-0 w-4 text-center font-bold",
                c.changeType === "added" ? "text-green-600 dark:text-green-400" :
                c.changeType === "removed" ? "text-red-600 dark:text-red-400" :
                "text-amber-600 dark:text-amber-400"
              )}
            >
              {c.changeType === "added" ? "+" : c.changeType === "removed" ? "−" : "~"}
            </span>
            <span className="text-foreground/80 font-medium">{c.path}</span>
            {c.changeType === "modified" && c.oldValue !== undefined && c.newValue !== undefined && (
              <span className="flex items-center gap-1 text-muted-foreground">
                <span className="text-red-600 dark:text-red-400 line-through">{c.oldValue}</span>
                <span>→</span>
                <span className="text-green-600 dark:text-green-400">{c.newValue}</span>
              </span>
            )}
            {c.changeType === "added" && c.newValue !== undefined && (
              <span className="text-green-600 dark:text-green-400">{c.newValue}</span>
            )}
            {c.changeType === "removed" && c.oldValue !== undefined && (
              <span className="text-red-600 dark:text-red-400 line-through">{c.oldValue}</span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

// ── History panel ─────────────────────────────────────────────────────────────

function formatRelativeTime(iso: string | null): string {
  if (!iso) return "unknown";
  const date = new Date(iso);
  const now = Date.now();
  const diffMs = now - date.getTime();
  const diffSec = Math.floor(diffMs / 1000);
  if (diffSec < 60) return "just now";
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDay = Math.floor(diffHr / 24);
  if (diffDay < 30) return `${diffDay}d ago`;
  return date.toLocaleDateString();
}

function formatAbsoluteTime(iso: string | null): string {
  if (!iso) return "";
  const date = new Date(iso);
  return date.toLocaleString();
}

function formatSavedBy(savedBy: string | null): string {
  if (!savedBy) return "unknown";
  if (savedBy.startsWith("restore:")) return `↩ restored by ${savedBy.slice(8)}`;
  return savedBy;
}

interface HistoryPanelProps {
  history: HistoryEntry[];
  loading: boolean;
  hasMore: boolean;
  loadingMore: boolean;
  savedYaml: string;
  selectedEntry: HistoryEntry | null;
  compareEntry: HistoryEntry | null;
  onSelectEntry: (entry: HistoryEntry | null) => void;
  onSetCompareEntry: (entry: HistoryEntry | null) => void;
  onRestore: (entry: HistoryEntry) => void;
  onLoadMore: () => void;
  restoringId: number | null;
}

function HistoryPanel({ history, loading, hasMore, loadingMore, savedYaml, selectedEntry, compareEntry, onSelectEntry, onSetCompareEntry, onRestore, onLoadMore, restoringId }: HistoryPanelProps) {
  const selected = selectedEntry;
  const [search, setSearch] = useState("");
  const [keyFilter, setKeyFilter] = useState("");

  const filtered = useMemo(() => {
    let entries = history;
    const q = search.trim().toLowerCase();
    if (q) {
      entries = entries.filter(e => {
        const byUser = (e.saved_by ?? "").toLowerCase().includes(q);
        const byDate =
          formatAbsoluteTime(e.saved_at).toLowerCase().includes(q) ||
          formatRelativeTime(e.saved_at).toLowerCase().includes(q);
        return byUser || byDate;
      });
    }
    const k = keyFilter.trim();
    if (k) {
      entries = entries.filter(e => e.yaml_content.includes(k));
    }
    return entries;
  }, [history, search, keyFilter]);

  const isFiltered = search.trim() !== "" || keyFilter.trim() !== "";

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center text-muted-foreground text-sm">
        <Loader2 className="w-4 h-4 animate-spin mr-2" />
        Loading history…
      </div>
    );
  }

  if (history.length === 0) {
    return (
      <div className="flex-1 flex items-center justify-center text-muted-foreground text-sm">
        No save history yet — history is recorded each time you save.
      </div>
    );
  }

  return (
    <div className="flex-1 flex overflow-hidden">
      {/* Left: history list */}
      <div className="w-64 flex-shrink-0 border-r flex flex-col">
        {/* Search / filter bar */}
        <div className="p-2 border-b space-y-1.5 flex-shrink-0">
          <div className="relative">
            <Search className="absolute left-2 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground pointer-events-none" />
            <Input
              className="h-7 pl-7 pr-7 text-xs"
              placeholder="Filter by user or date…"
              value={search}
              onChange={e => setSearch(e.target.value)}
            />
            {search && (
              <button
                className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                onClick={() => setSearch("")}
                aria-label="Clear search"
              >
                <X className="w-3 h-3" />
              </button>
            )}
          </div>
          <div className="relative">
            <Code className="absolute left-2 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground pointer-events-none" />
            <Input
              className="h-7 pl-7 pr-7 text-xs"
              placeholder="YAML key (e.g. default_ielts)…"
              value={keyFilter}
              onChange={e => setKeyFilter(e.target.value)}
            />
            {keyFilter && (
              <button
                className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                onClick={() => setKeyFilter("")}
                aria-label="Clear key filter"
              >
                <X className="w-3 h-3" />
              </button>
            )}
          </div>
          {isFiltered && (
            <p className="text-[10px] text-muted-foreground text-center">
              {filtered.length} of {history.length} {history.length === 1 ? "entry" : "entries"}
            </p>
          )}
        </div>

        {/* Shift-click hint */}
        <div className="px-3 py-1.5 border-b bg-muted/20 flex-shrink-0">
          <p className="text-[10px] text-muted-foreground leading-tight">
            Click to compare vs. current.{" "}
            <span className="font-medium">Shift-click</span> a second entry to compare two versions.
          </p>
        </div>

        {/* Entry list */}
        <div className="flex-1 overflow-y-auto">
          {filtered.length === 0 ? (
            <div className="px-3 py-6 text-center text-xs text-muted-foreground">
              No entries match your filter.
            </div>
          ) : (
            filtered.map((entry) => {
              const isSelected = selected?.id === entry.id;
              const isCompare = compareEntry?.id === entry.id;
              const isCurrent = entry.id === history[0]?.id;
              const isHighlighted = isSelected || isCompare;
              return (
                <button
                  key={entry.id}
                  onClick={(e) => {
                    if (e.shiftKey) {
                      if (isCompare) {
                        onSetCompareEntry(null);
                      } else if (isSelected) {
                        onSelectEntry(null);
                        onSetCompareEntry(null);
                      } else if (selected) {
                        onSetCompareEntry(entry);
                      } else {
                        onSelectEntry(entry);
                      }
                    } else {
                      if (isSelected && !compareEntry) {
                        onSelectEntry(null);
                      } else {
                        onSelectEntry(entry);
                        onSetCompareEntry(null);
                      }
                    }
                  }}
                  className={cn(
                    "w-full text-left px-3 py-2.5 border-b last:border-b-0 transition-colors",
                    isSelected ? "bg-blue-50 dark:bg-blue-950/30" :
                    isCompare ? "bg-purple-50 dark:bg-purple-950/30" :
                    "hover:bg-muted/50",
                  )}
                >
                  <div className="flex items-center justify-between gap-1">
                    <span className="text-xs font-medium truncate" title={formatAbsoluteTime(entry.saved_at)}>
                      {formatRelativeTime(entry.saved_at)}
                    </span>
                    <div className="flex items-center gap-1 flex-shrink-0">
                      {isSelected && (
                        <span className="text-[10px] px-1 py-0.5 rounded bg-blue-100 dark:bg-blue-900 text-blue-700 dark:text-blue-300 font-medium">
                          A
                        </span>
                      )}
                      {isCompare && (
                        <span className="text-[10px] px-1 py-0.5 rounded bg-purple-100 dark:bg-purple-900 text-purple-700 dark:text-purple-300 font-medium">
                          B
                        </span>
                      )}
                      {isCurrent && !isHighlighted && (
                        <span className="text-[10px] px-1 py-0.5 rounded bg-green-100 text-green-700 dark:bg-green-900 dark:text-green-300">
                          latest
                        </span>
                      )}
                    </div>
                  </div>
                  <div className="text-[11px] text-muted-foreground truncate mt-0.5">
                    {formatSavedBy(entry.saved_by)}
                  </div>
                  <div className="text-[10px] text-muted-foreground/70 mt-0.5">
                    {entry.yaml_content.split("\n").length} lines
                  </div>
                </button>
              );
            })
          )}
        </div>

        {/* Load more */}
        {hasMore && (
          <div className="p-2 border-t flex-shrink-0">
            <Button
              size="sm"
              variant="outline"
              className="w-full h-7 text-xs"
              onClick={onLoadMore}
              disabled={loadingMore}
            >
              {loadingMore
                ? <><Loader2 className="h-3 w-3 mr-1 animate-spin" />Loading…</>
                : "Load more"}
            </Button>
          </div>
        )}
      </div>

      {/* Right: diff or prompt */}
      <div className="flex-1 flex flex-col overflow-hidden">
        {selected && compareEntry ? (() => {
          const aDate = selected.saved_at ? new Date(selected.saved_at).getTime() : 0;
          const bDate = compareEntry.saved_at ? new Date(compareEntry.saved_at).getTime() : 0;
          const oldEntry = aDate <= bDate ? selected : compareEntry;
          const newEntry = aDate <= bDate ? compareEntry : selected;
          return (
            <>
              <div className="px-3 py-2 border-b flex items-center justify-between bg-muted/30 gap-2 flex-wrap">
                <div className="text-xs text-muted-foreground flex items-center gap-1.5">
                  <span className="inline-flex items-center gap-1">
                    <span className="px-1 py-0.5 rounded bg-blue-100 dark:bg-blue-900 text-blue-700 dark:text-blue-300 font-medium text-[10px]">A</span>
                    <span className="font-medium" title={formatAbsoluteTime(oldEntry.saved_at)}>{formatRelativeTime(oldEntry.saved_at)}</span>
                  </span>
                  <span className="text-muted-foreground/50">→</span>
                  <span className="inline-flex items-center gap-1">
                    <span className="px-1 py-0.5 rounded bg-purple-100 dark:bg-purple-900 text-purple-700 dark:text-purple-300 font-medium text-[10px]">B</span>
                    <span className="font-medium" title={formatAbsoluteTime(newEntry.saved_at)}>{formatRelativeTime(newEntry.saved_at)}</span>
                  </span>
                </div>
                <div className="flex items-center gap-2 flex-shrink-0">
                  <Button
                    size="sm"
                    variant="ghost"
                    className="h-6 text-[11px] gap-1 text-muted-foreground"
                    onClick={() => onSetCompareEntry(null)}
                  >
                    <X className="w-3 h-3" />
                    Clear B
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    className="h-6 text-[11px] gap-1"
                    onClick={() => onRestore(oldEntry)}
                    disabled={restoringId !== null}
                  >
                    {restoringId === oldEntry.id
                      ? <Loader2 className="w-3 h-3 animate-spin" />
                      : <RotateCcw className="w-3 h-3" />}
                    {restoringId === oldEntry.id ? "Restoring…" : "Restore A"}
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    className="h-6 text-[11px] gap-1"
                    onClick={() => onRestore(newEntry)}
                    disabled={restoringId !== null}
                  >
                    {restoringId === newEntry.id
                      ? <Loader2 className="w-3 h-3 animate-spin" />
                      : <RotateCcw className="w-3 h-3" />}
                    {restoringId === newEntry.id ? "Restoring…" : "Restore B"}
                  </Button>
                </div>
              </div>
              <ChangedKeysPanel oldYaml={oldEntry.yaml_content} newYaml={newEntry.yaml_content} />
              <DiffViewer
                oldYaml={oldEntry.yaml_content}
                newYaml={newEntry.yaml_content}
                oldLabel={`${formatRelativeTime(oldEntry.saved_at)} (older)`}
                newLabel={`${formatRelativeTime(newEntry.saved_at)} (newer)`}
              />
            </>
          );
        })() : selected ? (
          <>
            <div className="px-3 py-2 border-b flex items-center justify-between bg-muted/30">
              <div className="text-xs text-muted-foreground">
                <span className="inline-flex items-center gap-1">
                  <span className="px-1 py-0.5 rounded bg-blue-100 dark:bg-blue-900 text-blue-700 dark:text-blue-300 font-medium text-[10px]">A</span>
                  <span className="font-medium" title={formatAbsoluteTime(selected.saved_at)}>
                    {formatRelativeTime(selected.saved_at)}
                  </span>
                </span>
                {" · "}
                {formatSavedBy(selected.saved_by)}
              </div>
              <Button
                size="sm"
                variant="outline"
                className="h-6 text-[11px] gap-1"
                onClick={() => onRestore(selected)}
                disabled={restoringId === selected.id}
              >
                {restoringId === selected.id
                  ? <Loader2 className="w-3 h-3 animate-spin" />
                  : <RotateCcw className="w-3 h-3" />}
                {restoringId === selected.id ? "Restoring…" : "Restore this version"}
              </Button>
            </div>
            <ChangedKeysPanel oldYaml={selected.yaml_content} newYaml={savedYaml} />
            <DiffViewer
              oldYaml={selected.yaml_content}
              newYaml={savedYaml}
              oldLabel={formatRelativeTime(selected.saved_at)}
              newLabel="current saved"
            />
          </>
        ) : (
          <div className="flex-1 flex items-center justify-center text-muted-foreground text-sm px-6 text-center">
            Select a history entry to compare it against the current saved version
          </div>
        )}
      </div>
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────


export default function SettingsScraperConfigs() {
  const { toast } = useToast();
  const [configs, setConfigs] = useState<ConfigEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);
  const [editorYaml, setEditorYaml] = useState("");
  const [savedYaml, setSavedYaml] = useState("");
  const [editorSlug, setEditorSlug] = useState("");
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [filter, setFilter] = useState("");
  const [showNewModal, setShowNewModal] = useState(false);
  const [newModalMode, setNewModalMode] = useState<"ai" | "manual">("ai");
  const [manualSlug, setManualSlug] = useState("");
  const [manualYaml, setManualYaml] = useState(SAMPLE_YAML);
  const [copiedSample, setCopiedSample] = useState(false);
  const [pendingSlug, setPendingSlug] = useState<string | null>(null);
  const [draftBanner, setDraftBanner] = useState<{ slug: string; lineCount: number } | null>(null);
  const [generating, setGenerating] = useState(false);
  const [genStage, setGenStage] = useState("");
  const [view, setView] = useState<EditorView>("editor");
  const [genForm, setGenForm] = useState<GenerateForm>({
    university_name: "",
    website_url: "",
    country: "Australia",
    notes: "",
  });
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const draftTimerRef = useRef<number | null>(null);

  // ── History state ─────────────────────────────────────────────────────────
  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyHasMore, setHistoryHasMore] = useState(false);
  const [historyLoadingMore, setHistoryLoadingMore] = useState(false);
  const [historySelectedEntry, setHistorySelectedEntry] = useState<HistoryEntry | null>(null);
  const [historyCompareEntry, setHistoryCompareEntry] = useState<HistoryEntry | null>(null);
  const [restoringId, setRestoringId] = useState<number | null>(null);

  // ── Per-slug scrape job tracking ─────────────────────────────────────────
  const [triggerJobs, setTriggerJobs] = useState<Record<string, TriggerState>>({});
  const [triggering, setTriggering] = useState<string | null>(null);
  const pollTimerRef = useRef<number | null>(null);

  // ── AI YAML fix ──────────────────────────────────────────────────────────
  const [aiFixOpen, setAiFixOpen] = useState(false);
  const [aiFixPrompt, setAiFixPrompt] = useState("");
  const [aiFixing, setAiFixing] = useState(false);
  const [aiFixPrev, setAiFixPrev] = useState<string | null>(null);

  // ── AI Diagnose & Fix ─────────────────────────────────────────────────────
  const [diagnosing, setDiagnosing] = useState(false);
  const [diagnosisOpen, setDiagnosisOpen] = useState(false);
  const [diagnosisResult, setDiagnosisResult] = useState<DiagnosisResult | null>(null);
  const [diagnosisPrompt, setDiagnosisPrompt] = useState("");
  const [diagnosisExpanded, setDiagnosisExpanded] = useState<Record<number, boolean>>({});

  const fetchConfigs = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetchWithAuth(`${BASE}/api/settings/scraper-configs`);
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      setConfigs(data.configs ?? []);
    } catch (err) {
      toast({ title: "Failed to load configs", description: (err as Error).message, variant: "destructive" });
    } finally {
      setLoading(false);
    }
  }, [toast]);

  const fetchHistory = useCallback(async (slug: string, opts?: { beforeId?: number; append?: boolean }) => {
    const isAppend = opts?.append ?? false;
    if (isAppend) {
      setHistoryLoadingMore(true);
    } else {
      setHistoryLoading(true);
      setHistory([]);
      setHistoryHasMore(false);
    }
    try {
      const params = new URLSearchParams();
      if (opts?.beforeId != null) params.set("before_id", String(opts.beforeId));
      const url = `${BASE}/api/settings/scraper-configs/${slug}/history${params.size ? `?${params}` : ""}`;
      const res = await fetchWithAuth(url);
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      const newEntries: HistoryEntry[] = data.history ?? [];
      setHistory(prev => isAppend ? [...prev, ...newEntries] : newEntries);
      setHistoryHasMore(data.has_more ?? false);
    } catch (err) {
      toast({ title: "Failed to load history", description: (err as Error).message, variant: "destructive" });
    } finally {
      setHistoryLoading(false);
      setHistoryLoadingMore(false);
    }
  }, [toast]);

  useEffect(() => { void fetchConfigs(); }, [fetchConfigs]);

  // Prune stale drafts once on mount
  useEffect(() => { pruneOldDrafts(); }, []);

  // Debounced draft persistence to localStorage
  useEffect(() => {
    if (!editorSlug || editorYaml === savedYaml) return;
    if (draftTimerRef.current) clearTimeout(draftTimerRef.current);
    draftTimerRef.current = window.setTimeout(() => {
      writeDraft(editorSlug, editorYaml);
      draftTimerRef.current = null;
    }, 600);
    return () => {
      if (draftTimerRef.current) { clearTimeout(draftTimerRef.current); draftTimerRef.current = null; }
    };
  }, [editorYaml, editorSlug, savedYaml]);

  // Fetch history when user switches to history view
  useEffect(() => {
    if (view === "history" && selected) {
      void fetchHistory(selected);
    }
  }, [view, selected, fetchHistory]);

  // Clear history entry selection whenever the active config slug changes
  useEffect(() => {
    setHistorySelectedEntry(null);
    setHistoryCompareEntry(null);
  }, [selected]);

  // Poll all in-progress jobs
  useEffect(() => {
    const activeJobs = Object.entries(triggerJobs).filter(
      ([, state]) => !TERMINAL_STATUSES.includes(state.status),
    );
    if (activeJobs.length === 0) {
      if (pollTimerRef.current) { clearInterval(pollTimerRef.current); pollTimerRef.current = null; }
      return;
    }
    if (pollTimerRef.current) return; // already polling
    pollTimerRef.current = window.setInterval(async () => {
      const current = Object.entries(triggerJobs).filter(
        ([, s]) => !TERMINAL_STATUSES.includes(s.status),
      );
      if (current.length === 0) {
        clearInterval(pollTimerRef.current!);
        pollTimerRef.current = null;
        return;
      }
      await Promise.all(current.map(async ([slug, state]) => {
        try {
          const res = await fetchWithAuth(`${BASE}/api/scrape/status/${state.jobId}`);
          if (!res.ok) return;
          const d = await res.json();
          setTriggerJobs(prev => ({
            ...prev,
            [slug]: {
              ...prev[slug],
              status: d.status as JobStatus,
              imported: d.imported ?? d.progress?.imported,
              totalFound: d.totalFound ?? d.progress?.total,
            },
          }));
        } catch { /* network hiccup — keep polling */ }
      }));
    }, 2500);
    return () => {
      if (pollTimerRef.current) { clearInterval(pollTimerRef.current); pollTimerRef.current = null; }
    };
  }, [triggerJobs]);

  const triggerScrape = async (slug: string) => {
    if (triggering === slug) return;
    setTriggering(slug);
    try {
      const res = await fetchWithAuth(`${BASE}/api/settings/scraper-configs/${slug}/trigger`, {
        method: "POST",
      });
      const data = await res.json();
      if (!res.ok) {
        toast({ title: "Trigger failed", description: data.detail ?? "Could not start scrape", variant: "destructive" });
        return;
      }
      setTriggerJobs(prev => ({
        ...prev,
        [slug]: {
          jobId: data.jobId ?? data.runtimeJobId,
          status: data.status ?? "queued",
          universityId: data.universityId,
          universityName: data.universityName,
        },
      }));
      toast({ title: "Scrape started", description: `Job queued for ${data.universityName ?? slug}` });
    } catch (err) {
      toast({ title: "Trigger failed", description: (err as Error).message, variant: "destructive" });
    } finally {
      setTriggering(null);
    }
  };

  const selectConfig = (slug: string) => {
    const cfg = configs.find(c => c.slug === slug);
    if (!cfg) return;
    const draft = readDraft(slug);
    setSelected(slug);
    setEditorSlug(slug);
    setSavedYaml(cfg.yaml);
    setView("editor");
    setHistory([]);
    if (draft !== null && draft !== cfg.yaml) {
      setEditorYaml(draft);
      setDraftBanner({ slug, lineCount: draft.split("\n").length });
    } else {
      setEditorYaml(cfg.yaml);
      setDraftBanner(null);
    }
  };

  const handleSelectConfig = (slug: string) => {
    if (slug === selected) return;
    if (editorYaml !== savedYaml) {
      setPendingSlug(slug);
    } else {
      selectConfig(slug);
    }
  };

  const confirmDiscard = () => {
    if (pendingSlug) {
      selectConfig(pendingSlug);
      setPendingSlug(null);
    }
  };

  const discardDraft = () => {
    removeDraft(draftBanner?.slug ?? editorSlug);
    setEditorYaml(savedYaml);
    setDraftBanner(null);
  };

  const handleSave = async () => {
    if (!editorSlug.trim()) { toast({ title: "Slug required", variant: "destructive" }); return; }
    setSaving(true);
    try {
      const res = await fetchWithAuth(`${BASE}/api/settings/scraper-configs/${editorSlug.trim()}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ yaml_content: editorYaml }),
      });
      if (!res.ok) { const d = await res.json(); throw new Error(d.detail ?? "Save failed"); }
      const data = await res.json();
      toast({ title: "Saved", description: `Config for '${editorSlug}' saved` });
      setSavedYaml(editorYaml);
      setView("editor");
      setDraftBanner(null);
      removeDraft(editorSlug.trim());

      if (data.git_pushed && data.git_message && !data.git_message.includes("up-to-date")) {
        toast({ title: "Saved & synced to GitHub", description: data.git_message });
      }
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
      const res = await fetchWithAuth(`${BASE}/api/settings/scraper-configs/${selected}`, {
        method: "DELETE",
      });
      if (!res.ok) { const d = await res.json(); throw new Error(d.detail ?? "Delete failed"); }
      toast({ title: "Deleted", description: `Config for '${selected}' removed` });
      removeDraft(selected);
      setSelected(null);
      setEditorYaml("");
      setSavedYaml("");
      setEditorSlug("");
      setView("editor");
      setHistory([]);
      setDraftBanner(null);
      await fetchConfigs();
    } catch (err) {
      toast({ title: "Delete failed", description: (err as Error).message, variant: "destructive" });
    } finally {
      setDeleting(false);
    }
  };

  const handleAiFix = async () => {
    if (!aiFixPrompt.trim()) {
      toast({ title: "Prompt required", description: "Describe what you want to change.", variant: "destructive" });
      return;
    }
    if (!editorSlug.trim()) {
      toast({ title: "No config selected", description: "Select or create a config first.", variant: "destructive" });
      return;
    }
    setAiFixing(true);
    try {
      const res = await fetchWithAuth(`${BASE}/api/settings/scraper-configs/${editorSlug.trim()}/ai-fix`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt: aiFixPrompt.trim(), yaml_content: editorYaml }),
      });
      if (!res.ok) { const d = await res.json(); throw new Error(d.detail ?? "AI fix failed"); }
      const data = await res.json();
      setAiFixPrev(editorYaml);
      setEditorYaml(data.yaml ?? editorYaml);
      setAiFixOpen(false);
      setAiFixPrompt("");
      setView("diff");
      toast({ title: "AI fix applied", description: "Review the changes in the diff view, then Save when ready." });
    } catch (err) {
      const msg = (err as Error).message;
      const isBusy = msg.toLowerCase().includes("busy") || msg.toLowerCase().includes("try again");
      toast({ title: isBusy ? "Gemini is busy" : "AI fix failed", description: msg, variant: "destructive" });
    } finally {
      setAiFixing(false);
    }
  };

  const handleDiagnose = async () => {
    if (!editorSlug.trim()) {
      toast({ title: "No config selected", description: "Select a config first.", variant: "destructive" });
      return;
    }
    setDiagnosing(true);
    setDiagnosisOpen(true);
    setDiagnosisResult(null);
    setDiagnosisExpanded({});
    setAiFixOpen(false);
    try {
      const res = await fetchWithAuth(`${BASE}/api/settings/scraper-configs/${editorSlug.trim()}/ai-diagnose`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ yaml_content: editorYaml, prompt: diagnosisPrompt }),
      });
      if (!res.ok) { const d = await res.json(); throw new Error(d.detail ?? "Diagnosis failed"); }
      const data: DiagnosisResult = await res.json();
      setDiagnosisResult(data);
      const critCount = data.issues.filter(i => i.severity === "critical").length;
      if (data.has_changes) {
        toast({ title: `${critCount > 0 ? critCount + " critical issue(s) found" : "Diagnosis complete"}`, description: "Review the findings below, then apply the fix." });
      } else {
        toast({ title: "All clear", description: "No config changes needed — the config looks correct." });
      }
    } catch (err) {
      const msg = (err as Error).message;
      const isBusy = msg.toLowerCase().includes("busy") || msg.toLowerCase().includes("try again");
      toast({ title: isBusy ? "Gemini is busy" : "Diagnosis failed", description: msg, variant: "destructive" });
      setDiagnosisOpen(false);
    } finally {
      setDiagnosing(false);
    }
  };

  const applyDiagnosis = () => {
    if (!diagnosisResult?.yaml) return;
    setAiFixPrev(editorYaml);
    setEditorYaml(diagnosisResult.yaml);
    setDiagnosisOpen(false);
    setView("diff");
    toast({ title: "Fix applied", description: "Review in the Changes view, then Save to persist." });
  };

  const handleCreateManually = () => {
    const slug = manualSlug.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
    if (!slug) {
      toast({ title: "Slug required", description: "Enter a slug (e.g. 'macquarie' or 'utas').", variant: "destructive" });
      return;
    }
    if (configs.some(c => c.slug === slug)) {
      toast({ title: "Slug already exists", description: `A config for '${slug}' already exists. Select it from the list to edit.`, variant: "destructive" });
      return;
    }
    setEditorYaml(manualYaml);
    setSavedYaml("");
    setEditorSlug(slug);
    setSelected(null);
    setView("editor");
    setHistory([]);
    setDraftBanner(null);
    setShowNewModal(false);
    toast({ title: "Config created", description: "Edit the YAML below, then save when ready." });
    setTimeout(() => textareaRef.current?.focus(), 100);
  };

  const handleGenerate = async () => {
    if (!genForm.university_name.trim() || !genForm.website_url.trim()) {
      toast({ title: "Name and URL required", variant: "destructive" });
      return;
    }
    setGenerating(true);

    // Cycle through descriptive stage messages so the user knows what's happening
    const stages = [
      "Fetching homepage…",
      "Detecting SPA framework…",
      "Probing fee pages…",
      "Probing English requirement pages…",
      "Extracting nav links…",
      "Generating YAML with AI…",
    ];
    let stageIdx = 0;
    setGenStage(stages[0]);
    const stageTimer = window.setInterval(() => {
      stageIdx = Math.min(stageIdx + 1, stages.length - 1);
      setGenStage(stages[stageIdx]);
    }, 6000);

    try {
      const res = await fetchWithAuth(`${BASE}/api/settings/scraper-configs/generate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(genForm),
      });
      if (!res.ok) { const d = await res.json(); throw new Error(d.detail ?? "Generation failed"); }
      const data = await res.json();
      setEditorYaml(data.yaml ?? "");
      setSavedYaml("");
      setEditorSlug(data.slug ?? "");
      setSelected(null);
      setView("editor");
      setHistory([]);
      setDraftBanner(null);
      setShowNewModal(false);
      toast({ title: "Generated!", description: "Review and edit the config below, then save." });
      setTimeout(() => textareaRef.current?.focus(), 100);
    } catch (err) {
      toast({ title: "AI generation failed", description: (err as Error).message, variant: "destructive" });
    } finally {
      clearInterval(stageTimer);
      setGenerating(false);
      setGenStage("");
    }
  };

  const handleRestore = async (entry: HistoryEntry) => {
    if (!selected) return;
    if (!confirm(`Restore this version saved ${formatRelativeTime(entry.saved_at)}? The current saved YAML will be replaced.`)) return;
    setRestoringId(entry.id);
    try {
      const res = await fetchWithAuth(`${BASE}/api/settings/scraper-configs/${selected}/restore/${entry.id}`, {
        method: "POST",
      });
      if (!res.ok) { const d = await res.json(); throw new Error(d.detail ?? "Restore failed"); }
      toast({ title: "Restored", description: `Config for '${selected}' reverted to version from ${formatRelativeTime(entry.saved_at)}` });
      setSavedYaml(entry.yaml_content);
      setEditorYaml(entry.yaml_content);
      removeDraft(selected);
      setDraftBanner(null);
      setHistorySelectedEntry(null);
      setHistoryCompareEntry(null);
      await fetchConfigs();
      await fetchHistory(selected);
    } catch (err) {
      toast({ title: "Restore failed", description: (err as Error).message, variant: "destructive" });
    } finally {
      setRestoringId(null);
    }
  };

  const filtered = configs.filter(c =>
    c.slug.includes(filter.toLowerCase()) || c.title.toLowerCase().includes(filter.toLowerCase())
  );

  const selectedConfig = selected ? configs.find(c => c.slug === selected) : null;
  const selectedJob = selected ? triggerJobs[selected] : undefined;
  const selectedJobActive = selectedJob && !TERMINAL_STATUSES.includes(selectedJob.status);
  const isDirty = editorYaml !== savedYaml;

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
              filtered.map(cfg => {
                const job = triggerJobs[cfg.slug];
                const isRunning = job && !TERMINAL_STATUSES.includes(job.status);
                const isThisTriggering = triggering === cfg.slug;
                const hasUni = cfg.university_id != null;
                const configIsDirty = selected === cfg.slug && isDirty;

                return (
                  <div
                    key={cfg.slug}
                    className={cn(
                      "group flex items-center border-b last:border-b-0 transition-colors",
                      selected === cfg.slug ? "bg-primary/10" : "hover:bg-muted/50",
                    )}
                  >
                    <button
                      onClick={() => handleSelectConfig(cfg.slug)}
                      className="flex-1 text-left px-3 py-2 text-sm min-w-0"
                    >
                      <div className={cn("font-medium truncate flex items-center gap-1.5", selected === cfg.slug && "text-primary")}>
                        {cfg.slug}
                        {configIsDirty && (
                          <span className="inline-block w-1.5 h-1.5 rounded-full bg-amber-500 flex-shrink-0" title="Unsaved changes" />
                        )}
                      </div>
                      <div className="text-xs text-muted-foreground truncate">{cfg.title}</div>
                      {job && (
                        <div className="mt-0.5">
                          <JobStatusBadge state={job} compact />
                        </div>
                      )}
                    </button>
                    {/* Per-card trigger button */}
                    <button
                      title={hasUni ? `Trigger scrape for ${cfg.university_name ?? cfg.slug}` : "No university linked — add # Hostname: to the YAML"}
                      disabled={isThisTriggering || isRunning || !hasUni}
                      onClick={e => { e.stopPropagation(); void triggerScrape(cfg.slug); }}
                      className={cn(
                        "mr-2 flex-shrink-0 p-1 rounded transition-colors",
                        hasUni
                          ? "text-muted-foreground hover:text-green-600 hover:bg-green-50 group-hover:opacity-100 opacity-0"
                          : "text-muted-foreground/30 cursor-not-allowed opacity-0 group-hover:opacity-100",
                        (isThisTriggering || isRunning) && "opacity-100",
                      )}
                    >
                      {isThisTriggering || isRunning
                        ? <Loader2 className="w-3.5 h-3.5 animate-spin text-blue-500" />
                        : <Play className="w-3.5 h-3.5" />}
                    </button>
                  </div>
                );
              })
            )}
          </div>

          <div className="p-2 border-t flex flex-col gap-1.5">
            <Button
              size="sm"
              className="w-full h-8 text-xs"
              onClick={() => {
                setGenForm({ university_name: "", website_url: "", country: "Australia", notes: "" });
                setNewModalMode("ai");
                setManualSlug("");
                setManualYaml(SAMPLE_YAML);
                setShowNewModal(true);
              }}
            >
              <Plus className="h-3.5 w-3.5 mr-1" />
              New Config
            </Button>
            <div className="flex gap-1.5">
              <Button
                size="sm"
                variant="outline"
                className="flex-1 h-8 text-xs"
                onClick={downloadSampleYaml}
                title="Download sample YAML file"
              >
                <Download className="h-3.5 w-3.5 mr-1" />
                Sample YAML
              </Button>
              <Button
                size="sm"
                variant="outline"
                className="flex-1 h-8 text-xs"
                title="Copy sample YAML to clipboard"
                onClick={() => {
                  navigator.clipboard.writeText(SAMPLE_YAML).then(() => {
                    setCopiedSample(true);
                    setTimeout(() => setCopiedSample(false), 2000);
                  });
                }}
              >
                {copiedSample
                  ? <><Check className="h-3.5 w-3.5 mr-1 text-green-600" /><span className="text-green-600">Copied!</span></>
                  : <><Clipboard className="h-3.5 w-3.5 mr-1" />Copy</>
                }
              </Button>
            </div>
          </div>
        </div>

        {/* Right — editor / diff / history */}
        <div className="flex-1 border rounded-lg overflow-hidden flex flex-col bg-background">
          {!selected && !editorYaml ? (
            <div className="flex-1 flex items-center justify-center text-muted-foreground text-sm">
              Select a config from the list, or click <strong className="mx-1">New Config</strong> to create one.
            </div>
          ) : (
            <>
              <div className="px-4 py-2 border-b flex flex-wrap items-center gap-x-3 gap-y-2">
                <div className="flex items-center gap-2 min-w-0 flex-shrink-0">
                  <Label className="text-xs text-muted-foreground whitespace-nowrap">Slug</Label>
                  <Input
                    className="h-7 text-xs font-mono w-36"
                    value={editorSlug}
                    onChange={e => setEditorSlug(e.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, ""))}
                    placeholder="e.g. myuniversity"
                  />
                  <span className="text-xs text-muted-foreground hidden md:inline truncate max-w-[180px]" title={`scraper_config/unis/${editorSlug || "…"}.yaml`}>→ scraper_config/unis/{editorSlug || "…"}.yaml</span>
                </div>
                <div className="flex items-center gap-1.5 flex-wrap flex-1 justify-end">
                  {/* Trigger scrape button + status in editor header */}
                  {selected && (
                    <div className="flex items-center gap-2">
                      {selectedJob && (
                        <JobStatusBadge state={selectedJob} />
                      )}
                      <Button
                        size="sm"
                        variant="outline"
                        className={cn(
                          "h-7 text-xs",
                          selectedConfig?.university_id == null
                            ? "text-muted-foreground cursor-not-allowed"
                            : "text-green-700 border-green-300 hover:bg-green-50",
                          selectedJobActive && "text-blue-600 border-blue-300",
                        )}
                        disabled={triggering === selected || !!selectedJobActive || selectedConfig?.university_id == null}
                        title={
                          selectedConfig?.university_id == null
                            ? "No university linked — add '# Hostname: your.domain.edu.au' to the YAML comment"
                            : selectedJobActive
                            ? "Scrape already running"
                            : `Trigger scrape for ${selectedConfig?.university_name ?? selected}`
                        }
                        onClick={() => void triggerScrape(selected)}
                      >
                        {triggering === selected || selectedJobActive
                          ? <Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" />
                          : <Play className="h-3.5 w-3.5 mr-1" />}
                        {triggering === selected
                          ? "Starting…"
                          : selectedJobActive
                          ? "Running…"
                          : selectedConfig?.university_id == null
                          ? "Not linked"
                          : "Trigger scrape"}
                      </Button>
                    </div>
                  )}

                  {/* View toggle buttons */}
                  <div className="flex items-center border rounded-md overflow-hidden">
                    <button
                      onClick={() => setView("editor")}
                      className={cn(
                        "flex items-center gap-1 px-2.5 py-1 text-xs transition-colors",
                        view === "editor" ? "bg-primary text-primary-foreground" : "hover:bg-muted/50"
                      )}
                      title="Edit YAML"
                    >
                      <Code className="h-3.5 w-3.5" />
                      Editor
                      {isDirty && view !== "editor" && (
                        <span className="inline-block w-1.5 h-1.5 rounded-full bg-amber-400" />
                      )}
                    </button>
                    <button
                      onClick={() => setView("diff")}
                      className={cn(
                        "flex items-center gap-1 px-2.5 py-1 text-xs border-l transition-colors",
                        view === "diff" ? "bg-primary text-primary-foreground" : "hover:bg-muted/50",
                        isDirty && view !== "diff" && "text-amber-600 dark:text-amber-400"
                      )}
                      title="Preview changes vs saved version"
                    >
                      <GitCompare className="h-3.5 w-3.5" />
                      Changes
                      {isDirty && (
                        <span className={cn(
                          "inline-block w-1.5 h-1.5 rounded-full",
                          view === "diff" ? "bg-amber-200" : "bg-amber-400"
                        )} />
                      )}
                    </button>
                    {selected && (
                      <button
                        onClick={() => setView("history")}
                        className={cn(
                          "flex items-center gap-1 px-2.5 py-1 text-xs border-l transition-colors",
                          view === "history" ? "bg-primary text-primary-foreground" : "hover:bg-muted/50"
                        )}
                        title="View save history"
                      >
                        <History className="h-3.5 w-3.5" />
                        History
                      </button>
                    )}
                  </div>

                  <Button
                    size="sm"
                    variant={diagnosisOpen ? "default" : "outline"}
                    className={cn("h-7 text-xs", diagnosisOpen ? "" : "border-blue-300 text-blue-700 hover:bg-blue-50 dark:border-blue-700 dark:text-blue-400 dark:hover:bg-blue-950/40")}
                    onClick={() => {
                      if (diagnosisOpen && diagnosisResult) { setDiagnosisOpen(false); }
                      else { void handleDiagnose(); }
                    }}
                    disabled={diagnosing}
                    title="Auto-diagnose scrape issues using real data from the last scrape job"
                  >
                    {diagnosing
                      ? <><Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" />Diagnosing…</>
                      : <><Bot className="h-3.5 w-3.5 mr-1" />Diagnose &amp; Fix</>
                    }
                  </Button>
                  <Button
                    size="sm"
                    variant={aiFixOpen ? "default" : "outline"}
                    className="h-7 text-xs"
                    onClick={() => { setAiFixOpen(o => !o); setDiagnosisOpen(false); }}
                    title="Fix YAML with AI — describe a change and Gemini applies it"
                  >
                    <Wand2 className="h-3.5 w-3.5 mr-1" />
                    Fix with AI
                  </Button>
                  {aiFixPrev !== null && (
                    <Button
                      size="sm"
                      variant="outline"
                      className="h-7 text-xs text-amber-700 border-amber-300 hover:bg-amber-50"
                      onClick={() => { setEditorYaml(aiFixPrev!); setAiFixPrev(null); setView("editor"); toast({ title: "Undone", description: "AI fix reverted to previous YAML." }); }}
                      title="Undo the last AI fix"
                    >
                      <Undo2 className="h-3.5 w-3.5 mr-1" />
                      Undo AI
                    </Button>
                  )}
                  <Button size="sm" className="h-7 text-xs" onClick={handleSave} disabled={saving}>
                    <Save className="h-3.5 w-3.5 mr-1" />
                    {saving ? "Saving…" : "Save"}
                  </Button>
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
                </div>
              </div>

              {/* AI Fix panel */}
              {aiFixOpen && (
                <div className="px-4 py-3 border-b bg-violet-50 dark:bg-violet-950/30 flex flex-col gap-2">
                  <div className="flex items-center gap-2">
                    <Wand2 className="h-3.5 w-3.5 text-violet-600 dark:text-violet-400 flex-shrink-0" />
                    <span className="text-xs font-medium text-violet-800 dark:text-violet-300">AI YAML Fix</span>
                    <span className="text-xs text-muted-foreground flex-1">Describe what to change — Gemini updates the YAML for you to review</span>
                    <button onClick={() => setAiFixOpen(false)} className="text-muted-foreground hover:text-foreground">
                      <X className="h-3.5 w-3.5" />
                    </button>
                  </div>
                  <div className="flex gap-2">
                    <input
                      className="flex-1 h-8 rounded-md border border-input bg-background px-3 text-xs focus:outline-none focus:ring-1 focus:ring-violet-400"
                      placeholder={`e.g. "add allow_url_patterns for /courses/ only" or "set bfs_page_budget to 80" or "enable always_sitemap_supplement"`}
                      value={aiFixPrompt}
                      onChange={e => setAiFixPrompt(e.target.value)}
                      onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void handleAiFix(); } }}
                      disabled={aiFixing}
                      autoFocus
                    />
                    <Button
                      size="sm"
                      className="h-8 text-xs bg-violet-600 hover:bg-violet-700 text-white"
                      onClick={handleAiFix}
                      disabled={aiFixing || !aiFixPrompt.trim()}
                    >
                      {aiFixing ? <><Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" />Fixing…</> : <><Wand2 className="h-3.5 w-3.5 mr-1" />Fix</>}
                    </Button>
                  </div>
                  <p className="text-xs text-muted-foreground">
                    Changes appear in the editor — switch to <strong>Changes</strong> to review the diff, then <strong>Save</strong> to persist.
                  </p>
                </div>
              )}

              {/* AI Diagnose & Fix panel */}
              {diagnosisOpen && (
                <div className="border-b bg-blue-50 dark:bg-blue-950/30 flex flex-col">
                  {/* Header */}
                  <div className="px-4 py-3 flex items-center gap-2 border-b border-blue-100 dark:border-blue-900">
                    <Bot className="h-4 w-4 text-blue-600 dark:text-blue-400 flex-shrink-0" />
                    <span className="text-xs font-semibold text-blue-900 dark:text-blue-200">AI Scrape Diagnosis</span>
                    {diagnosing && <span className="text-xs text-blue-600 dark:text-blue-400 flex items-center gap-1"><Loader2 className="h-3 w-3 animate-spin" />Analysing last scrape job…</span>}
                    {diagnosisResult && !diagnosing && (
                      <span className="text-xs text-muted-foreground flex-1">
                        {diagnosisResult.university_found
                          ? `${diagnosisResult.university_name} · ${diagnosisResult.issues.length} issue(s) found`
                          : "University not linked — add a # Hostname: comment to the YAML"}
                      </span>
                    )}
                    <div className="flex items-center gap-2 ml-auto">
                      {/* Optional extra note input */}
                      <input
                        className="h-7 w-48 rounded-md border border-blue-200 dark:border-blue-700 bg-white dark:bg-blue-950/60 px-2 text-xs focus:outline-none focus:ring-1 focus:ring-blue-400 placeholder:text-muted-foreground"
                        placeholder="Optional note (e.g. fees are in PDF)"
                        value={diagnosisPrompt}
                        onChange={e => setDiagnosisPrompt(e.target.value)}
                        onKeyDown={e => { if (e.key === "Enter") void handleDiagnose(); }}
                        disabled={diagnosing}
                      />
                      <Button size="sm" variant="outline" className="h-7 text-xs border-blue-300 text-blue-700 hover:bg-blue-100 dark:border-blue-700 dark:text-blue-400" onClick={handleDiagnose} disabled={diagnosing}>
                        {diagnosing ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
                      </Button>
                      <button onClick={() => setDiagnosisOpen(false)} className="text-muted-foreground hover:text-foreground">
                        <X className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  </div>

                  {/* Loading skeleton */}
                  {diagnosing && (
                    <div className="px-4 py-5 flex flex-col gap-3">
                      {[1,2,3].map(i => (
                        <div key={i} className="flex gap-3 animate-pulse">
                          <div className="h-5 w-5 rounded-full bg-blue-200 dark:bg-blue-800 flex-shrink-0" />
                          <div className="flex-1 space-y-1.5">
                            <div className="h-3 bg-blue-200 dark:bg-blue-800 rounded w-1/3" />
                            <div className="h-3 bg-blue-100 dark:bg-blue-900 rounded w-2/3" />
                          </div>
                        </div>
                      ))}
                    </div>
                  )}

                  {/* Results */}
                  {diagnosisResult && !diagnosing && (
                    <div className="px-4 py-3 flex flex-col gap-3 max-h-[500px] overflow-y-auto">

                      {/* Last job stats bar */}
                      {diagnosisResult.last_job && (
                        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground bg-white dark:bg-blue-950/20 border border-blue-100 dark:border-blue-800 rounded-md px-3 py-2">
                          <span className="font-medium text-foreground">Last scrape:</span>
                          <span>🔍 {diagnosisResult.last_job.raw_discovered} discovered</span>
                          <span>→ {diagnosisResult.last_job.after_filter} after filter</span>
                          <span>→ <strong>{diagnosisResult.last_job.imported}</strong> staged</span>
                          {diagnosisResult.last_job.errors > 0 && <span className="text-red-600">⚠ {diagnosisResult.last_job.errors} errors</span>}
                          {diagnosisResult.last_job.created_at && <span className="text-muted-foreground">· {diagnosisResult.last_job.created_at}</span>}
                        </div>
                      )}

                      {/* Issues */}
                      {diagnosisResult.issues.length > 0 ? (
                        <div className="flex flex-col gap-2">
                          {diagnosisResult.issues.map((issue, idx) => {
                            const isExpanded = diagnosisExpanded[idx] ?? true;
                            const color = issue.severity === "critical"
                              ? "border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-950/30"
                              : issue.severity === "warning"
                              ? "border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/30"
                              : "border-blue-200 dark:border-blue-800 bg-white dark:bg-blue-950/20";
                            const Icon = issue.severity === "critical" ? ShieldAlert : issue.severity === "warning" ? TriangleAlert : Info;
                            const iconColor = issue.severity === "critical"
                              ? "text-red-600 dark:text-red-400"
                              : issue.severity === "warning"
                              ? "text-amber-600 dark:text-amber-400"
                              : "text-blue-500 dark:text-blue-400";
                            const badge = issue.severity === "critical"
                              ? "bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-300"
                              : issue.severity === "warning"
                              ? "bg-amber-100 text-amber-700 dark:bg-amber-900/50 dark:text-amber-300"
                              : "bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-300";
                            return (
                              <div key={idx} className={cn("border rounded-md overflow-hidden", color)}>
                                <button
                                  className="w-full flex items-center gap-2 px-3 py-2 text-left"
                                  onClick={() => setDiagnosisExpanded(prev => ({ ...prev, [idx]: !isExpanded }))}
                                >
                                  <Icon className={cn("h-4 w-4 flex-shrink-0", iconColor)} />
                                  <span className="flex-1 text-xs font-medium text-foreground">{issue.title}</span>
                                  <span className={cn("text-[10px] font-semibold px-1.5 py-0.5 rounded uppercase tracking-wide flex-shrink-0", badge)}>
                                    {issue.severity}
                                  </span>
                                  {isExpanded ? <ChevronUp className="h-3 w-3 text-muted-foreground flex-shrink-0" /> : <ChevronDown className="h-3 w-3 text-muted-foreground flex-shrink-0" />}
                                </button>
                                {isExpanded && issue.detail && (
                                  <div className="px-3 pb-2.5 text-xs text-muted-foreground leading-relaxed border-t border-inherit pt-2">
                                    {issue.detail}
                                  </div>
                                )}
                              </div>
                            );
                          })}
                        </div>
                      ) : (
                        <div className="flex items-center gap-2 text-xs text-green-700 dark:text-green-400 bg-green-50 dark:bg-green-950/30 border border-green-200 dark:border-green-800 rounded-md px-3 py-2">
                          <CheckCircle2 className="h-4 w-4 flex-shrink-0" />
                          No issues detected — the config looks correct.
                        </div>
                      )}

                      {/* Summary */}
                      {diagnosisResult.summary && (
                        <div className="text-xs text-muted-foreground italic border-l-2 border-blue-300 dark:border-blue-600 pl-3">
                          {diagnosisResult.summary}
                        </div>
                      )}

                      {/* Changes to be applied */}
                      {diagnosisResult.has_changes && diagnosisResult.changes.length > 0 && (
                        <div className="bg-white dark:bg-blue-950/20 border border-blue-100 dark:border-blue-800 rounded-md px-3 py-2.5">
                          <p className="text-xs font-medium text-foreground mb-1.5">Config changes ready to apply:</p>
                          <ul className="flex flex-col gap-1">
                            {diagnosisResult.changes.map((c, i) => (
                              <li key={i} className="text-xs text-muted-foreground flex gap-1.5">
                                <span className="text-green-600 dark:text-green-400 flex-shrink-0">+</span>
                                {c}
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}

                      {/* Action buttons */}
                      <div className="flex gap-2 pt-1">
                        {diagnosisResult.has_changes ? (
                          <>
                            <Button size="sm" className="h-8 text-xs bg-blue-600 hover:bg-blue-700 text-white" onClick={applyDiagnosis}>
                              <CheckCircle2 className="h-3.5 w-3.5 mr-1" />
                              Apply Fix &amp; Review Changes
                            </Button>
                            <Button size="sm" variant="outline" className="h-8 text-xs" onClick={() => setDiagnosisOpen(false)}>
                              Dismiss
                            </Button>
                          </>
                        ) : (
                          <Button size="sm" variant="outline" className="h-8 text-xs" onClick={() => setDiagnosisOpen(false)}>
                            Close
                          </Button>
                        )}
                      </div>
                    </div>
                  )}
                </div>
              )}

              {/* Draft-restored banner */}
              {draftBanner && draftBanner.slug === (selected ?? editorSlug) && (
                <div className="flex items-center gap-3 px-4 py-2 bg-amber-50 dark:bg-amber-950/40 border-b border-amber-200 dark:border-amber-800 text-amber-800 dark:text-amber-300 text-xs">
                  <span className="flex-1">
                    Draft restored — <strong>{draftBanner.lineCount} lines</strong> of unsaved edits recovered from a previous session.
                  </span>
                  <button
                    className="underline underline-offset-2 hover:no-underline font-medium"
                    onClick={() => setDraftBanner(null)}
                  >
                    Keep draft
                  </button>
                  <button
                    className="underline underline-offset-2 hover:no-underline font-medium text-red-600 dark:text-red-400"
                    onClick={discardDraft}
                  >
                    Discard
                  </button>
                </div>
              )}

              {view === "history" ? (
                <HistoryPanel
                  history={history}
                  loading={historyLoading}
                  hasMore={historyHasMore}
                  loadingMore={historyLoadingMore}
                  savedYaml={savedYaml}
                  selectedEntry={historySelectedEntry}
                  compareEntry={historyCompareEntry}
                  onSelectEntry={setHistorySelectedEntry}
                  onSetCompareEntry={setHistoryCompareEntry}
                  onRestore={handleRestore}
                  onLoadMore={() => {
                    if (!selected || historyLoadingMore) return;
                    const minId = history.length > 0 ? Math.min(...history.map(e => e.id)) : undefined;
                    void fetchHistory(selected, { beforeId: minId, append: true });
                  }}
                  restoringId={restoringId}
                />
              ) : view === "diff" ? (
                <DiffViewer oldYaml={savedYaml} newYaml={editorYaml} />
              ) : (
                <textarea
                  ref={textareaRef}
                  className="flex-1 resize-none font-mono text-xs p-4 bg-muted/20 focus:outline-none focus:bg-background transition-colors"
                  value={editorYaml}
                  onChange={e => setEditorYaml(e.target.value)}
                  spellCheck={false}
                  placeholder={`# University Name\n# Hostname: www.example.edu.au\n\ndiscovery: {}\nextraction:\n  fees:\n    default_currency: "AUD"\n`}
                />
              )}

              <div className="px-4 py-1.5 border-t bg-muted/30 text-xs text-muted-foreground flex items-center gap-4">
                {view === "history" ? (
                  <span>{history.length} saved version{history.length !== 1 ? "s" : ""}</span>
                ) : view === "diff" ? (
                  <>
                    <span>Diff: saved → current edit</span>
                    {isDirty ? (
                      <span className="text-amber-600 dark:text-amber-400">Unsaved changes</span>
                    ) : (
                      <span>No changes</span>
                    )}
                  </>
                ) : (
                  <>
                    <span>{editorYaml.split("\n").length} lines</span>
                    {isDirty ? (
                      <span className="text-amber-600 dark:text-amber-400">Unsaved changes</span>
                    ) : (
                      <span>Changes take effect on next scrape job</span>
                    )}
                  </>
                )}
                {selectedConfig?.university_name && (
                  <span className="text-green-700">
                    ✓ Linked to {selectedConfig.university_name}
                  </span>
                )}
                {selected && selectedConfig?.university_id == null && (
                  <span className="text-amber-600">
                    ⚠ No university linked — add <code className="font-mono">{'# Hostname: www.example.edu'}</code> to the YAML
                  </span>
                )}
              </div>
            </>
          )}
        </div>
      </div>

      {/* Generate modal */}
      {showNewModal && (
        <div className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4">
          <div className={cn("bg-background rounded-xl shadow-xl w-full p-6 space-y-4", newModalMode === "manual" ? "max-w-2xl" : "max-w-md")}>
            <div className="flex items-center justify-between">
              <h2 className="font-semibold text-lg">New University Config</h2>
              <button onClick={() => setShowNewModal(false)} className="p-1 rounded hover:bg-muted">
                <X className="h-4 w-4" />
              </button>
            </div>

            {newModalMode === "ai" ? (
              <>
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
                    <div className="mt-1">
                      <CountrySelect
                        value={genForm.country}
                        onChange={v => setGenForm(f => ({ ...f, country: v }))}
                        className="h-9"
                      />
                    </div>
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
                  <Button variant="outline" className="flex-1" onClick={() => setShowNewModal(false)} disabled={generating}>
                    Cancel
                  </Button>
                  <Button
                    className="flex-1"
                    onClick={handleGenerate}
                    disabled={generating || !genForm.university_name.trim() || !genForm.website_url.trim()}
                  >
                    {generating
                      ? <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                      : <Sparkles className="h-4 w-4 mr-2" />}
                    {generating ? "Working…" : "Generate with AI"}
                  </Button>
                </div>

                {generating && (
                  <div className="space-y-1.5">
                    <div className="flex items-center gap-2 text-xs text-muted-foreground">
                      <Loader2 className="h-3 w-3 animate-spin shrink-0" />
                      <span className="truncate">{genStage}</span>
                    </div>
                    <div className="w-full h-1 bg-muted rounded-full overflow-hidden relative">
                      <style>{`@keyframes indbar{0%{transform:translateX(-100%)}100%{transform:translateX(350%)}}`}</style>
                      <div className="absolute h-full w-1/3 bg-primary rounded-full" style={{ animation: "indbar 1.4s ease-in-out infinite" }} />
                    </div>
                  </div>
                )}

                {!generating && (
                  <div className="flex flex-col items-center gap-1">
                    <p className="text-xs text-muted-foreground">
                      Uses Gemini AI · crawls the site first · review before saving
                    </p>
                    <button
                      className="text-xs text-muted-foreground hover:text-foreground underline underline-offset-2"
                      onClick={() => setNewModalMode("manual")}
                    >
                      <Code className="inline h-3 w-3 mr-1 -mt-0.5" />
                      Write manually instead
                    </button>
                  </div>
                )}
              </>
            ) : (
              <>
                <p className="text-sm text-muted-foreground">
                  Enter a slug and write or paste the YAML config directly. The sample template is pre-loaded — edit as needed.
                </p>

                <div className="space-y-3">
                  <div>
                    <Label className="text-xs">Config Slug *</Label>
                    <Input
                      className="mt-1 font-mono"
                      placeholder="e.g. macquarie or utas"
                      value={manualSlug}
                      onChange={e => setManualSlug(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ""))}
                    />
                    <p className="text-[11px] text-muted-foreground mt-1">
                      Lowercase letters, numbers, hyphens. Usually the university's short name.
                    </p>
                  </div>
                  <div>
                    <div className="flex items-center justify-between mb-1">
                      <Label className="text-xs">YAML Config</Label>
                      <button
                        className="text-[11px] text-muted-foreground hover:text-foreground underline"
                        onClick={() => setManualYaml(SAMPLE_YAML)}
                      >
                        Reset to template
                      </button>
                    </div>
                    <textarea
                      className="w-full h-64 font-mono text-xs border rounded-md p-2 bg-muted/30 resize-y focus:outline-none focus:ring-2 focus:ring-ring"
                      value={manualYaml}
                      onChange={e => setManualYaml(e.target.value)}
                      spellCheck={false}
                    />
                  </div>
                </div>

                <div className="flex gap-2 pt-1">
                  <Button variant="outline" className="flex-1" onClick={() => setShowNewModal(false)}>
                    Cancel
                  </Button>
                  <Button
                    className="flex-1"
                    onClick={handleCreateManually}
                    disabled={!manualSlug.trim()}
                  >
                    <Code className="h-4 w-4 mr-2" />
                    Open in Editor
                  </Button>
                </div>
              </>
            )}
          </div>
        </div>
      )}

      {/* Unsaved changes confirmation dialog */}
      {pendingSlug !== null && (
        <div className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4">
          <div className="bg-background rounded-xl shadow-xl w-full max-w-sm p-6 space-y-4">
            <div>
              <h2 className="font-semibold text-base">Unsaved changes</h2>
              <p className="text-sm text-muted-foreground mt-1">
                You have unsaved edits to <span className="font-mono font-medium">{selected ?? "current draft"}</span>.
                Switching to <span className="font-mono font-medium">{pendingSlug}</span> will discard them.
              </p>
            </div>
            <div className="flex gap-2">
              <Button
                variant="outline"
                className="flex-1"
                onClick={() => setPendingSlug(null)}
              >
                Keep editing
              </Button>
              <Button
                variant="destructive"
                className="flex-1"
                onClick={confirmDiscard}
              >
                Discard & switch
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
