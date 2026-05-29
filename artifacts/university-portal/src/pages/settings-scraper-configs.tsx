import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useToast } from "@/hooks/use-toast";
import { SettingsTabs } from "@/components/settings-tabs";
import { Plus, Save, Trash2, Sparkles, Search, RefreshCw, X, Play, Loader2, CheckCircle2, AlertCircle, Clock, GitCompare, Code, History, RotateCcw, Download, Clipboard, Check } from "lucide-react";
import { cn } from "@/lib/utils";
import { fetchWithAuth } from "@/lib/api";
import { CountrySelect } from "@/components/country-select";

const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");

const SAMPLE_YAML = `# University Full Name
# Hostname: www.example.edu.au
#
# Bug history / rationale:
#   (add notes here as you discover site-specific quirks)

# ── WHEN TO EDIT THIS FILE ────────────────────────────────────────────────────
# Edit when a scrape of THIS university produces wrong or missing data due to
# site-specific quirks. Safe pattern:
#   1. Run scrape → inspect staged rows in the portal
#   2. Find the symptom in the table at the bottom of this file
#   3. Uncomment the relevant field → re-run THIS university only
#
# Do NOT edit scraper_config/defaults.yaml — changes there affect every university
# and require a full regression sweep + human approval.
# Do NOT edit extractor code in app/services/scraper/extractors/ — same risk.
#
# NOT YAML-fixable — escalate to engineering instead:
#   - Page returns 0 bytes even with always_browser_discover/use_stealth_browser →
#     needs a code change to per_course_browser.py (_NETWORKIDLE_HOSTS or
#     _SKIP_BROWSER_HOSTS) or a new XHR/API extraction route.
#   - Cloudflare 403 that use_stealth_browser does not solve (WAF fingerprinting).
#   - Fees live only behind a JS fee calculator (AJAX, no static HTML to parse).
#   - English requirements are behind a login wall.
#   - CRICOS 0% coverage even though the page contains "CRICOS" → regex pattern
#     mismatch; fix is in extractors/cricos_code.py, not YAML.

# ── DISCOVERY ─────────────────────────────────────────────────────────────────
# Settings that are safe to replay against unknown universities (Tier-3).
# They do not assume anything about a specific site's content structure.

discovery:
  # Always merge sitemap results with BFS candidates even when BFS succeeded.
  # Enable for JS-rendered SPAs (Torrens, CDU) or sites where BFS burns its
  # page budget on info/marketing pages (AUT, ACU):
  # always_sitemap_supplement: true

  # Probe extra subdomains when BFS finds fewer than 5 candidates.
  # Use {domain} as a placeholder for the apex domain:
  # fallback_subdomains:
  #   - handbook.{domain}
  #   - study.{domain}
  #   - international.{domain}

  # Drop discovered URLs matching any of these regex patterns (block-list):
  # block_url_patterns:
  #   - /news/
  #   - /events/
  #   - /staff/
  #   - /handbook/handbook-20   # drop old ACU handbooks

  # Keep ONLY URLs matching at least one of these patterns (allow-list).
  # Empty list = allow everything. Narrowing here cuts Gemini cost significantly:
  # allow_url_patterns:
  #   - /courses/
  #   - /programs/
  #   - /study/

  # Require every candidate URL to contain at least one of these substrings
  # (case-insensitive, simpler than allow_url_patterns — no regex needed):
  # must_contain:
  #   - /courses/
  #   - /study/

  # Override the auto-detected sitemap location:
  # sitemap_url: https://www.example.edu.au/custom-sitemap.xml

  # Override the default BFS page budget (12 fast / 25 full).
  # Raise for sites with many listing pages (e.g. UOW ~62 pages):
  # bfs_page_budget: 80

  # Override the candidate URL cap (20 fast / 200 full).
  # Raise when a sitemap publishes more courses than the default cap:
  # max_candidates: 350

  # Run Playwright browser discovery IN ADDITION to BFS (not just as fallback).
  # Enable for Cloudflare-protected sites where plain-HTTP BFS misses faculties:
  # always_browser_discover: true

  # Route browser discovery AND per-course HTML fetches through the stealth
  # Playwright stack. Enable ONLY for hosts where regular headless Playwright
  # fails the Cloudflare challenge (e.g. Macquarie/www.mq.edu.au):
  # use_stealth_browser: true

  # Fall back to Wayback Machine CDX when all live-site discovery fails:
  # use_wayback: true

  # Inject specific course page URLs directly into the discovered set,
  # bypassing BFS / sitemap / browser entirely. Use as a surgical fallback
  # for known-CRICOS courses that all discovery tiers consistently miss:
  # extra_course_urls:
  #   - https://www.example.edu.au/courses/some-hidden-course

# ── EXTRACTION ────────────────────────────────────────────────────────────────
# Settings that depend on a specific university's content structure.
# Must NOT be replayed against unknown universities in Tier-3.

extraction:

  # ── Fees ──────────────────────────────────────────────────────────────────
  fees:
    default_currency: "AUD"   # change to NZD for New Zealand universities

    # URL of the university-wide fee schedule page (not per-course):
    # central_page: https://www.example.edu.au/fees

    # URL of the university-wide fee schedule PDF:
    # fees_pdf_url: https://www.example.edu.au/fees-schedule.pdf

    # Mark all staged courses as "has central fee page" so the staging gate
    # does not reject courses without a per-course fee listing.
    # Use when the university publishes fees on a calculator/central schedule
    # rather than on individual course pages (e.g. UWA research degrees):
    # force_central_fee_stage: true

    # Number of credit points per unit of study. Set when the site publishes
    # per-unit fees (multiply to get full-course fee). null = auto-extract:
    # credit_points_per_unit: 6

    # Prefer the annual figure when the PDF row has both annual AND total:
    # prefer_annual_over_total: true

    # Prefer the Year-1 figure when the course page has both Year-1 AND total:
    # prefer_year_one_over_total: true

    # Let a successful per-course PDF row OVERWRITE values already written by
    # page-regex / Gemini (makes the PDF schedule the authoritative source):
    # pdf_overrides_page_regex: true

    # Switch to the column-aware pdftotext PDF parser for fee schedules where
    # course titles wrap across multiple lines (e.g. Torrens):
    # pdf_parser: "columnar"

    # Per-uni PDF row regex. Must define a named group "cricos". Optional
    # groups: per_unit, annual, total. Use single quotes — no YAML escaping needed:
    # pdf_row_pattern: '(?P<cricos>\\d{6}[A-Z])\\s+(?P<annual>\\$[\\d,]+)'

    # Fee term to emit for per-course PDF rows when it cannot be auto-derived:
    # pdf_fee_term: "Annual"

    # Map DB course names (lower-cased) → names as they appear in the fee PDF.
    # Use when the PDF uses a different qualifier than the DB course name:
    # course_pdf_aliases:
    #   "master of design": "Master of Design (Non-Cognate)"

  # ── English requirements ───────────────────────────────────────────────────
  english:
    # URL of the university-wide English requirements page:
    # central_page: https://www.example.edu.au/english-requirements

    # URL of the English requirements PDF:
    # requirements_pdf_url: https://www.example.edu.au/english-reqs.pdf

    # Set to false if Gemini vision hallucinates IELTS scores from decorative
    # images (e.g. ACAP):
    # trust_vision_ocr: false

    # Set to true only for universities whose requirements live exclusively
    # inside images that the DOM-section detector misses (e.g. ASAHE). Off
    # by default globally:
    # trust_tier1_vision_ocr_english: true

    # University-wide English defaults applied as a last resort when no
    # per-course value is found:
    # default_ielts: 6.5
    # default_pte: 58
    # default_toefl: 80

    # Drop specific test names from extracted results (e.g. if a marketing
    # page mentions a test the university does not actually accept):
    # test_blocklist:
    #   - pte
    #   - kite

  # ── Intake ────────────────────────────────────────────────────────────────
  intake:
    # Write a fixed label into intake_months for research degrees (PhD/MPhil)
    # that have rolling enrolment rather than fixed semester intakes:
    # rolling_enrollment_label: "Rolling"
    # rolling_enrollment_markers:
    #   - "enrolment shall be continuous"
    #   - "rolling intake"

  # ── Filters ───────────────────────────────────────────────────────────────
  filters:
    domestic_only:
      # Enable if the site lists domestic-only courses without marking them:
      enabled: false

    online_only:
      # Disable for distance-education universities (e.g. CSU):
      enabled: true

    # Retry the bare URL once when the broken-CMS short-circuit fires and the
    # URL has a query string. Enable when most pages need a query flag but a
    # small number of pages return a branded error template with that flag:
    # broken_cms_retry_strip_query: true

  # ── URL rewrites ──────────────────────────────────────────────────────────
  # Append query parameters to every course URL before fetching so the
  # international-student view (fees, IELTS, intakes) is visible:
  # url_rewrites:
  #   - host: www.example.edu.au
  #     append_query: "international=true"
  #   - host: www.example.edu.au
  #     path_contains: /courses/
  #     append_query: "audience=INTERNATIONAL"

  # ── Text cleaning ─────────────────────────────────────────────────────────
  text_cleaning:
    location:
      # Regex patterns stripped from raw location strings before parsing.
      # Use single quotes — backslashes need no extra escaping in YAML single-quoted strings:
      # strip_patterns:
      #   - '\\^\\s*\\^.*$'          # ACAP "^ ^Available in Perth" cruft
      #   - '\\bDelivery\\s*method\\b'

    duration:
      # Split compound duration strings on '/' before parsing:
      # split_on_slash: true

      # Reject sentences that match these patterns from the duration tournament
      # (e.g. max-completion-time phrases the global filter misses).
      # Use single quotes for all regex patterns:
      # reject_sentence_patterns:
      #   - 'up to \\d+ years to complete'
      #   - 'up to \\d+ months'
      #   - 'candidature.*\\d+ years'

    # Substrings stripped from EVERY string field on the staged payload.
    # Use for stock boilerplate that pollutes multiple fields:
    # global_substring_blocklist:
    #   - "Apply Now"
    #   - "Find out more"

    # Hard-override a payload field when a course URL matches a regex.
    # Use sparingly — extractor/YAML fixes are preferred:
    # field_overrides:
    #   - url_regex: "/faculty-of-arts/some-course"
    #     field: "course_location"
    #     value: "Melbourne"

  # ── Course name ───────────────────────────────────────────────────────────
  course_name:
    # Strip fixed provider suffixes appended by the CMS before the standard
    # suffix-detection regex runs (matching is from the END of the title):
    # strip_title_suffixes:
    #   - " : the University of Western Australia"

  # ── Staging gate ──────────────────────────────────────────────────────────
  staging:
    # Fields that must be non-empty for a staged course to pass the gate.
    # Default requires only course_name:
    # reject_if_missing:
    #   - course_name
    #   - international_fee

  # ── Concurrency ───────────────────────────────────────────────────────────
  # Cap the number of parallel page fetches for this university.
  # Lower for Cloudflare-heavy sites that rate-limit aggressively (e.g. UTAS):
  # max_parallel_fetch: 2

  # Fallback course_location when all extractors return empty (e.g. UTAS whose
  # Cloudflare occasionally delivers partial HTML that omits the Location panel):
  # default_course_location: "Hobart"

# ── QUICK REFERENCE — symptom → YAML field ────────────────────────────────────
# Symptom                                        Field to set
# ─────────────────────────────────────────────────────────────────────────────
# Discovery finds nav/news pages, not courses    discovery.must_contain
#                                                discovery.block_url_patterns
# Sitemap not auto-discovered                    discovery.sitemap_url
# BFS finds < 5 courses (subdomain)              discovery.fallback_subdomains
# Cloudflare blocks plain-HTTP BFS               discovery.always_browser_discover
# Cloudflare blocks headless Playwright too      discovery.use_stealth_browser
# All courses staged as no_international_fee     extraction.fees.force_central_fee_stage
# Fee PDF has multi-line course titles           extraction.fees.pdf_parser: "columnar"
# Page shows Year-1 fee; we want annual          extraction.fees.prefer_year_one_over_total
# IELTS hallucinated from decorative images      extraction.english.trust_vision_ocr: false
# PTE/TOEFL appears on pages that don't list it  extraction.english.test_blocklist
# Duration shows max-candidature time            extraction.text_cleaning.duration.reject_sentence_patterns
# Location panel blank on Cloudflare-heavy site  extraction.default_course_location
# Location string has CMS junk suffix            extraction.text_cleaning.location.strip_patterns
# Course title ends with " : University of X"   extraction.course_name.strip_title_suffixes
# PhD shows no intake months                     extraction.intake.rolling_enrollment_label
# Domestic-only courses are being staged         extraction.filters.domestic_only.enabled: true
# Site uses per-unit fees                        extraction.fees.credit_points_per_unit
# International view needs a query parameter     extraction.url_rewrites
# ─────────────────────────────────────────────────────────────────────────────
# NOT YAML-fixable (ask engineering):
#   rendered=0B even with browser flags          → per_course_browser.py _NETWORKIDLE_HOSTS
#   scrape times out, all pages blank            → per_course_browser.py _SKIP_BROWSER_HOSTS
#   Cloudflare WAF still blocks stealth browser  → new extraction route needed
#   Fees only on JS calculator (no HTML)         → new XHR extractor needed
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
  const [copiedSample, setCopiedSample] = useState(false);
  const [pendingSlug, setPendingSlug] = useState<string | null>(null);
  const [draftBanner, setDraftBanner] = useState<{ slug: string; lineCount: number } | null>(null);
  const [generating, setGenerating] = useState(false);
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
      } else if (!data.git_skipped && !data.git_pushed && data.git_message) {
        toast({
          title: "Saved locally — GitHub sync failed",
          description: data.git_message,
          variant: "destructive",
        });
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

  const handleGenerate = async () => {
    if (!genForm.university_name.trim() || !genForm.website_url.trim()) {
      toast({ title: "Name and URL required", variant: "destructive" });
      return;
    }
    setGenerating(true);
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
      setGenerating(false);
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
                <div className="flex items-center gap-2 flex-shrink-0">
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
