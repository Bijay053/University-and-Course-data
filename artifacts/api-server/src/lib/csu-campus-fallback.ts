import * as cheerio from "cheerio";

/**
 * Real CSU campuses (city/town names). Used by the text-block fallback
 * extractor below for CSU pages where ocb_metadata.course_offerings is absent
 * (typically client-side rendered pages such as
 * /courses/postgraduate/bachelor-veterinary-biology-doctor-veterinary-medicine).
 *
 * Each entry is a canonical display name; the matcher accepts hyphen / space
 * variants (e.g. "Albury-Wodonga" ↔ "Albury Wodonga", "Port Macquarie" with
 * any whitespace) and word-boundary case-insensitive matching.
 */
// Compound-name separator: ASCII hyphen, en-dash, em-dash, non-breaking
// hyphen, forward slash, or whitespace. Covers the punctuation variants the
// CSU pages and AI-rewritten course descriptions actually emit
// (e.g. "Albury–Wodonga" with U+2013 en-dash, "Albury/Wodonga"). Used by
// the multi-word CSU campus patterns below.
const SEP = "[\\s\\-\\u2010\\u2011\\u2013\\u2014/]+";
const CSU_KNOWN_CAMPUSES: ReadonlyArray<{ canonical: string; pattern: RegExp }> = [
  { canonical: "Albury-Wodonga", pattern: new RegExp(`\\bAlbury${SEP}Wodonga\\b`, "i") },
  { canonical: "Bathurst",       pattern: /\bBathurst\b/i },
  { canonical: "Canberra",       pattern: /\bCanberra\b/i },
  { canonical: "Dubbo",          pattern: /\bDubbo\b/i },
  { canonical: "Goulburn",       pattern: /\bGoulburn\b/i },
  { canonical: "Manly",          pattern: /\bManly\b/i },
  { canonical: "Orange",         pattern: /\bOrange\b/i },
  { canonical: "Parramatta",     pattern: /\bParramatta\b/i },
  { canonical: "Port Macquarie", pattern: new RegExp(`\\bPort${SEP}Macquarie\\b`, "i") },
  { canonical: "Wagga Wagga",    pattern: new RegExp(`\\bWagga${SEP}Wagga\\b`, "i") },
];

/**
 * Headings / labels that, on a CSU course page, introduce the campus list.
 * Matched case-insensitively against trimmed text content.
 */
const CSU_CAMPUS_LABEL_RE =
  /^(?:where\s+you\s+can\s+study|study\s+(?:mode\s+and\s+)?locations?|campus(?:es)?(?:\s+locations?)?|available\s+(?:at|in|on(?:\s+campus)?)|delivered\s+(?:at|in)|on[- ]?campus\s+locations?|where\s+to\s+study|locations?)\b\s*[:\-–]?$/i;

export function isCsuCoursePage(url: string): boolean {
  try {
    const host = new URL(url).hostname.toLowerCase();
    return host === "study.csu.edu.au" || host === "sydney.csu.edu.au";
  } catch {
    return false;
  }
}

/**
 * Extract a campus list for CSU pages whose static HTML lacks
 * `ocb_metadata.course_offerings` (client-side rendered pages). Looks for
 * (1) headings that match CSU_CAMPUS_LABEL_RE and harvests known campus names
 * from the immediately following text/list block, and (2) inline "Available
 * at: …" / "Delivered at: …" sentences in the visible page text. Emits the
 * canonical campus names plus an "Online" suffix when an online attendance
 * mode is also indicated. No-op when `data.courseLocation` is already set,
 * and no-op for non-CSU URLs.
 */
export function applyCsuTextualCampusFallback(
  $: ReturnType<typeof cheerio.load>,
  html: string,
  url: string,
  data: { courseLocation?: string },
): void {
  if (!isCsuCoursePage(url)) return;
  if (data.courseLocation) return;

  const found = new Set<string>();
  let onlineMentioned = false;

  const harvestFromText = (raw: string) => {
    if (!raw) return;
    for (const { canonical, pattern } of CSU_KNOWN_CAMPUSES) {
      if (pattern.test(raw)) found.add(canonical);
    }
    if (/\b(?:study\s+)?online\b|\bonline\s+(?:study|delivery|mode|offering)\b/i.test(raw)) {
      onlineMentioned = true;
    }
  };

  // (1) Heading-anchored harvest: find a heading/label whose text matches the
  // CSU campus introducer regex, then read the immediately following sibling
  // for campus names. We restrict the sibling-probe to elements that are
  // semantically a "value block" (lists, definition descriptions, paragraphs,
  // table cells) and we DO NOT walk arbitrary descendant <div>/<span>/<a>
  // chains, because on componentized CSU layouts those bring in unrelated
  // sidebar/news text and produce false positives (P1 from code review).
  const VALUE_BLOCK_TAGS = "ul, ol, dl, dd, p, td";
  $("h1, h2, h3, h4, h5, h6, p strong, p b, dt, .label, .field-label").each((_, el) => {
    const label = $(el).text().trim().replace(/\s+/g, " ");
    if (!CSU_CAMPUS_LABEL_RE.test(label)) return;
    let probe = $(el).next();
    let hops = 0;
    while (probe.length && hops < 3) {
      if (probe.is(VALUE_BLOCK_TAGS)) {
        // Concatenated text() collapses adjacent <li>s into "FooBar" with no
        // separator, breaking our \b-anchored campus patterns. Walk list
        // items / dd children explicitly so each entry stays distinct.
        // For paragraphs we use the raw text since punctuation already
        // separates names.
        const parts = probe.is("ul, ol")
          ? probe.find("li").map((__, li) => $(li).text()).get()
          : probe.is("dl")
            ? probe.find("dd").map((__, dd) => $(dd).text()).get()
            : [probe.text()];
        const blockText = parts
          .map((s) => s.replace(/\s+/g, " ").trim())
          .filter(Boolean)
          .join(", ");
        if (blockText) {
          harvestFromText(blockText);
          if (found.size > 0 || onlineMentioned) break;
        }
      }
      probe = probe.next();
      hops += 1;
    }
    // Inline "Campus: Bathurst, Wagga Wagga" pattern: the label is a strong/b
    // inside a paragraph and the value is the rest of the parent's own text
    // (immediate text nodes only — NOT descendants, which would pull in
    // sibling field labels and unrelated copy).
    if (found.size === 0 && !onlineMentioned) {
      const parent = $(el).parent();
      if (parent.length) {
        const ownText = parent
          .contents()
          .filter((__, n) => (n as { type?: string }).type === "text")
          .map((__, n) => $(n).text())
          .get()
          .join(" ")
          .replace(/\s+/g, " ")
          .trim();
        if (ownText) harvestFromText(ownText);
      }
    }
  });

  // (2) Inline-sentence harvest, scoped per-element. P1 fix from code review:
  // do NOT flatten the entire page into one text blob and then split on
  // punctuation — on componentized pages a single "sentence" can span
  // hundreds of unrelated words, and any anchor phrase (e.g. "available at")
  // would then drag in city mentions from sidebars/news/alumni copy. Instead
  // we iterate <p>/<li>/<dd> elements individually, and only those whose own
  // text contains a campus-anchor phrase are harvested.
  if (found.size === 0) {
    const anchorRe = /\b(?:available\s+(?:at|in|on)|delivered\s+(?:at|in)|study\s+(?:at|on[- ]?campus|location[s]?|mode\s+and\s+location[s]?)|where\s+you\s+can\s+study|campus(?:es)?\s*(?:include|are|:))\b/i;
    $("p, li, dd, td").each((_, el) => {
      const elText = $(el).text().replace(/\s+/g, " ").trim();
      if (elText.length === 0 || elText.length > 600) return; // ignore giant CMS blocks
      if (!anchorRe.test(elText)) return;
      harvestFromText(elText);
    });
  }

  if (found.size === 0 && !onlineMentioned) return;

  const order = new Map(CSU_KNOWN_CAMPUSES.map((c, i) => [c.canonical, i] as const));
  const campuses = [...found].sort((a, b) => (order.get(a) ?? 0) - (order.get(b) ?? 0));
  if (onlineMentioned) campuses.push("Online");
  if (campuses.length > 0) data.courseLocation = campuses.join(", ");
}
