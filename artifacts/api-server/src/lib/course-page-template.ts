/**
 * Detects common course detail page layouts so we can run template-first extraction
 * on bulk scrapes (sample 1–3 pages, merge votes, apply to matching pages, generic fallback otherwise).
 *
 * Implemented without Cheerio so this module stays lightweight and testable with plain Node.
 */

export type CoursePageTemplateKind =
  | "elementor_summary_blocks"
  | "vit_keyword_summary"
  | "course_card_panels"
  | "unknown";

export type CoursePageTemplate = {
  kind: CoursePageTemplateKind;
  /** 0–1 — higher means more signals matched */
  confidence: number;
};

function stripScriptsAndStyles(html: string): string {
  return html
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<noscript[\s\S]*?<\/noscript>/gi, " ");
}

/** Extract inner text of each h1–h6 (handles nested spans). */
function forEachHeadingInnerText(html: string, fn: (inner: string) => void): void {
  const block = stripScriptsAndStyles(html).slice(0, 500_000);
  const re = /<h[1-6][^>]*>([\s\S]*?)<\/h[1-6]>/gi;
  let m: RegExpExecArray | null;
  while ((m = re.exec(block)) !== null) {
    const inner = m[1].replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
    if (inner) fn(inner);
  }
}

function headingMatchesSummaryLabel(inner: string): boolean {
  const t = inner.replace(/[:\s]+$/g, "").trim();
  return (
    /^cricos\s*code$/i.test(t) ||
    /^intakes?$/i.test(t) ||
    /^course\s*length$/i.test(t) ||
    /^campus$/i.test(t) ||
    /^delivery\s*mode$/i.test(t) ||
    /^study\s*mode$/i.test(t)
  );
}

function countSummaryHeadings(html: string): number {
  let n = 0;
  forEachHeadingInnerText(html, (inner) => {
    if (headingMatchesSummaryLabel(inner)) n++;
  });
  return n;
}

function hasElementorSignals(html: string): boolean {
  return /elementor-widget-text-editor|elementor-element/i.test(html.slice(0, 120_000));
}

function countCourseCardPanels(html: string): number {
  const m = html.match(/course-card-panel__item/gi);
  return m ? m.length : 0;
}

/**
 * Classify a single fetched course page (no network).
 */
export function detectCoursePageTemplate(html: string, url: string): CoursePageTemplate {
  if (!html || html.length < 200) return { kind: "unknown", confidence: 0 };

  const panelItems = countCourseCardPanels(html);
  if (panelItems >= 3) {
    return { kind: "course_card_panels", confidence: Math.min(1, 0.55 + panelItems * 0.05) };
  }

  let host = "";
  try {
    host = new URL(url).hostname.toLowerCase();
  } catch {
    /* ignore */
  }

  const text = stripScriptsAndStyles(html).replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").slice(0, 80_000);
  if (/vit\.edu\.au$/i.test(host) && /\bLocations:\s*/i.test(text) && /\b20\d{2}\s+intakes:/i.test(text)) {
    return { kind: "vit_keyword_summary", confidence: 0.92 };
  }

  const summaryHits = countSummaryHeadings(html);
  const elementor = hasElementorSignals(html);
  if (summaryHits >= 4 && elementor) {
    return { kind: "elementor_summary_blocks", confidence: Math.min(1, 0.45 + summaryHits * 0.1) };
  }
  if (summaryHits >= 5) {
    return { kind: "elementor_summary_blocks", confidence: Math.min(1, 0.4 + summaryHits * 0.09) };
  }

  if (panelItems >= 2) {
    return { kind: "course_card_panels", confidence: 0.65 };
  }

  return { kind: "unknown", confidence: 0 };
}

/**
 * Merge 1–3 per-page detections into one batch hint.
 * - 1 sample: use it if not unknown
 * - 2 samples: both must agree (same kind)
 * - 3+ samples: majority (≥2) wins
 */
export function mergeBatchCoursePageTemplates(detections: CoursePageTemplate[]): CoursePageTemplate {
  const valid = detections.filter((d) => d && d.kind !== "unknown");
  if (valid.length === 0) return { kind: "unknown", confidence: 0 };

  const total = detections.length;
  if (total === 1) {
    return valid[0]!;
  }

  const byKind = new Map<CoursePageTemplateKind, { votes: number; sum: number }>();
  for (const d of valid) {
    const cur = byKind.get(d.kind) || { votes: 0, sum: 0 };
    cur.votes += 1;
    cur.sum += d.confidence;
    byKind.set(d.kind, cur);
  }

  let bestKind: CoursePageTemplateKind = "unknown";
  let bestVotes = 0;
  for (const [k, v] of byKind) {
    if (v.votes > bestVotes) {
      bestVotes = v.votes;
      bestKind = k;
    }
  }

  const minVotes = total >= 3 ? 2 : 2;
  if (bestVotes < minVotes) return { kind: "unknown", confidence: 0 };

  const agg = byKind.get(bestKind)!;
  return {
    kind: bestKind,
    confidence: Math.min(1, agg.sum / agg.votes),
  };
}

/**
 * Use batch consensus only when the current page independently matches the same layout kind.
 */
export function pickEffectiveCourseTemplate(
  batchHint: CoursePageTemplate | null | undefined,
  page: CoursePageTemplate,
): CoursePageTemplate {
  if (batchHint && batchHint.kind !== "unknown" && page.kind === batchHint.kind) {
    return {
      kind: batchHint.kind,
      confidence: Math.min(1, (batchHint.confidence + page.confidence) / 2),
    };
  }
  if (page.kind !== "unknown") return page;
  return { kind: "unknown", confidence: 0 };
}
