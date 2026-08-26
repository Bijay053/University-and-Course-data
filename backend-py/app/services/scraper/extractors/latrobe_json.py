"""La Trobe per-course JSON pre-extractor.

La Trobe University ships every course's authoritative metadata as a
separate JSON document published per-locale (domestic / international)
and per-campus, e.g.::

    https://www.latrobe.edu.au/courses/data/2026/international/ci/master-of-public-health?v=0.0.19

The static per-course HTML at ``/courses/<slug>`` lists the URLs of all
locale × campus × year variants in an inline ``"allDetailUrls"`` JSON
block but contains NONE of the actual fee / duration / intake / location
values — those are loaded client-side via the SPA. The standard text
strip therefore gives the regex extractors nothing to bite on, so:

    * ``international_fee`` is NULL on ~all 219 La Trobe rows
    * ``duration`` is NULL on most rows (Gemini fills some with the
      domestic value or hallucinates)
    * ``intake_months`` falls back to the central-page parse which
      returns 0 records
    * ``course_location`` is sometimes the central marketing campus
      list rather than the per-course campus

This module fetches the international JSON document directly and
overrides those fields with the canonical values, the same way
``federation_json`` and ``cqu_json`` work for their respective sites.

Hostname-gated (``is_latrobe_host``) so this is a true no-op for every
other uni in the fleet.
"""
from __future__ import annotations

import html as html_lib
import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

from app.services.scraper.http_fetcher import fetch_html_scrape_do

log = logging.getLogger("uniportal.scraper.latrobe_json")


_CAPTURE_MANIFEST_AND_NAVIGATE = r"""(() => {
  const source = document.documentElement.outerHTML;
  const key = source.indexOf('"allDetailUrls"');
  if (key < 0) {
    window.name = JSON.stringify({courseHtml: source, error: "manifest_missing"});
    return;
  }
  const start = source.indexOf("{", key);
  let depth = 0, inString = false, escaped = false, end = -1;
  for (let i = start; i < source.length; i++) {
    const char = source[i];
    if (escaped) { escaped = false; continue; }
    if (char === "\\") { escaped = true; continue; }
    if (char === '"') { inString = !inString; continue; }
    if (inString) continue;
    if (char === "{") depth++;
    else if (char === "}" && --depth === 0) { end = i + 1; break; }
  }
  if (end < 0) {
    window.name = JSON.stringify({courseHtml: source, error: "manifest_unbalanced"});
    return;
  }
  const allDetailUrls = JSON.parse(source.slice(start, end));
  let detailUrl = "";
  const campusPriority = ["CI", "BU", "ON", "SY"];
  for (const year of Object.keys(allDetailUrls).sort()) {
    const international = allDetailUrls[year] && allDetailUrls[year].international;
    if (international && Object.keys(international).length) {
      for (const campus of campusPriority) {
        if (typeof international[campus] === "string") {
          detailUrl = international[campus];
          break;
        }
      }
      if (!detailUrl) {
        detailUrl = Object.values(international).find(
          value => typeof value === "string"
        ) || "";
      }
      break;
    }
  }
  window.name = JSON.stringify({courseHtml: source, allDetailUrls, detailUrl});
  if (detailUrl) window.location.assign(detailUrl);
})()"""

_COMBINE_COURSE_AND_DETAIL = r"""(() => {
  try {
    const result = JSON.parse(window.name || "{}");
    result.detailText = result.detailUrl
      ? ((document.querySelector("pre") || document.body).textContent || "")
      : "";
    const encoded = JSON.stringify(result)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
    document.body.innerHTML = '<pre id="__latrobe_combined">' + encoded + "</pre>";
  } catch (error) {
    document.body.innerHTML = '<pre id="__latrobe_combined">' +
      JSON.stringify({error: String(error)}) + "</pre>";
  }
})()"""


def _decode_json_response(raw: str) -> dict[str, Any]:
    """Decode either raw JSON or Chromium's HTML-wrapped JSON viewer output.

    Scrape.do ``render=true`` opens JSON endpoints in Chromium. Chromium wraps
    the response in ``<pre>...</pre>`` and HTML-escapes the JSON text, so a
    direct ``json.loads(raw)`` fails even though the authoritative document is
    present. La Trobe needs rendered requests to pass Cloudflare, so unwrap the
    browser representation before giving up.
    """
    try:
        doc = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        match = re.search(r"<pre\b[^>]*>(.*?)</pre>", raw, re.IGNORECASE | re.DOTALL)
        if not match:
            raise
        doc = json.loads(html_lib.unescape(match.group(1)))

    if not isinstance(doc, dict):
        raise ValueError("La Trobe detail response must be a JSON object")
    return doc


async def fetch_course_bundle(
    url: str,
    *,
    wait_for_ms: int = 3000,
    local_concurrency_limit: int | None = None,
) -> tuple[str | None, dict[str, Any] | None, str | None]:
    """Fetch the course shell and international JSON in one browser request.

    La Trobe blocks direct and in-page XHR access to the detail endpoint, but a
    top-level navigation in the already-rendered Scrape.do browser succeeds.
    The browser actions preserve the original course HTML in ``window.name``,
    navigate to the authoritative JSON URL, then return both documents in one
    ``<pre>`` wrapper. The selected detail URL is returned with the document so
    :func:`apply_overrides` can verify it matches Python's canonical selector.
    Any malformed/partial result returns ``(None, None, None)`` so the caller
    can use the established rendered fallback.
    """
    actions: list[dict[str, object]] = [
        {"Action": "Execute", "Execute": _CAPTURE_MANIFEST_AND_NAVIGATE},
        {"Action": "Wait", "Timeout": 8000},
        {"Action": "Execute", "Execute": _COMBINE_COURSE_AND_DETAIL},
        {"Action": "Wait", "Timeout": 200},
    ]
    raw = await fetch_html_scrape_do(
        url,
        render=True,
        wait_for_ms=wait_for_ms,
        play_with_browser=actions,
        unescape_json_html=False,
        local_concurrency_limit=local_concurrency_limit,
    )
    if not raw:
        return None, None, None
    try:
        bundle = _decode_json_response(raw)
    except (json.JSONDecodeError, ValueError):
        log.warning("[LATROBE BUNDLE] %s — combined browser response was invalid", url)
        return None, None, None

    course_html = bundle.get("courseHtml")
    if not isinstance(course_html, str) or not course_html:
        log.warning("[LATROBE BUNDLE] %s — original course HTML was not preserved", url)
        return None, None, None

    detail_doc: dict[str, Any] | None = None
    detail_url = bundle.get("detailUrl")
    if not isinstance(detail_url, str) or not detail_url:
        detail_url = None
    detail_text = bundle.get("detailText")
    if isinstance(detail_text, str) and detail_text.strip():
        try:
            detail_doc = _decode_json_response(detail_text)
        except (json.JSONDecodeError, ValueError):
            log.warning(
                "[LATROBE BUNDLE] %s — navigated detail response was invalid; "
                "the normal detail fetch will retry",
                url,
            )
    log.info(
        "[LATROBE BUNDLE] %s — one browser request preserved %dB course HTML; detail=%s",
        url,
        len(course_html),
        "ready" if detail_doc else "fallback",
    )
    return course_html, detail_doc, detail_url


# ── Host gate ────────────────────────────────────────────────────────────
def is_latrobe_host(url: str) -> bool:
    """Strict netloc check. ``latrobe.edu.au`` or any subdomain only."""
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except (ValueError, AttributeError):
        return False
    if not host:
        return False
    return host == "latrobe.edu.au" or host.endswith(".latrobe.edu.au")


# ── allDetailUrls extraction ─────────────────────────────────────────────
# Anchored on the literal ``"allDetailUrls":`` key. The value is a
# nested object up to 3 levels deep:
#   {year: {locale: {campus_code: url}}}
# We extract the JSON object using a balanced-brace scan rather than
# regex so we tolerate variable nesting / whitespace.
_ALL_DETAIL_KEY = '"allDetailUrls"'


def _slice_balanced_object(html: str, start_idx: int) -> str | None:
    """Return ``html[start_idx:end]`` covering one balanced ``{...}``.

    ``start_idx`` must point at the opening ``{``. Returns None if the
    object is not balanced (truncated page, etc.).
    """
    depth = 0
    in_str = False
    esc = False
    for i in range(start_idx, len(html)):
        ch = html[i]
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return html[start_idx : i + 1]
    return None


def parse_all_detail_urls(html: str) -> dict[str, Any]:
    """Return the parsed ``allDetailUrls`` mapping or ``{}``."""
    if not html:
        return {}
    pos = html.find(_ALL_DETAIL_KEY)
    if pos < 0:
        return {}
    # Skip past key + colon + whitespace to the opening brace.
    brace = html.find("{", pos + len(_ALL_DETAIL_KEY))
    if brace < 0:
        return {}
    raw = _slice_balanced_object(html, brace)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        # The block embeds escaped quotes from JS source — try a softer
        # parse by replacing single-quoted edge cases. Rare; the page
        # source is already valid JSON in every sample inspected.
        return {}


def pick_international_url(all_detail_urls: dict[str, Any]) -> str | None:
    """Return the best international JSON URL.

    Preference order:
        1. lowest year ≥ current scrape year (i.e. 2026 over 2027) so
           we always read the soonest-publishing fee schedule the user
           would see.
        2. campus code ``CI`` (City Melbourne) > ``BU`` (Bundoora) >
           ``ON`` (Online) > whichever first key is published. The
           City / Bundoora campuses publish the canonical full-time
           fee; Online sometimes publishes a part-time-only rate.

    Returns None when no international entry exists at all (extremely
    rare — most La Trobe courses publish to international students).
    """
    if not all_detail_urls:
        return None
    # Years are strings in the JSON.
    years = sorted(k for k in all_detail_urls.keys() if isinstance(k, str) and k.isdigit())
    if not years:
        return None
    for year in years:
        intl = (all_detail_urls.get(year) or {}).get("international") or {}
        if not isinstance(intl, dict) or not intl:
            continue
        for preferred in ("CI", "BU", "ON", "SY"):
            if preferred in intl and isinstance(intl[preferred], str):
                return intl[preferred]
        # Fallback to the first available campus.
        for v in intl.values():
            if isinstance(v, str):
                return v
    return None


# ── Field parsers (all REPLACE — JSON is canonical) ──────────────────────
# Duration tokens: "2 years full-time", "18 months", "0.5 years part-time",
# "1 year". We grab the leading numeric pair only; the qualifying suffix
# is preserved as raw text for evidence.
_DURATION_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s+(?P<unit>year|years|month|months|week|weeks)\b",
    re.IGNORECASE,
)
_UNIT_TO_TERM = {
    "year": "Year",
    "years": "Year",
    "month": "Month",
    "months": "Month",
    "week": "Week",
    "weeks": "Week",
}


def parse_duration(s: str | None) -> tuple[float | None, str | None]:
    if not s:
        return None, None
    m = _DURATION_RE.search(s)
    if not m:
        return None, None
    val = float(m.group("value"))
    if val.is_integer():
        val = int(val)  # type: ignore[assignment]
    return val, _UNIT_TO_TERM[m.group("unit").lower()]


# startDates: "LTU Term 2 (March 2026), LTU Term 3 (May 2026), ..." or
# similar. Pull every month name that appears.
_MONTH_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\b",
    re.IGNORECASE,
)
_MONTH_ABBR = {
    "january": "Jan", "february": "Feb", "march": "Mar", "april": "Apr",
    "may": "May", "june": "Jun", "july": "Jul", "august": "Aug",
    "september": "Sep", "october": "Oct", "november": "Nov", "december": "Dec",
}


def parse_intake_months(s: str | None) -> list[str]:
    if not s:
        return []
    seen: list[str] = []
    for m in _MONTH_RE.findall(s):
        abbr = _MONTH_ABBR[m.lower()]
        if abbr not in seen:
            seen.append(abbr)
    return seen


# Fee parser. Examples seen:
#   "A$44 200 per 120 credit points. <br>Note: 120 credit points represents full-time study for one year."
#   "A$35,600 per year"
#   "A$17,800 per year"
# La Trobe uses a non-breaking space as the thousands separator on the
# JSON ``amountDescription`` field — the existing latrobe.yaml
# "space-thousands-separator" fix handles regular pages but the JSON
# uses a literal U+00A0 (non-breaking space) AND/OR a regular space.
# Our regex accepts both.
_FEE_AMOUNT_RE = re.compile(
    r"A\$\s*(\d{1,3}(?:[\s\u00a0,]\d{3})*(?:\.\d+)?)",
    re.IGNORECASE,
)


def parse_international_fee(fees: dict[str, Any] | None) -> tuple[int | None, str | None]:
    """Return ``(amount, fee_term)`` or ``(None, None)``.

    ``fee_term`` is one of ``"Full Course"``, ``"Annual"`` (per credit
    point amounts are normalised to "Annual" because La Trobe's
    "120 credit points = one year" note makes the per-CP value the
    annual fee for a standard-load student).
    """
    if not fees or not isinstance(fees, dict):
        return None, None
    text = (fees.get("amountDescription") or fees.get("overview") or "").strip()
    if not text:
        # La Trobe's current international detail response exposes both the
        # domestic and international rates in ``fees.rawFees`` instead of the
        # legacy ``amountDescription`` field. Select the explicitly-labelled
        # International row only; falling back to the first row would silently
        # publish the Domestic amount.
        raw_fees = fees.get("rawFees")
        if isinstance(raw_fees, list):
            for row in raw_fees:
                if not isinstance(row, dict):
                    continue
                fee_type = str(row.get("Fee_Type") or "").strip().lower()
                if "international" not in fee_type:
                    continue
                amount = str(row.get("Fee_Amount") or "").strip()
                if not amount:
                    continue
                try:
                    numeric_amount = int(
                        float(re.sub(r"[\s\u00a0,]", "", amount))
                    )
                except (TypeError, ValueError):
                    continue
                if numeric_amount <= 0:
                    continue
                description = str(
                    row.get("Advertised_Fee_Description") or ""
                ).strip()
                text = f"A${numeric_amount:,} {description}".strip()
                break
    if not text:
        return None, None
    m = _FEE_AMOUNT_RE.search(text)
    if not m:
        return None, None
    raw = m.group(1)
    # Strip any thousand separators (regular space, NBSP, comma).
    digits = re.sub(r"[\s\u00a0,]", "", raw)
    try:
        amt = int(float(digits))
    except (ValueError, TypeError):
        return None, None
    if amt <= 0:
        return None, None
    # Normalise term. La Trobe's "120 credit points represents full-
    # time study for one year" makes a "per 120 credit points" rate
    # equivalent to the annual fee. When the per-CP denominator is
    # NOT 120 (e.g. "per 80 credit points" on some Graduate Certs),
    # the value is a load-adjusted rate, NOT the annual fee — labelling
    # it "Annual" would understate the real annual cost. In that case
    # fall through to "Full Course" so the staging layer doesn't treat
    # it as a true annual rate.
    low = text.lower()
    if "per year" in low or "/year" in low or "annual" in low:
        return amt, "Annual"
    if re.search(r"per\s+120\s+credit\s+point", low):
        return amt, "Annual"
    if "credit point" in low:
        # Per-CP value with a non-120 denominator (or no denominator).
        return amt, "Full Course"
    if "full course" in low or "total" in low:
        return amt, "Full Course"
    return amt, "Annual"  # safe default for La Trobe per-year publishing


def international_authority_missing(
    html: str,
    applied: dict[str, Any] | None,
) -> bool:
    """Return True when an international detail exists but yielded no fields.

    A rendered La Trobe SPA shell is non-empty HTML even when the authoritative
    detail navigation fails under provider contention. Without this check, the
    pipeline treats that shell as a successful fetch, applies weak generic
    Online/domestic classifications, and never queues the URL for recovery.
    """
    if applied:
        return False
    return bool(pick_international_url(parse_all_detail_urls(html)))


# ── English requirement parser (entryReq.engReq) ─────────────────────────
# La Trobe publishes the canonical IELTS requirement inside
# ``entryReq.engReq`` as a short HTML snippet, e.g.
#   "<p><span>6.0 IELTS (Academic) with no individual band less than
#    6.0.</span></p>"
#   "<p><span>6.5 IELTS (Academic) with no individual band less than
#    6.0.</span></p>"
# Some courses spell out per-band scores instead, e.g.
#   "7.0 IELTS (Academic) with the following individual band scores:
#    Listening 7.0, Reading 7.0, Writing 7.0, Speaking 7.0"
# Vision OCR / Gemini fallbacks routinely produce wrong values for La
# Trobe (capturing scores from unrelated marketing imagery), so the
# JSON value REPLACES whatever those produced.
_IELTS_OVERALL_RE = re.compile(
    # Three accepted phrasings (all observed live on La Trobe engReq snippets
    # as of 2026-05-17):
    #   1. "6.0 IELTS (Academic)"           — leading score + "Academic" tag
    #   2. "6.5 IELTS (Academic) with …"    — same as (1), longer sentence
    #   3. "IELTS of 7.0 with no individual band score less than 7.0"
    #      — Bachelor of Nursing 2027/international/bu engReq.  Has NO
    #      "Academic" qualifier and the score follows the word "IELTS".
    #      Pattern (3) must come AFTER (1)/(2) in the alternation so the
    #      leading-numeric phrasing wins when both are present in the
    #      same snippet.
    r"(?:(\d(?:\.\d)?)\s*IELTS\s*\(?Academic\)?"
    r"|IELTS\s+of\s+(\d(?:\.\d)?))",
    re.IGNORECASE,
)
_IELTS_NO_BAND_LESS_RE = re.compile(
    r"no\s+individual\s+band\s+(?:score\s+)?(?:less|lower)\s+than\s+(\d(?:\.\d)?)",
    re.IGNORECASE,
)
_IELTS_PER_BAND_RES = {
    "ielts_listening": re.compile(r"Listening\s+(\d(?:\.\d)?)", re.IGNORECASE),
    "ielts_reading":   re.compile(r"Reading\s+(\d(?:\.\d)?)",   re.IGNORECASE),
    "ielts_writing":   re.compile(r"Writing\s+(\d(?:\.\d)?)",   re.IGNORECASE),
    "ielts_speaking":  re.compile(r"Speaking\s+(\d(?:\.\d)?)",  re.IGNORECASE),
}

# PTE Academic overall — La Trobe engReq snippets phrase this as
#   "Pearson Test of English (PTE) Academic. Applicants must achieve a
#    minimum overall score of 65"
# i.e. the PTE mention appears IMMEDIATELY before (or within the same
# clause as) the "minimum overall score of N" sentence. The proximity
# constraint is critical because multi-test engReqs interleave PTE with
# TOEFL / CAE clauses that also use "minimum overall score of N" — a
# document-level co-occurrence rule would lift a TOEFL=79 or CAE=180
# score and mis-attribute it to PTE.  Reject any candidate score whose
# nearest preceding PTE/Pearson mention is more than _PTE_PROXIMITY chars
# away OR which is closer to a competing TOEFL/CAE mention.
_PTE_MENTION_RE = re.compile(
    r"\b(?:PTE|Pearson\s+Test\s+of\s+English)\b",
    re.IGNORECASE,
)
_PTE_OVERALL_RE = re.compile(
    r"minimum\s+overall\s+score\s+of\s+(\d{2,3})",
    re.IGNORECASE,
)
# Competing English-test mentions that also commonly precede "minimum
# overall score of N" sentences. When one of these sits closer to the
# candidate score than the nearest PTE mention does, the score belongs
# to the competing test, NOT PTE.
_COMPETING_TEST_RE = re.compile(
    r"\b(?:TOEFL(?:\s+iBT)?|Cambridge|CAE|C1\s+Advanced|IELTS|Duolingo)\b",
    re.IGNORECASE,
)
# Max chars between the PTE mention and the "minimum overall score of N"
# clause. La Trobe's canonical phrasing keeps them in the same or adjacent
# sentence (≤180 chars including punctuation and "Applicants must
# achieve a "). 220 leaves headroom for HTML-strip whitespace variation.
_PTE_PROXIMITY = 220


# ── Campus-code → display-name map (mirrors La Trobe's CMS) ─────────────
# Used to aggregate course_location across every published intl variant.
# Codes are the third level of allDetailUrls; display names match the
# strings La Trobe shows in its course-page campus dropdown.
_CAMPUS_CODE_TO_NAME: dict[str, str] = {
    "CI": "Melbourne",
    # 2026-05-17: BU (Bundoora) is the canonical Melbourne campus.
    # La Trobe's own per-course campus dropdown
    # (``<select id="courseCampus"><option value="BU">Melbourne</option>``)
    # and the page banner ("Course information for international students
    # at Selected course campus Melbourne in Selected course year 2027")
    # both label BU as "Melbourne", not "Bundoora".  Mapping it to
    # "Melbourne" keeps the staged location consistent with what the
    # public-facing course page tells prospective students.
    "BU": "Melbourne",
    "ME": "Melbourne",
    "BE": "Bendigo",
    "AW": "Albury-Wodonga",
    "WO": "Albury-Wodonga",  # alternate CMS code, same campus as AW
    "MI": "Mildura",
    "SH": "Shepparton",
    "SY": "Sydney",
    "ON": "Online",
}
_CAMPUS_PRIORITY = ("CI", "ME", "BU", "BE", "AW", "WO", "MI", "SH", "SY", "ON")


def _collect_intl_locations(all_detail_urls: dict[str, Any]) -> list[str]:
    """Return ordered, deduped list of intl campus display names.

    Walks every year and gathers the campus codes under
    ``<year>.international``. Returns campus *display names* in
    ``_CAMPUS_PRIORITY`` order. Empty list when the manifest has no
    international entries.
    """
    if not all_detail_urls:
        return []
    seen_codes: set[str] = set()
    for year_block in all_detail_urls.values():
        if not isinstance(year_block, dict):
            continue
        intl = year_block.get("international") or {}
        if not isinstance(intl, dict):
            continue
        for code in intl:
            if isinstance(code, str):
                seen_codes.add(code.upper())
    if not seen_codes:
        return []
    ordered: list[str] = []
    for code in _CAMPUS_PRIORITY:
        if code in seen_codes:
            name = _CAMPUS_CODE_TO_NAME.get(code, code)
            if name not in ordered:
                ordered.append(name)
            seen_codes.discard(code)
    # Trailing unknown campus codes — keep their raw code so we don't
    # silently drop a new campus La Trobe adds in future.
    for code in sorted(seen_codes):
        if code not in ordered:
            ordered.append(_CAMPUS_CODE_TO_NAME.get(code, code))
    return ordered


def _collect_intl_campus_codes(all_detail_urls: dict[str, Any]) -> set[str]:
    """Return normalized campus codes from every international variant."""
    codes: set[str] = set()
    for year_block in (all_detail_urls or {}).values():
        if not isinstance(year_block, dict):
            continue
        intl = year_block.get("international") or {}
        if not isinstance(intl, dict):
            continue
        for code, detail_url in intl.items():
            if isinstance(code, str) and isinstance(detail_url, str) and detail_url:
                codes.add(code.strip().upper())
    return codes


def classify_study_mode(
    data: dict[str, Any],
    all_detail_urls: dict[str, Any],
) -> tuple[str | None, float, str]:
    """Classify La Trobe delivery without trusting SPA marketing copy.

    Explicit OL/OC detail codes are authoritative. Multi-Modal is mapped to
    Blended when a physical campus is published. When the detail omits a mode,
    the international manifest provides a conservative fallback: physical-only
    campuses mean On Campus, a physical+ON mix means Blended, and ON-only means
    Online.
    """
    dmc = str(data.get("deliveryModeCode") or "").strip().upper()
    dmd = str(data.get("deliveryModeDescription") or "").strip().lower()
    campus_codes = _collect_intl_campus_codes(all_detail_urls)
    has_online = "ON" in campus_codes
    physical_codes = campus_codes - {"ON"}
    campus_summary = ",".join(sorted(campus_codes)) or "—"

    # The compact code is the authoritative field. Descriptions are consulted
    # only when the code is absent or unrecognized, because stale upstream
    # descriptions have occasionally contradicted the code.
    if dmc == "OL":
        return "Online", 0.95, (
            f"detail deliveryModeCode={dmc or '—'} "
            f"deliveryModeDescription={dmd or '—'} campuses={campus_summary}"
        )
    if dmc == "OC":
        return "On Campus", 0.95, (
            f"detail deliveryModeCode={dmc or '—'} "
            f"deliveryModeDescription={dmd or '—'} campuses={campus_summary}"
        )

    if dmc in {"MM", "BL"}:
        mode = "Blended" if physical_codes else ("Online" if has_online else "Blended")
        return mode, 0.95, (
            f"detail multi-modal deliveryModeCode={dmc or '—'} "
            f"deliveryModeDescription={dmd or '—'} campuses={campus_summary}"
        )

    if dmd == "online":
        return "Online", 0.90, (
            f"detail description fallback deliveryModeDescription={dmd} "
            f"campuses={campus_summary}"
        )
    if dmd in {"on campus", "on-campus"}:
        return "On Campus", 0.90, (
            f"detail description fallback deliveryModeDescription={dmd} "
            f"campuses={campus_summary}"
        )
    if dmd in {
            "multi-modal",
            "multimodal",
            "multi modal",
            "blended",
            "hybrid",
    }:
        mode = "Blended" if physical_codes else ("Online" if has_online else "Blended")
        return mode, 0.90, (
            f"detail description fallback multi-modal "
            f"deliveryModeDescription={dmd} campuses={campus_summary}"
        )

    if physical_codes and has_online:
        return "Blended", 0.85, (
            f"manifest fallback physical+online campuses={campus_summary}"
        )
    if physical_codes:
        return "On Campus", 0.85, (
            f"manifest fallback physical campuses={campus_summary}"
        )
    if has_online:
        return "Online", 0.85, (
            f"manifest fallback online-only campuses={campus_summary}"
        )
    return None, 0.0, "no delivery evidence"


def _strip_html(s: str) -> str:
    """Remove HTML tags + collapse whitespace; sufficient for engReq."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def _parse_pte(text: str) -> dict[str, float]:
    """Return ``{pte_overall: X}`` or ``{}``.

    Multi-test engReqs interleave PTE with TOEFL / CAE / IELTS clauses
    that ALL use the "minimum overall score of N" template, so a
    document-level co-occurrence rule would silently mis-attribute a
    TOEFL=79 or CAE=180 score to PTE.

    Decision rule per candidate score:
      1. Find every ``minimum overall score of N`` occurrence.
      2. For each, the score belongs to PTE only when the NEAREST
         preceding mention (within ``_PTE_PROXIMITY`` chars) is a
         PTE/Pearson mention — i.e. PTE sits closer than any
         competing TOEFL / CAE / IELTS / Duolingo mention.
      3. Return the FIRST matching candidate.
    """
    if not text:
        return {}
    pte_positions = [m.start() for m in _PTE_MENTION_RE.finditer(text)]
    if not pte_positions:
        return {}
    competing_positions = [m.start() for m in _COMPETING_TEST_RE.finditer(text)]
    for score_match in _PTE_OVERALL_RE.finditer(text):
        s_pos = score_match.start()
        # Distance to nearest PTE mention that precedes this score (or
        # very near it — La Trobe sometimes uses parenthetical PTE that
        # follows the score by a few chars; tolerate a small forward
        # window so we don't lose those cases).
        preceding_pte = [p for p in pte_positions if p <= s_pos]
        if not preceding_pte:
            continue
        pte_dist = s_pos - max(preceding_pte)
        if pte_dist > _PTE_PROXIMITY:
            continue
        # Is there a competing test mention sitting CLOSER to the score
        # than the PTE mention does?  If so, the score is theirs.
        preceding_comp = [p for p in competing_positions if p <= s_pos]
        if preceding_comp:
            comp_dist = s_pos - max(preceding_comp)
            if comp_dist < pte_dist:
                continue
        try:
            return {"pte_overall": float(score_match.group(1))}
        except (ValueError, TypeError):
            continue
    return {}


def parse_eng_req(eng_req_html: str | None) -> dict[str, float]:
    """Return ``{ielts_overall, ielts_listening, ..., pte_overall}`` parsed
    from engReq.

    Empty dict when the HTML is missing or unparseable.
    """
    text = _strip_html(eng_req_html or "")
    if not text:
        return {}
    out: dict[str, float] = {}
    m = _IELTS_OVERALL_RE.search(text)
    if not m:
        # No IELTS — still try to harvest a PTE score from a PTE-only
        # engReq snippet before bailing out.
        pte_only = _parse_pte(text)
        return pte_only
    # Group 1 = "X.X IELTS [Academic]" phrasing; group 2 = "IELTS of X.X"
    # phrasing.  Exactly one is non-None per successful match.
    raw_score = m.group(1) or m.group(2)
    try:
        out["ielts_overall"] = float(raw_score)
    except (ValueError, TypeError):
        return {}
    # Per-band: explicit "Listening X.X, Reading X.X, ..." wins over the
    # "no individual band less than Y.Y" floor (the floor is a minimum,
    # not the actual required score).
    per_band: dict[str, float] = {}
    for key, rx in _IELTS_PER_BAND_RES.items():
        bm = rx.search(text)
        if bm:
            try:
                per_band[key] = float(bm.group(1))
            except ValueError:
                pass
    if per_band:
        out.update(per_band)
    else:
        nb = _IELTS_NO_BAND_LESS_RE.search(text)
        if nb:
            try:
                floor = float(nb.group(1))
                for key in _IELTS_PER_BAND_RES:
                    out[key] = floor
            except ValueError:
                pass
    # Harvest PTE alongside IELTS when both appear in the same engReq.
    out.update(_parse_pte(text))
    return out


# ── Main override entrypoint ─────────────────────────────────────────────
async def apply_overrides(
    payload: dict[str, Any],
    html: str,
    *,
    url: str = "",
    evidence: list[dict[str, Any]] | None = None,
    prefetched_doc: dict[str, Any] | None = None,
    prefetched_url: str | None = None,
    local_concurrency_limit: int | None = None,
) -> dict[str, Any]:
    """Async — fetches the per-course international JSON and overrides.

    Returns a dict describing which overrides fired (for logging /
    evidence trails). Empty dict means nothing applied.

    The overrides REPLACE whatever the regex extractors / AI fallback
    produced, mirroring the Federation / CQU JSON-override convention.
    """
    applied: dict[str, Any] = {}
    if not html:
        return applied

    all_detail = parse_all_detail_urls(html)
    intl_url = pick_international_url(all_detail)
    if not intl_url:
        # When the manifest exists and lists at least one DOMESTIC variant
        # but ZERO international variants across every published year, the
        # course is genuinely not offered to international students (e.g.
        # Master of Nurse Practitioner, Master of Teaching Nexus, Bachelor
        # of Psychology Honours — all AHPRA / APAC / state-funded
        # programs). Flag the payload so the staging guard rejects it
        # with reason="domestic_only" and the completeness scorer skips
        # it instead of warning about a missing international fee.
        has_any_domestic = any(
            isinstance(locales, dict) and "domestic" in locales
            for locales in (all_detail or {}).values()
            if isinstance(locales, dict)
        )
        if all_detail and has_any_domestic:
            payload["domestic_only"] = True
            applied["domestic_only"] = {"old": False, "new": True}
            log.info(
                "[LATROBE JSON] %s — manifest has only domestic variants; "
                "flagging payload domestic_only=True",
                url,
            )
        else:
            log.info("[LATROBE JSON] %s — no international detail URL in allDetailUrls", url)
        return applied

    doc: dict[str, Any] | None = None
    if prefetched_doc is not None and prefetched_url == intl_url:
        doc = prefetched_doc
        log.info("[LATROBE JSON] %s — using detail prefetched in course browser", url)
    elif prefetched_doc is not None:
        log.warning(
            "[LATROBE JSON] %s — prefetched detail URL mismatch "
            "(browser=%s canonical=%s); using rendered canonical fallback",
            url,
            prefetched_url,
            intl_url,
        )

    if doc is None:
        raw = await fetch_html_scrape_do(
            intl_url,
            render=True,
            wait_for_ms=0,
            local_concurrency_limit=local_concurrency_limit,
        )
        if not raw:
            log.warning(
                "[LATROBE JSON] %s — rendered fetch of detail URL %s failed",
                url,
                intl_url,
            )
            return applied
        try:
            doc = _decode_json_response(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            log.warning("[LATROBE JSON] %s — invalid JSON at %s: %s", url, intl_url, exc)
            return applied

    data = (doc or {}).get("data") or {}
    if not data:
        return applied

    # ── 2026-05-17: empty-stub detection ─────────────────────────────────
    # La Trobe occasionally publishes a `allDetailUrls` entry for an
    # international variant whose detail JSON is a STUB — the manifest
    # promises an international offering but the per-course JSON has
    # both ``duration`` and ``entryReq.engReq`` left blank.  Two live
    # confirmations 2026-05-17:
    #   * Graduate Certificate in Business Analytics — 2026/intl/ON:
    #     duration="", engReq="", fee=$16,900 (legacy fee row).
    # The course page itself shows "THIS COURSE IS NOT AVAILABLE TO
    # INTERNATIONAL STUDENTS …" (banner) for every campus selector.
    # Stub variants must NOT stage; flag the payload domestic_only so
    # ``guards.py:394`` rejects the row with reason="domestic_only".
    # Genuine published variants always carry a non-empty ``duration``
    # (e.g. "1.5 years full-time") so duration-empty alone is the
    # high-precision signal.  We also require ``engReq`` empty to keep
    # the rule conservative — a course with a legitimate duration but
    # missing engReq is just an English-data hole, not a stub.
    _dur_raw = (data.get("duration") or "").strip()
    _eng_raw_for_stub = _strip_html(((data.get("entryReq") or {}).get("engReq")) or "")
    if not _dur_raw and not _eng_raw_for_stub:
        payload["domestic_only"] = True
        applied["domestic_only"] = {
            "old": False, "new": True,
            "reason": "intl_detail_json_stub_empty_duration_and_engReq",
        }
        log.info(
            "[LATROBE JSON] %s — intl detail %s is a STUB (empty duration "
            "AND empty engReq); flagging payload domestic_only=True",
            url, intl_url,
        )
        return applied

    # Duration.
    dur_val, dur_term = parse_duration(data.get("duration"))
    if dur_val is not None and dur_term:
        prev = (payload.get("duration"), payload.get("duration_term"))
        payload["duration"] = dur_val
        payload["duration_term"] = dur_term
        applied["duration"] = {"old": prev, "new": (dur_val, dur_term)}

    # The same authoritative duration string carries attendance load, e.g.
    # "0.5 years full-time" or "1 year part-time".  The generic study-load
    # fallback cannot see this because it receives the SPA shell and the
    # numeric duration parser intentionally discards qualifier text.
    _dur_lower = _dur_raw.lower()
    _has_full_time = bool(re.search(r"\bfull[- ]time\b", _dur_lower))
    _has_part_time = bool(re.search(r"\bpart[- ]time\b", _dur_lower))
    _study_load = (
        "Both"
        if _has_full_time and _has_part_time
        else "Full Time"
        if _has_full_time
        else "Part Time"
        if _has_part_time
        else None
    )
    if _study_load:
        prev_load = payload.get("study_load")
        payload["study_load"] = _study_load
        applied["study_load"] = {"old": prev_load, "new": _study_load}
        if evidence is not None:
            evidence.append({
                "field_key": "study_load",
                "value": _study_load,
                "confidence": 0.95,
                "method": "latrobe_json",
                "source_url": intl_url,
                "snippet": f"latrobe_json duration: {_dur_raw}",
            })

    # ── Authoritative study mode from the international detail JSON ─────
    # The SPA shell regularly contains generic "online" marketing copy and
    # no usable per-course location. Its weak rule result can therefore be
    # downgraded to Online before this JSON override runs. At La Trobe, the
    # selected international detail variant explicitly identifies both Online
    # (OL) and On Campus (OC) delivery, so both must override that weak result.
    # Mixed delivery stays with the generic extractor because it needs the
    # wider page context to distinguish Blended from a multi-campus offering.
    _authoritative_mode, _mode_confidence, _mode_snippet = classify_study_mode(
        data,
        all_detail,
    )
    if _authoritative_mode:
        prev_mode = payload.get("study_mode")
        payload["study_mode"] = _authoritative_mode
        applied["study_mode"] = {"old": prev_mode, "new": _authoritative_mode}
        if evidence is not None:
            evidence.append({
                "field_key": "study_mode",
                "value": _authoritative_mode,
                "confidence": _mode_confidence,
                "method": "latrobe_json",
                "source_url": intl_url,
                "snippet": f"latrobe_json {_mode_snippet}",
            })

    # Intake months from startDates.
    months = parse_intake_months(data.get("startDates"))
    if months:
        prev = payload.get("intake_months")
        payload["intake_months"] = months
        applied["intake_months"] = {"old": prev, "new": months}

    # International fee.
    amt, fee_term = parse_international_fee(data.get("fees"))
    if amt is not None:
        prev_fee = payload.get("international_fee")
        payload["international_fee"] = amt
        if fee_term:
            payload["fee_term"] = fee_term
        # La Trobe always publishes in AUD.
        payload.setdefault("currency", "AUD")
        applied["international_fee"] = {"old": prev_fee, "new": amt, "fee_term": fee_term}
        if evidence is not None:
            _fees = data.get("fees") or {}
            _raw = (
                _fees.get("amountDescription")
                or _fees.get("overview")
                or ""
            )
            if not _raw:
                for _row in _fees.get("rawFees") or []:
                    if not isinstance(_row, dict):
                        continue
                    if "international" not in str(
                        _row.get("Fee_Type") or ""
                    ).lower():
                        continue
                    _raw = (
                        f"{_row.get('Fee_Type') or 'International'} "
                        f"{_row.get('Fee_Currency') or 'AUD'} "
                        f"{_row.get('Fee_Amount') or ''}"
                    ).strip()
                    break
            evidence.append({
                "field_key": "international_fee",
                "value": amt,
                "confidence": 0.95,
                "method": "latrobe_json",
                "source_url": intl_url,
                "snippet": f"latrobe_json fees.amountDescription: {_raw}",
                "raw_value": _raw,
            })

    # English requirement — entryReq.engReq is the canonical source.
    # La Trobe's per-course IELTS values from vision OCR / Gemini
    # fallbacks are routinely wrong (they pick up scores from unrelated
    # marketing imagery), so the JSON value REPLACES whatever the
    # extractors produced. PTE is now also harvested when the engReq
    # snippet mentions Pearson Test of English with a "minimum overall
    # score of X" clause (e.g. Bachelor of Nursing 2027/intl/BU which
    # publishes PTE=65 + IELTS=7.0 side-by-side); TOEFL still comes
    # from the central English Language Requirements page.
    eng_req_html = (data.get("entryReq") or {}).get("engReq")
    eng = parse_eng_req(eng_req_html)
    if eng:
        prev = {k: payload.get(k) for k in eng}
        for k, v in eng.items():
            payload[k] = v
        applied["english"] = {"old": prev, "new": eng}
        if evidence is not None:
            _eng_snippet = _strip_html(eng_req_html or "")[:300]
            if "ielts_overall" in eng:
                evidence.append({
                    "field_key": "ielts_overall",
                    "value": eng["ielts_overall"],
                    "confidence": 0.95,
                    "method": "latrobe_json",
                    "source_url": intl_url,
                    "snippet": f"latrobe_json entryReq.engReq: {_eng_snippet}",
                    "raw_value": _eng_snippet,
                })
            if "pte_overall" in eng:
                evidence.append({
                    "field_key": "pte_overall",
                    "value": eng["pte_overall"],
                    "confidence": 0.95,
                    "method": "latrobe_json",
                    "source_url": intl_url,
                    "snippet": f"latrobe_json entryReq.engReq (PTE): {_eng_snippet}",
                    "raw_value": _eng_snippet,
                })

    # Course location — aggregate the display names of EVERY published
    # international campus variant across the manifest, deduped and in
    # priority order (City/Melbourne, Bundoora, Bendigo, Albury-Wodonga,
    # Mildura, Shepparton, Sydney, Online). The static central
    # marketing list is often missing campuses (e.g. Bachelor of
    # Business is offered in Melbourne, Bendigo and Sydney) so the
    # JSON manifest is canonical and REPLACES the existing value.
    locations = _collect_intl_locations(all_detail)
    if locations:
        joined = ", ".join(locations)
        prev_loc = (payload.get("course_location") or "").strip()
        if joined != prev_loc:
            payload["course_location"] = joined
            applied["course_location"] = {"old": prev_loc, "new": joined}
    else:
        # Fall back to the single fetched variant's display name when we
        # could not aggregate from the manifest (older code path).
        loc = (data.get("locationDisplayName") or "").strip()
        if loc:
            prev_loc = (payload.get("course_location") or "").strip()
            if not prev_loc:
                payload["course_location"] = loc
                applied["course_location"] = {"old": prev_loc, "new": loc}

    if applied:
        log.info(
            "[LATROBE JSON] %s — overrides applied via %s: %s",
            url or "(no url)",
            intl_url,
            sorted(applied.keys()),
        )

    return applied
