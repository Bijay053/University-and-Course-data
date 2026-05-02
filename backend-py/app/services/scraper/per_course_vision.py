"""Per-course vision-OCR fallback for image-only english requirement
tables (T208).

Some universities publish their English-language requirement matrix as a
PNG / SVG inside the course page rather than as machine-readable text —
the WhatUni / RMIT pattern. The HTTP and browser passes both return HTML
without extractable scores, the fee/IELTS extractors find nothing, and
the row stages with empty english slots that block auto-publish.

Mirrors Node's ``perCourseVisionFallback`` (routes/scrape.ts:11790):
1. Take the rendered HTML from the per-course browser pass.
2. Parse out every ``<img>`` tag, drop decorative assets (logo, icon,
   banner, hero, sprite), and keep at most :data:`_MAX_IMAGES` candidates.
3. Download each candidate, send the bytes + the english-requirements
   prompt to Gemini Vision.
4. Parse the resulting plain-text dump back through
   :func:`english_test.extract` and merge any new slot values.

Activation gate: ``GEMINI_API_KEY`` must be set AND at least one of
IELTS/PTE/TOEFL/CAE must still be empty after the browser pass. Without
both we no-op so the scrape stays cheap.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Awaitable, Callable, Final
from urllib.parse import unquote, urljoin

import httpx
from bs4 import BeautifulSoup

from app.config import settings
from app.services.ai import gemini_client
from app.services.scraper.extractors import english_test
from app.services.scraper.extractors.base import ExtractionResult

log = logging.getLogger(__name__)

# Overall-only slots — used for the "is anything still missing?" gate
# (we won't pay for vision when every overall is filled) and for the
# early-stop loop check inside the candidate-image walk. Sub-bands are
# excluded from this set on purpose: a course page that only fills
# `*_overall` should still trigger vision so we can recover sub-bands
# from the image, and conversely we shouldn't keep OCR'ing extra images
# just to backfill sub-bands once every overall is known.
_ENGLISH_OVERALL_SLOTS: Final = (
    "ielts_overall",
    "pte_overall",
    "toefl_overall",
    "cambridge_overall",
    "duolingo_overall",
)

# Output-filter slots — superset of overall + sub-bands. Vision results
# matching any of these keys are persisted into the merged payload and
# evidence rows. Without sub-bands here we used to silently drop
# `ielts_listening`, etc. even when the english_test extractor parsed
# them out of the Gemini response, leaving sub-bands stuck on the
# uni-wide PDF fallback (ASA: every course showed sub-bands = 5.5 even
# when the course-page MaSTER.png clearly says 6.0 across the board).
_ENGLISH_OUTPUT_SLOTS: Final = (
    *_ENGLISH_OVERALL_SLOTS,
    "ielts_listening", "ielts_reading", "ielts_writing", "ielts_speaking",
    "pte_listening", "pte_reading", "pte_writing", "pte_speaking",
    "toefl_listening", "toefl_reading", "toefl_writing", "toefl_speaking",
)

# Backwards-compat alias for any external import / test that referenced
# the old name. New code should use one of the two pairs above.
_ENGLISH_SLOTS: Final = _ENGLISH_OVERALL_SLOTS

# Pages that wrap the requirements table in an image often have it as
# the *only* substantial graphic — we cap at 6 to avoid burning the
# Gemini budget on hero / banner / sponsor logos that slipped through
# the decorative filter. Node uses 8; 6 is a safer default for the
# Gemini-only Python build.
_MAX_IMAGES: Final = 6
_IMG_TAG_RE = re.compile(r"<img\b[^>]*?>", re.IGNORECASE)
_SRC_RE = re.compile(r"\bsrc\s*=\s*\"([^\"]+)\"|\bsrc\s*=\s*'([^']+)'", re.IGNORECASE)
# Lazy-loading attributes used by modern sites — tried in order after src.
# data-src is most common (Intersection Observer pattern); others are
# plugin-specific (lazysizes → data-srcset, WP lazy → data-lazy,
# Cloudflare/Shopify → data-original, generic → data-lazy-src).
_LAZY_SRC_ATTRS: Final = (
    "data-src",
    "data-lazy-src",
    "data-lazy",
    "data-original",
)
_LAZY_SRC_RE: Final = re.compile(
    r'\b(?:' + '|'.join(re.escape(a) for a in _LAZY_SRC_ATTRS) + r')\s*=\s*"([^"]+)"|'
    r'\b(?:' + '|'.join(re.escape(a) for a in _LAZY_SRC_ATTRS) + r")\s*=\s*'([^']+)'",
    re.IGNORECASE,
)
_ALT_RE = re.compile(r"\balt\s*=\s*\"([^\"]*)\"|\balt\s*=\s*'([^']*)'", re.IGNORECASE)

# Substrings that flag an image filename as decorative. Checked against
# the URL-decoded filename (last path segment) and alt text — NOT the
# full URL — to avoid false-positives on CDN path hashes.
#
# NOTE: Do NOT use \b word-boundary regex here. Python regex treats `_`
# as a word character (\w), so `\bicon\b` fails to match in a CDN filename
# like ``bec3d_Icon-facebook_2.png`` because the underscore before "icon"
# prevents the boundary from firing.  Simple substring matching on just the
# filename is more reliable and covers all the real-world patterns we see.
_DECORATIVE_HINTS: Final = (
    "logo", "icon", "banner", "hero", "sprite", "avatar", "favicon",
    "social", "footer", "header", "nav", "menu", "decoration",
    "spinner", "loader", "placeholder", "thumb", "thumbnail",
    "sponsor", "partner", "facebook", "twitter", "instagram", "insta",
    "linkedin", "youtube", "tiktok",
    # Contact / UI widgets — never contain requirements tables
    "phone", "email", "map-marker", "map_marker", "marker",
    # ASA-style square-format logos ("ASA square 32.png")
    "square",
    # UI chrome / navigation affordances
    "arrow", "chevron", "breadcrumb", "external",
    # People / scene / marketing illustrations (VIT: students.svg, award.svg)
    # These are always decorative photography or artwork — never data tables.
    "student", "students", "award", "trophy", "medal",
    "campus", "building", "library",
    "people", "person", "staff", "teacher", "lecturer", "professor",
    "graduate", "graduation",
    "photo", "photograph", "picture", "image", "pic",
    "illustration", "graphic", "artwork", "background", "bg",
    "hero-image", "feature",
    # Testimonial / portrait / profile photos — never contain data tables
    "testimonial", "testimonials", "portrait", "headshot",
    "profile", "review", "reviews", "team-member", "team_member",
    "case-study", "case_study", "success-story", "success_story",
    "alumni", "alum",
)

# File extensions that are ALWAYS skipped by the vision extractor.
# SVG is a vector format — it is used for icons, logos, and illustrations,
# never for scanned English-requirement tables (which are always raster
# screenshots: PNG, JPG, WebP). Sending an SVG to Gemini Vision causes it
# to hallucinate scores because the model "sees" shapes it interprets as
# numbers, not actual scanned data. GIF is excluded for the same reason
# (animated UI affordances, not data images).
_SKIP_EXTENSIONS: Final = frozenset({".svg", ".gif"})

# Exact filename stems (without extension) that are always decorative UI
# affordances regardless of surrounding context. Used in addition to
# _DECORATIVE_HINTS so that short common names like "link" don't
# accidentally match legitimate image names that contain "link" as a
# substring (e.g. a hypothetical "curriculum-link-chart.png").
_DECORATIVE_EXACT_STEMS: Final = frozenset({
    "link", "chain", "check", "tick", "cross", "close", "play",
    "download", "share", "search", "upload", "edit", "delete",
    "add", "remove", "back", "next", "prev", "forward",
    "plus", "minus", "caret", "dot", "bullet",
})

# URL *path* substrings (checked against the full lowercased URL, not just
# the filename) that indicate a site-wide marketing or application-process
# image — never a per-course English-requirement table.
#
# These fire BEFORE Gemini is called so we don't pay for OCR on images like
#   /shared/how-to-apply/how-to-apply-1-tua.jpg   (generic apply steps)
#   /shared/marketing/hero-international.jpg       (branding photo)
#   /site-assets/apply-now-banner.png              (CTA graphic)
#
# Rule: if the full image URL (lowercased) contains any of these substrings
# it is skipped, even if its filename would otherwise survive _DECORATIVE_HINTS.
_GENERIC_MARKETING_PATH_BLOCKS: Final = (
    "/how-to-apply",
    "/how-to-enrol",
    "/how-to-enroll",
    "/apply-now",
    "/application-process",
    "/applying-to",
    "/admissions-process",
    "/site-assets",
    "/shared/marketing",
    "/shared/site-wide",
    "/shared/global",
    "/shared/header",
    "/shared/footer",
    "/shared/nav",
    "/shared/apply",
    "/shared/how-to",
    "/shared/cta",
    "/shared/callout",
    "/global/",
    "/marketing/",
)

# Regex applied to the FULL absolute URL (lower-cased) to catch social media
# icons and chrome components that don't appear in the filename and whose
# containing page element uses non-standard class names (e.g. Flinders AEM:
# <div class="reference parbase"> rather than <footer> or class="footer").
#
# Rationale: static path block-lists fail on every new university because each
# site uses its own asset paths.  Checking for social platform names and
# "social-footer" path segments in the URL itself is both university-agnostic
# and very low false-positive risk — course content URLs never contain
# "facebook", "bluesky", "social-footer", etc.
_SOCIAL_CHROME_PATH_RE = re.compile(
    r"/"
    r"(?:social[-_]?(?:footer|bar|media|icons?|links?|sharing)|"
    r"reference[-_]components?/social|"
    r"facebook|twitter(?:[-_]bird)?|instagram|linkedin|youtube|"
    r"tiktok|bluesky|whatsapp|pinterest|snapchat|discord|"
    r"x[-_](?:twitter|social)|threads[-_]?(?:app)?)"
    r"(?:[/?_.-]|$)",
    re.IGNORECASE,
)

# URL path-segment keywords that strongly suggest the image contains
# English language requirement scores.  Images whose URL (lower-cased)
# contains any of these move to the FRONT of the processing queue so
# the early-stop condition triggers sooner and avoids paying for OCR on
# decorative images that slipped through the basic filter above.
_ENGLISH_IMG_PRIORITY_KEYWORDS: Final = (
    "english", "master", "bachelor", "undergraduate", "postgraduate",
    "ielts", "pte", "toefl", "cambridge", "duolingo", "language",
    "requirement", "admission", "entry",
)

_VISION_PROMPT: Final = (
    "You are reading an image taken from a university course page. The "
    "image likely contains an English-language requirements table or "
    "an admissions chart. Extract every English-test score VISIBLE IN "
    "THE IMAGE and return ONLY a plain-text dump with one fact per "
    "line, following this format exactly:\n"
    "  IELTS overall: [number]\n"
    "  IELTS listening: [number]\n"
    "  PTE overall: [number]\n"
    "  TOEFL iBT: [number]\n"
    "  Cambridge Advanced: [number]\n"
    "  Duolingo English Test: [number]\n"
    "CRITICAL RULES:\n"
    "- Only report scores you can literally READ from the image pixels.\n"
    "- Do NOT invent, guess, or recall typical values — ONLY transcribe "
    "what is visually present.\n"
    "- If a score is not shown, omit that line entirely.\n"
    "- If the image is decorative (logo, banner, icon, photo) with no "
    "numeric scores, return nothing at all.\n"
    "- Do NOT add commentary, headings, or markdown."
)


# Regex that matches English / Entry Requirements section headings.
# Used by :func:`_find_english_section_images` to locate images that are
# definitionally inside the requirements section — regardless of filename.
ENGLISH_SECTION_HEADING_RE: Final = re.compile(
    r"(?:English\s+(?:Language\s+)?Requirements?|Entry\s+Requirements?)",
    re.IGNORECASE,
)

# English-test names used for post-OCR validation.
_ENGLISH_TEST_NAMES: Final = ("ielts", "pte", "toefl", "cambridge", "duolingo")

# A valid OCR result must mention at least this many distinct test names.
# Requiring 2+ guards against Gemini hallucinating a single test name when
# processing a logo or decorative image — real requirements tables always
# list multiple tests side-by-side.
_OCR_MIN_TEST_NAMES: Final = 2

# Must also contain at least one numeric score (e.g. "6.5", "58", "85").
_OCR_SCORE_RE: Final = re.compile(r"\b\d{1,3}(?:\.\d)?\b")


def _find_english_section_images(html: str, base_url: str) -> list[tuple[str, str]]:
    """Find ``<img>`` tags inside a DOM section headed by English/Entry Requirements.

    Filename-agnostic — works for images with opaque CDN names such as
    ``Screenshot%202026-01-19%20104316.png`` (the ASAHE Bachelor image) that
    contain no level or English-requirement hint in the URL.

    Strategy: use BeautifulSoup to find every text node that matches
    :data:`ENGLISH_SECTION_HEADING_RE`, walk up to the nearest block
    container (``<section>``, ``<article>``, ``<div>``), then collect every
    ``<img>`` inside that container.  Lazy-loading attributes
    (``data-src`` / ``data-lazy-src`` / ``data-lazy`` / ``data-original``)
    are tried in order when ``src`` is absent or a data-URI.

    Returns ``[(absolute_url, alt_text)]`` deduplicated in DOM order.
    The caller (:func:`_extract_img_candidates`) promotes these to tier-0
    so they are processed before all other candidates.
    """
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    try:
        soup = BeautifulSoup(html or "", "lxml")
    except Exception:  # noqa: BLE001
        return result

    for text_node in soup.find_all(string=ENGLISH_SECTION_HEADING_RE):
        container = text_node.find_parent(["section", "article", "div"])
        if not container:
            continue
        for img in container.find_all("img"):
            src: str = img.get("src") or ""
            if not src or src.startswith("data:"):
                for attr in ("data-src", "data-lazy-src", "data-lazy", "data-original"):
                    src = img.get(attr) or ""
                    if src and not src.startswith("data:"):
                        break
            if not src or src.startswith("data:"):
                continue
            # Skip vector / animated formats — they are always decorative
            # (icons, logos, illustrations) and cause Gemini to hallucinate.
            _ext = "." + src.rsplit(".", 1)[-1].split("?")[0].lower() if "." in src else ""
            if _ext in _SKIP_EXTENSIONS:
                continue
            try:
                absolute = urljoin(base_url, src)
            except Exception:  # noqa: BLE001
                continue
            if absolute in seen:
                continue
            seen.add(absolute)
            alt: str = img.get("alt") or ""
            result.append((absolute, alt))

    return result


def _is_valid_english_ocr_result(ocr_text: str) -> bool:
    """Return ``True`` when OCR output looks like a real English requirements table.

    Two conditions must both hold:

    1. At least :data:`_OCR_MIN_TEST_NAMES` (2) distinct test names appear
       (IELTS, PTE, TOEFL, Cambridge, Duolingo).  A logo or decorative image
       that slips through the filename filter and triggers a Gemini
       hallucination will typically only mention one test (or none), whereas
       a genuine requirements table always lists several tests side-by-side.

    2. At least one numeric score (e.g. ``6.5``, ``58``, ``85``) is present.
       Guards against prose like "Prepare for IELTS and PTE" that contains
       test names but no actual scores.
    """
    if not ocr_text:
        return False
    lower = ocr_text.lower()
    distinct_tests = sum(1 for t in _ENGLISH_TEST_NAMES if t in lower)
    if distinct_tests < _OCR_MIN_TEST_NAMES:
        return False
    return bool(_OCR_SCORE_RE.search(ocr_text))


# Mapping from test keyword → tuple of fields that belong to that test.
# Used by :func:`_vision_fields_consistent_with_ocr` to cross-validate
# that every test claimed in the extracted fields has its name present in
# the raw OCR text.
_TEST_FIELD_MAP: Final = {
    "ielts": (
        "ielts_overall", "ielts_listening", "ielts_reading",
        "ielts_writing", "ielts_speaking",
    ),
    "pte": (
        "pte_overall", "pte_listening", "pte_reading",
        "pte_writing", "pte_speaking",
    ),
    "toefl": (
        "toefl_overall", "toefl_listening", "toefl_reading",
        "toefl_writing", "toefl_speaking",
    ),
    "cambridge": ("cambridge_overall",),
    "duolingo": ("duolingo_overall",),
}

# Page-text keywords for each test. Multiple aliases because universities spell
# them differently ("Duolingo", "DET", "Duolingo English Test"; "Cambridge",
# "CAE", "C1 Advanced", "C2 Proficiency"). Checked against plain text extracted
# from the rendered course-page HTML — if none of a test's keywords appear
# anywhere on the page, any vision result for that test is a hallucination.
_PAGE_TEST_KEYWORDS: Final[dict[str, tuple[str, ...]]] = {
    "ielts":     ("ielts",),
    "pte":       ("pte", "pearson test of english"),
    "toefl":     ("toefl",),
    "cambridge": ("cambridge", "cae", "c1 advanced", "c2 proficiency"),
    "duolingo":  ("duolingo", "det "),
}


def _extract_page_text(html: str) -> str:
    """Return lower-cased plain text from rendered course-page HTML.

    Uses BeautifulSoup to strip all tags so keyword matching runs against
    actual page content, not attribute noise or CDN path strings.
    """
    try:
        soup = BeautifulSoup(html or "", "lxml")
        return soup.get_text(" ", strip=True).lower()
    except Exception:  # noqa: BLE001
        return (html or "").lower()


def _tests_in_page_text(page_text: str) -> frozenset[str]:
    """Return the set of test keys (from _TEST_FIELD_MAP) found in page_text."""
    return frozenset(
        test
        for test, keywords in _PAGE_TEST_KEYWORDS.items()
        if any(kw in page_text for kw in keywords)
    )


def _filter_by_page_tests(
    normalized: dict[str, Any],
    page_tests: frozenset[str],
    *,
    img_url: str = "",
) -> dict[str, Any]:
    """Remove fields for tests whose keywords don't appear anywhere on the page.

    When Gemini Vision returns (e.g.) ``cambridge_overall=170`` but the page
    HTML never mentions "cambridge", "CAE", or "C1 Advanced", that value is
    a hallucination — there is nothing on the page for the model to have read.
    This filter discards such fields before they reach the evidence table.

    IELTS is always kept — it appears on virtually every Australian university
    page and false-negatives from an absent keyword would be harmful.

    This catches *consistent* hallucinations where Gemini invents both the
    test name in its output text (passing _vision_fields_consistent_with_ocr)
    and the score — because neither check currently reads the original HTML.
    """
    filtered: dict[str, Any] = {}
    dropped: list[str] = []
    for k, v in normalized.items():
        owning_test: str | None = None
        for test, fields in _TEST_FIELD_MAP.items():
            if k in fields:
                owning_test = test
                break
        if owning_test is None or owning_test == "ielts":
            # Unknown field or IELTS — always keep
            filtered[k] = v
        elif owning_test in page_tests:
            filtered[k] = v
        else:
            dropped.append(f"{k}={v}")
    if dropped:
        log.info(
            "[VISION FILTER] %s: discarded %s — test keyword not found on page",
            img_url or "?",
            ", ".join(dropped),
        )
    return filtered


# Sub-band groups used by :func:`_infer_missing_subbands`. Keys are the
# overall slot; values are the four sub-band slots for that test.
_SUBBAND_GROUPS: Final = {
    "ielts_overall": (
        "ielts_listening", "ielts_reading", "ielts_speaking", "ielts_writing",
    ),
    "pte_overall": (
        "pte_listening", "pte_reading", "pte_speaking", "pte_writing",
    ),
    "toefl_overall": (
        "toefl_listening", "toefl_reading", "toefl_speaking", "toefl_writing",
    ),
}


def _infer_missing_subbands(normalized: dict[str, Any]) -> None:
    """Fill in missing sub-bands for a test using the value of found sub-bands.

    Australian university English requirements tables list every sub-band for
    a given test as the same score (e.g. IELTS L/R/S/W all = 6.0, TOEFL
    L/R/S/W all = 20).  When Gemini's OCR is incomplete and only returns some
    sub-bands, infer the remaining ones so the cache entry is comprehensive and
    sibling courses don't fall back to the wrong uni-wide PDF value.

    Mutates ``normalized`` in-place.  Only fills *missing* keys — never
    overwrites a value that Gemini explicitly returned.
    """
    for overall_key, sbands in _SUBBAND_GROUPS.items():
        if overall_key not in normalized:
            continue  # test not present in this OCR result — skip
        found_values = [normalized[s] for s in sbands if s in normalized]
        if not found_values:
            continue  # no sub-band found at all — cannot infer safely
        # Use the first found sub-band value as the template for missing ones.
        # Real tables always share the same value, so the first is representative.
        inferred = found_values[0]
        for sb in sbands:
            if sb not in normalized:
                normalized[sb] = inferred


def _vision_fields_consistent_with_ocr(
    ocr_text: str, normalized: dict[str, Any]
) -> bool:
    """Cross-check: every test that appears in ``normalized`` must also be
    named in ``ocr_text``.

    This catches the case where ``english_test.extract`` parses a value for
    (e.g.) TOEFL from a half-hallucinated Gemini response that never actually
    mentioned the word "TOEFL" — a strong indicator that the parser matched
    noise and the entire result should be discarded.

    Does NOT catch consistent hallucinations where Gemini both names the test
    AND invents a plausible score — those are addressed by Fix 1 (decorative
    filter) and Fix 3 (tier-0 authority model).
    """
    lower = ocr_text.lower()
    for test_name, fields in _TEST_FIELD_MAP.items():
        if any(normalized.get(f) for f in fields) and test_name not in lower:
            return False
    return True


_CHROME_TAG_RE = re.compile(
    # CSS class or id values that indicate the element is a chrome/global
    # component rather than per-course content.  Checked against the
    # *value* of each class token and the id attribute independently.
    r"footer|header|nav(?:igation|bar)?|sidebar|side[-_]?bar|"
    r"social(?:[-_]footer|[-_]bar|[-_]links?|[-_]icons?|[-_]media)?|"
    r"breadcrumb|cookie|skip[-_]?(?:link|nav|to)|"
    r"utility[-_]bar|chrome|global[-_](?:nav|header|footer)|"
    r"site[-_](?:nav|header|footer)|top[-_]bar|bottom[-_]bar|"
    r"reference[-_]components?",
    re.IGNORECASE,
)


def _strip_chrome_html(html: str) -> str:
    """Remove header/footer/nav/aside and social chrome elements from HTML.

    The path-block and filename-hint filters are a block-list strategy that
    fails for every new university because each site uses its own asset paths
    (e.g. Torrens uses /shared/how-to-apply/, Flinders uses
    /reference-components/social-footer/).  The only durable fix is to strip
    structural chrome elements from the HTML *before* the image scan, using
    the DOM itself rather than URL heuristics.

    We decompose:
    • Semantic HTML5 chrome tags: <header>, <footer>, <nav>, <aside>
    • Any element whose class or id contains a chrome keyword (footer,
      header, nav, sidebar, social-footer, reference-components, etc.)

    The tier-0 English-section scan is unaffected — it already uses a
    separate BeautifulSoup pass on the original HTML.
    """
    try:
        soup = BeautifulSoup(html or "", "lxml")
        # 1. Semantic chrome tags — always structural, never course-content.
        for tag in soup.find_all(["header", "footer", "nav", "aside"]):
            tag.decompose()
        # 2. Class-based / id-based chrome elements.
        for tag in soup.find_all(True):  # all elements
            classes = " ".join(tag.get("class") or [])
            tag_id = tag.get("id") or ""
            if _CHROME_TAG_RE.search(classes) or _CHROME_TAG_RE.search(tag_id):
                tag.decompose()
        return str(soup)
    except Exception:  # noqa: BLE001 — never crash the scrape over a strip failure
        return html


def _extract_img_candidates(
    html: str, base_url: str
) -> tuple[list[tuple[str, str]], frozenset[str]]:
    """Return ``[(absolute_url, alt_text)]`` for non-decorative ``<img>``.

    Three priority tiers (lower number = higher priority):

    * **Tier 0** — images found by :func:`_find_english_section_images` inside
      a DOM section headed "English Requirements" / "Entry Requirements".
      Filename-agnostic: even opaque CDN names like a screenshot timestamp
      are included here if they live in the right section.
    * **Tier 1** — images whose URL contains a keyword from
      ``_ENGLISH_IMG_PRIORITY_KEYWORDS`` (``master``, ``bachelor``,
      ``ielts``, ``requirement``, etc.).
    * **Tier 2** — all other non-decorative images.

    The early-stop loop in :func:`maybe_vision_refetch` fires as soon as
    every overall slot is filled, so images processed later (tier 1/2) are
    only OCR'd if the tier-0 image didn't satisfy all overalls.

    The raw list is capped at ``_MAX_IMAGES`` after sorting.

    Lazy-loading support: modern sites use ``data-src`` / ``data-lazy-src``
    / ``data-lazy`` / ``data-original`` instead of ``src`` (Intersection
    Observer pattern).  The ``src`` attribute on those tags is either absent
    or a 1×1 transparent GIF placeholder — useless for OCR.  We now try
    ``src`` first; if it is missing or a data-URI we fall back to the lazy
    attributes in ``_LAZY_SRC_RE`` order so the real image URL is found.
    """
    # ── Tier 0: DOM-based English-section images ───────────────────────────
    # Build a set of absolute URLs for these so we can de-duplicate them out
    # of the regex scan below (they'd otherwise appear at tier 1 or 2 too).
    tier0 = _find_english_section_images(html, base_url)
    tier0_urls = {item[0] for item in tier0}

    # ── Tiers 1 & 2: regex scan of chrome-stripped <img> tags ─────────────
    # Strip <header>, <footer>, <nav>, <aside>, and class/id-matched chrome
    # elements BEFORE scanning so that social media icons, footer logos, and
    # nav thumbnails never reach the Gemini vision API.  This is the durable
    # replacement for the path-block list, which failed on both Torrens
    # (/shared/how-to-apply/) and Flinders (/reference-components/social-footer/)
    # because every university uses different asset paths.
    _html_no_chrome = _strip_chrome_html(html)
    raw: list[tuple[str, str]] = []
    for tag in _IMG_TAG_RE.findall(_html_no_chrome):
        m_src = _SRC_RE.search(tag)
        src = (m_src.group(1) or m_src.group(2) or "").strip() if m_src else ""
        # Lazy-load fallback: data-src / data-lazy-src / data-lazy / data-original
        if not src or src.startswith("data:"):
            m_lazy = _LAZY_SRC_RE.search(tag)
            if m_lazy:
                src = (m_lazy.group(1) or m_lazy.group(2) or "").strip()
        if not src or src.startswith("data:"):
            continue
        m_alt = _ALT_RE.search(tag)
        alt = (m_alt.group(1) or m_alt.group(2) or "").strip() if m_alt else ""
        # Check the URL-decoded FILENAME (last path segment) + alt text for
        # decorative hints using simple substring matching. Do NOT apply the
        # check to the full URL string — CDN paths contain opaque hashes like
        # ``bec3d_Icon-facebook_2.png`` where the hash-underscore prefix
        # (``bec3d_``) is classified as a word character (\w) by Python regex,
        # causing \b word-boundary patterns to miss the keyword entirely.
        _filename = unquote(src).split("/")[-1].lower()
        # Skip vector / animated formats unconditionally — SVG/GIF are never
        # scanned requirements tables; sending them to Gemini causes hallucination
        # (e.g. VIT's students.svg → fabricated IELTS 6.5, award.svg → DET 100).
        _ext = "." + _filename.rsplit(".", 1)[-1].split("?")[0] if "." in _filename else ""
        if _ext in _SKIP_EXTENSIONS:
            continue
        _alt_lower = alt.lower()
        if any(h in _filename or h in _alt_lower for h in _DECORATIVE_HINTS):
            continue
        # Exact-stem check: short UI affordance names like "link.png" that
        # don't contain any _DECORATIVE_HINTS substring but are clearly icons.
        _stem = _filename.rsplit(".", 1)[0]
        if _stem in _DECORATIVE_EXACT_STEMS:
            continue
        # Resolve to absolute URL first — the path-block check MUST run on
        # the absolute URL so that relative srcs without a leading slash
        # (e.g. ``how-to-apply/img.jpg``) are correctly caught.  Checking
        # the raw src attribute misses those because the block-list entries
        # all start with "/" (e.g. "/how-to-apply").
        try:
            absolute = urljoin(base_url, src)
        except Exception:  # noqa: BLE001 — never fail the scrape on a bad src
            continue
        # Generic marketing path block: check the FULL absolute URL for
        # known site-wide image directories that never contain per-course
        # English-requirement tables (e.g. /shared/how-to-apply/, /marketing/).
        _abs_lower = absolute.lower()
        if any(block in _abs_lower for block in _GENERIC_MARKETING_PATH_BLOCKS):
            continue
        # Social-chrome path regex: university-agnostic catch for social media
        # icons and chrome components regardless of their hosting path.  Works
        # where the static block-list fails (e.g. Flinders social footer at
        # /reference-components/social-footer/ has no matching block-list entry).
        if _SOCIAL_CHROME_PATH_RE.search(_abs_lower):
            continue
        # Skip images already captured as tier-0 English-section candidates.
        if absolute in tier0_urls:
            continue
        raw.append((absolute, alt))
        if len(raw) >= _MAX_IMAGES * 2:
            break

    # Promote English-requirement images to tier 1 (keyword in URL).
    def _priority(item: tuple[str, str]) -> int:
        url_lower = item[0].lower()
        return 1 if any(kw in url_lower for kw in _ENGLISH_IMG_PRIORITY_KEYWORDS) else 2

    raw.sort(key=_priority)

    # Combine: tier-0 first, then sorted tier-1/2, capped at _MAX_IMAGES.
    combined = tier0 + raw
    return combined[:_MAX_IMAGES], frozenset(tier0_urls)


async def _download(url: str) -> bytes | None:
    """Best-effort image download. ``None`` on any failure (timeout,
    404, oversized payload).
    """
    try:
        async with httpx.AsyncClient(
            timeout=15, follow_redirects=True
        ) as client:
            resp = await client.get(url)
        if resp.status_code >= 400 or not resp.content:
            return None
        # Cap at 4 MB — anything bigger is almost certainly a hero
        # image, not a requirements table; we don't want to drop a
        # 50 MB transparent PNG into the Gemini request.
        if len(resp.content) > 4_000_000:
            return None
        return resp.content
    except Exception as exc:  # noqa: BLE001
        log.debug("per_course_vision download %s failed: %s", url, exc)
        return None


VisionImageCache = dict[tuple[str, str], "asyncio.Future[dict[str, Any]]"]
"""Type alias for the per-scrape-run cache used by
:func:`maybe_vision_refetch`.

The cache key is a ``(img_url, course_url)`` tuple so that the same hero
image appearing on two different courses (e.g. a shared Health faculty
banner) never causes one course's IELTS values to be silently reused for
another.  Within a single course the leader/waiter coalescing pattern still
applies — the same ``(img_url, course_url)`` key deduplications concurrent
extraction of the same image on the same page.

Stores ``asyncio.Future`` values (not raw parsed dicts) so that
concurrent coroutines processing the same image URL coalesce into a
single Gemini call — the leader resolves the future, waiters await it.
The orchestrator creates a fresh empty dict per scrape run; callers
should never read from the cache themselves, only pass it through.
Use the :func:`new_vision_image_cache` factory below to construct one
without depending on the internal value type.
"""


def new_vision_image_cache() -> VisionImageCache:
    """Construct a fresh empty per-scrape-run image cache.

    Provided so callers can stay decoupled from the internal Future-based
    representation: see :data:`VisionImageCache` for why we store futures
    instead of plain dicts.
    """
    return {}


async def maybe_vision_refetch(
    url: str,
    rendered_html: str | None,
    payload: dict[str, Any],
    *,
    emit: Callable[..., Awaitable[None]] | None = None,
    image_cache: VisionImageCache | None = None,
    degree_level: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Scan ``<img>`` tags on the course page and ask Gemini Vision to
    read any English-requirement tables found.

    **Phase A behaviour (Priority 2):** vision now runs whenever the
    page contains candidate images — even when every English slot is
    already filled from text extraction.  This ensures course-page image
    values (tier-4 authority) can override wrong tier-3 text values
    (e.g. ASAHE Masters whose true requirements are image-only).

    ``image_cache`` is an optional caller-owned mapping (one per scrape
    run, created via :func:`new_vision_image_cache`) keyed by absolute
    image URL. When the same image appears on multiple course pages
    (ASA's ``MaSTER.png`` lives on every Master page) we OCR it exactly
    once and reuse the parsed values — saving Gemini cost AND eliminating
    per-course vision non-determinism.  The cache stores
    ``asyncio.Future`` values so concurrent coroutines for the same URL
    coalesce: the first ("leader") performs the work, others ("waiters")
    await its result.

    ``degree_level`` is used for level-aware image selection: images
    whose filename matches the course's level (e.g. "master" in
    MaSTER.png for a Master's course) are promoted to the front of the
    processing queue so the correct level image is OCR'd first.

    Returns ``(filled_values, evidence_rows)``.  ``filled_values``
    contains ALL English slots the vision pass found — including slots
    that were already set from text extraction — so the caller can apply
    the authority model and decide whether to override.  Both dicts are
    empty when vision no-ops (no API key, no HTML, no candidate images,
    or Gemini skipped for every candidate).
    """
    if not rendered_html:
        log.info("[VISION SKIP] no rendered HTML — %s", url)
        return {}, []
    if not getattr(settings, "gemini_api_key", None):
        log.warning(
            "[VISION SKIP] GEMINI_API_KEY not configured — vision OCR will "
            "never run until the key is set. url=%s", url
        )
        return {}, []

    candidates, tier0_url_set = _extract_img_candidates(rendered_html, url)
    if not candidates:
        log.info("[VISION SKIP] no candidate images found on page — %s", url)
        return {}, []

    # Compute which English tests are mentioned anywhere in the page HTML.
    # Vision results for tests NOT mentioned on the page are hallucinations
    # (there is nothing for the model to read) and are discarded before
    # they reach the evidence table. Computed once here — not per image —
    # so the cost is one BS4 parse per course, not one per Gemini call.
    _page_text = _extract_page_text(rendered_html)
    _page_tests = _tests_in_page_text(_page_text)
    if _page_tests:
        log.info(
            "[VISION] page mentions tests: %s — %s",
            ", ".join(sorted(_page_tests)), url,
        )

    # ── Level-aware image promotion ───────────────────────────────────────
    # When the page has images for different degree levels (e.g. ASAHE has
    # MaSTER.png and BACHELOR.png), put the image that matches this course's
    # degree level at the front so the early-stop loop picks the right one
    # without burning Gemini calls on the wrong-level image.
    if degree_level:
        _dl = degree_level.lower()
        def _level_key(item: tuple[str, str]) -> int:
            _fn = item[0].lower().split("/")[-1]
            if "master" in _dl and "master" in _fn:
                return 0
            if ("bachelor" in _dl or "undergraduate" in _dl) and (
                "bachelor" in _fn or "undergrad" in _fn
            ):
                return 0
            if ("doctoral" in _dl or "phd" in _dl or "doctorate" in _dl) and (
                "phd" in _fn or "doctor" in _fn
            ):
                return 0
            if "diploma" in _dl and "diploma" in _fn:
                return 0
            if "certificate" in _dl and ("cert" in _fn or "certificate" in _fn):
                return 0
            return 1  # no level match — process after level-matched images
        candidates = sorted(candidates, key=_level_key)

    log.info(
        "[VISION] %d candidate image(s) found — starting OCR pass for %s",
        len(candidates), url,
    )
    if emit:
        await emit(
            "status",
            f"[VISION] {len(candidates)} candidate image(s) found — starting OCR pass",
            phase="fallback",
            kind="per_course_vision_start",
            url=url,
            candidates=len(candidates),
        )

    filled: dict[str, Any] = {}
    evidence: list[dict[str, Any]] = []
    images_consumed = 0
    cache_hits = 0
    for img_url, alt in candidates:
        # Stop early once the vision pass ITSELF has found every overall
        # slot — saves Gemini calls on extra images once the requirements
        # image has already been read.
        # NOTE: we do NOT check payload.get(k) here — payload values may
        # have come from text extraction (tier 3) and vision (tier 4) is
        # allowed to override them. Stopping on payload values would skip
        # the image entirely when text extraction filled the slot with the
        # wrong value (the ASAHE bug).
        if all(k in filled for k in _ENGLISH_OVERALL_SLOTS):
            break

        # ── Per-image cache lookup with in-flight coalescing ─────────
        # Same image URL seen on a sibling course in this scrape? Reuse
        # whatever english_test parsed out of it last time. This is
        # what makes the 4 ASA Masters internally consistent (all see
        # the same MaSTER.png) without re-paying Gemini for each one.
        #
        # The cache value is an asyncio.Future, NOT the parsed dict
        # directly, so that when N coroutines (e.g. all 4 ASA IT
        # Masters running under _MAX_PARALLEL_FETCH=4) reach this point
        # for the same img_url, only the first one ("leader") performs
        # the download + Gemini call + parse, and the others ("waiters")
        # await the leader's result. Without the Future, all 4 would
        # see "url not in cache", all 4 would race to fire Gemini, and
        # the cache would only help cross-WAVE (later courses) — not
        # the very ASA-Masters scenario the cache was built for.
        normalized: dict[str, Any] | None = None
        cached_method = "per_course_vision"
        leader_future: asyncio.Future[dict[str, Any]] | None = None
        _cache_key = (img_url, url)
        if image_cache is not None:
            existing: asyncio.Future[dict[str, Any]] | None = image_cache.get(_cache_key)
            if existing is None:
                # Be the leader for this URL. Install our Future
                # synchronously (no await between get and set) so any
                # subsequent coroutine in the same event-loop iteration
                # sees it and becomes a waiter.
                leader_future = asyncio.get_running_loop().create_future()
                image_cache[_cache_key] = leader_future
            else:
                # Waiter path: someone is already (or has already) OCR'd
                # this image. Await the Future (resolves instantly if
                # already set) and treat the result as a cache hit.
                try:
                    normalized = await existing
                except Exception:  # noqa: BLE001 — leader's error already logged
                    normalized = {}
                # Infer missing sub-bands in case the leader's OCR was
                # incomplete (e.g. listening returned but not reading).
                # The dict is from the resolved Future so we must copy first.
                if normalized:
                    normalized = dict(normalized)
                    _infer_missing_subbands(normalized)
                cache_hits += 1
                cached_method = "per_course_vision_cached"

        if leader_future is not None or image_cache is None:
            # Leader (or no cache at all) actually does the work.
            try:
                log.info("[VISION] attempting OCR on %s", img_url)
                img_bytes = await _download(img_url)
                if not img_bytes:
                    # Negative-cache: a 404 / oversized image must not
                    # be re-downloaded per sibling course. Resolve the
                    # Future with {} so waiters short-circuit too.
                    log.info("[VISION FAIL] %s: download returned empty (404 or oversized)", img_url)
                    if leader_future is not None:
                        leader_future.set_result({})
                    continue
                images_consumed += 1
                try:
                    resp = await gemini_client.generate_with_images(
                        _VISION_PROMPT, [img_bytes]
                    )
                except Exception as exc:  # noqa: BLE001
                    _fail_msg = (
                        f"[VISION FAIL] {img_url[:70]}: Gemini call failed — {exc} "
                        f"(quota exhausted or API key invalid?)"
                    )
                    log.warning(_fail_msg)
                    if emit:
                        await emit(
                            "status",
                            _fail_msg,
                            phase="fallback",
                            kind="per_course_vision_fail",
                            url=url,
                            image_url=img_url,
                        )
                    # Evict this URL from the image cache so sibling courses
                    # can retry independently instead of inheriting the failure.
                    # (Download failures stay cached because a 404 won't fix
                    # itself; API quota failures may clear between courses.)
                    if image_cache is not None and _cache_key in image_cache:
                        del image_cache[_cache_key]
                    if leader_future is not None and not leader_future.done():
                        leader_future.set_result({})
                    continue
                if resp.skipped or not resp.text:
                    skip_reason = getattr(resp, "skip_reason", "no text returned")
                    _skip_msg = (
                        f"[VISION FAIL] {img_url[:70]}: Gemini skipped — {skip_reason} "
                        f"(likely quota exhausted)"
                    )
                    log.warning(_skip_msg)
                    if emit:
                        await emit(
                            "status",
                            _skip_msg,
                            phase="fallback",
                            kind="per_course_vision_fail",
                            url=url,
                            image_url=img_url,
                        )
                    if image_cache is not None and _cache_key in image_cache:
                        del image_cache[_cache_key]
                    if leader_future is not None and not leader_future.done():
                        leader_future.set_result({})
                    continue
                # Validate that the OCR text contains at least one English-test
                # keyword (IELTS, PTE, TOEFL, Cambridge, Duolingo). This guards
                # against images that are inside the English requirements section
                # of the page but are actually logos, diagrams, or "How to apply"
                # graphics — Gemini returns text for them but none of it refers to
                # a language test, so the result is useless and should be discarded
                # before we attempt to parse scores from it.
                if not _is_valid_english_ocr_result(resp.text):
                    log.info(
                        "[VISION SKIP OCR] %s: Gemini returned text but no "
                        "IELTS/PTE/TOEFL/Cambridge/Duolingo keyword found — "
                        "likely a non-requirements image (logo, diagram, etc.)",
                        img_url,
                    )
                    if leader_future is not None:
                        leader_future.set_result({})
                    continue
                # Wrap the plain-text dump back in a tiny HTML shell so the
                # existing english_test extractor can re-parse it. The
                # extractor walks <p>/<li>-like text — pre-tags work too
                # because the underlying _text helper strips tags.
                text_html = "<pre>" + resp.text + "</pre>"
                try:
                    results: list[ExtractionResult] = await english_test.extract(
                        text_html, url
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("[VISION FAIL] %s: english_test parse failed — %s", img_url, exc)
                    if leader_future is not None:
                        leader_future.set_result({})
                    continue
                normalized = {}
                for r in results:
                    if not r.normalized:
                        continue
                    for k, v in r.normalized.items():
                        if v in (None, "", 0):
                            continue
                        if k not in _ENGLISH_OUTPUT_SLOTS:
                            continue
                        normalized.setdefault(k, v)
                if normalized:
                    # Secondary cross-check: every test claimed in the
                    # extracted fields must also appear in the raw OCR text.
                    # Catches partial hallucinations where english_test.extract
                    # matched noise and the claimed test name is absent from
                    # the Gemini text response.
                    if not _vision_fields_consistent_with_ocr(resp.text, normalized):
                        log.info(
                            "[VISION SKIP OCR] %s: extracted fields inconsistent "
                            "with OCR text (claimed test not named) — discarding",
                            img_url,
                        )
                        normalized = {}
                    else:
                        # Infer any missing IELTS/PTE/TOEFL sub-bands from the
                        # found sub-bands. This ensures the cache entry is
                        # comprehensive even when Gemini's OCR only returned
                        # some sub-bands (e.g. listening but not reading).
                        _infer_missing_subbands(normalized)
                        log.info(
                            "[VISION OK] %s: extracted %s",
                            img_url,
                            " ".join(f"{k}={v}" for k, v in sorted(normalized.items())),
                        )
                else:
                    log.info("[VISION] %s: Gemini returned text but no English scores parsed", img_url)
                if leader_future is not None:
                    leader_future.set_result(dict(normalized))
            except BaseException as exc:
                # Propagate the leader failure to any waiters so they
                # don't await forever. Re-raise so existing exception
                # handling (the outer try/except in extract_course)
                # behaves identically to before.
                if leader_future is not None and not leader_future.done():
                    leader_future.set_exception(exc)
                raise

        if not normalized:
            continue

        # Apply page-text filter: discard test fields whose test keyword
        # doesn't appear anywhere in the course-page HTML.  If a course page
        # never mentions "Cambridge" / "CAE" / "C1 Advanced", any
        # cambridge_overall from vision is a hallucination — there was
        # nothing on the page for Gemini to read it from.
        # Note: the image cache stores UNFILTERED results so sibling courses
        # with different page content apply their own filter independently.
        if _page_tests:
            normalized = _filter_by_page_tests(normalized, _page_tests, img_url=img_url)

        if not normalized:
            continue

        for k, v in normalized.items():
            if k not in _ENGLISH_OUTPUT_SLOTS:
                continue
            if v in (None, "", 0):
                continue
            # `filled` de-dupes within one vision pass (first image wins
            # for any given slot). We do NOT gate on `payload.get(k)` here
            # — the caller applies the authority model (tier-4 vision may
            # override tier-3 text) and decides whether to write to payload.
            if k in filled:
                continue
            filled[k] = v
            evidence.append(
                {
                    "field_key": k,
                    "value": v,
                    "confidence": 0.85,
                    "method": cached_method,
                    # source_url is the image URL, not the course page URL,
                    # so the Evidence Review panel can show exactly which
                    # image the value came from.
                    "source_url": img_url,
                    "snippet": (alt or img_url)[:240],
                    # source_tier=0 means the image was found inside the
                    # English/Entry Requirements DOM section — strong signal.
                    # source_tier=1 means it passed the decorative filter but
                    # wasn't DOM-anchored to the requirements section — treat
                    # as lower-authority: can fill empty slots but must NOT
                    # override existing page-text extractor values.
                    "source_tier": 0 if img_url in tier0_url_set else 1,
                }
            )

    if emit:
        def _fmt(k: str) -> str:
            v = filled.get(k)
            return str(v) if v not in (None, "", 0) else "—"

        cache_note = f" (cache hits {cache_hits})" if cache_hits else ""
        await emit(
            "status",
            f"[per-course vision img {images_consumed}/{len(candidates)}{cache_note}] "
            f"{url} — IELTS={_fmt('ielts_overall')} "
            f"PTE={_fmt('pte_overall')} TOEFL={_fmt('toefl_overall')} "
            f"CAE={_fmt('cambridge_overall')}",
            phase="fallback",
            kind="per_course_vision_done",
            url=url,
            consumed=images_consumed,
            cache_hits=cache_hits,
            filled=list(filled.keys()),
        )

    return filled, evidence
