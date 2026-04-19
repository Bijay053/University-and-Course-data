/**
 * User-pasted scrape targets are often missing a scheme, protocol-relative, or have stray characters.
 * Node's `fetch` / `new URL` require an absolute URL — normalize before any network or URL parsing.
 */

export function tryParseLooseUrl(s: string): URL | null {
  const t = String(s ?? "")
    .replace(/^\uFEFF/, "")
    .trim();
  if (!t) return null;
  let candidate = t;
  // Protocol-relative: //example.com/foo
  if (candidate.startsWith("//")) candidate = `https:${candidate}`;
  try {
    if (/^https?:\/\//i.test(candidate)) return new URL(candidate);
    // Hostname-shaped: www.example.edu.au/path?x=1#h
    if (/^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}([/?#].*)?$/i.test(candidate)) {
      return new URL(`https://${candidate}`);
    }
  } catch {
    return null;
  }
  return null;
}

export function normalizeScrapeUrl(raw: unknown): string {
  let t = String(raw ?? "")
    .replace(/^\uFEFF/, "")
    .trim()
    .replace(/^['"]|['"]$/g, "");
  if (!t) throw new Error("URL is empty");
  if (t.startsWith("//")) t = `https:${t}`;

  const parsed = tryParseLooseUrl(t);
  if (parsed) return parsed.href;

  try {
    if (/^https?:\/\//i.test(t)) return new URL(t).href;
  } catch {
    /* fall through */
  }
  try {
    return new URL(`https://${t}`).href;
  } catch {
    throw new TypeError(`Invalid URL: ${String(raw)}`);
  }
}
