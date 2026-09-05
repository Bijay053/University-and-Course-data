"""Course name extractor.

Best-effort: takes the first ``<h1>`` (then ``<title>``) and cleans it
the same way the Node ``preserveOriginalCapitalization`` helper does:
preserve standalone acronyms (MBA, BBA, ICT) and prepositions
(of/in/and/for) without lowercasing them. Strips trailing university or
campus suffixes ("- USQ", "| Charles Sturt University").
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from app.services.scraper.extractors.base import ExtractionResult

# Catalogue path segments used by university websites (mirrors discovery.py).
_CATALOGUE_SEGS: frozenset[str] = frozenset({
    "courses", "course", "programs", "programme", "programmes",
    "program", "degrees", "degree", "study",
})

_PREPOSITIONS = {"of", "in", "and", "for", "the", "with", "to", "on", "by"}
_ACRONYMS = {
    "MBA", "BBA", "BA", "MA", "BS", "BSc", "MSc", "PhD", "ICT", "IT", "AI",
    "MD", "JD", "LLM", "LLB", "ME", "MEng", "EMBA", "GDip", "GCert",
    "MIT", "USQ", "CSU", "UTS", "ANU", "UNSW", "UoN", "RMIT",
    # Issue 3b: Roman numerals used in AQF course names (Certificate III,
    # Certificate IV, Diploma etc.). Without these, _smart_case title-cases
    # "III" as "Iii" and "IV" as "Iv".
    "II", "III", "IV", "VI", "VII", "VIII", "IX", "XI", "XII",
    "XIII", "XIV", "XV",
    # "V", "I", "X" are skipped — too likely to be the letter, not a numeral.
}
# Lookup map: uppercase key → canonical form.
# _smart_case does w.upper() before the set check, so mixed-case acronyms
# like "MSc" / "BSc" / "PhD" would never match their all-caps key "MSC" /
# "BSC" / "PHD" and were wrongly title-cased as "Msc" / "Bsc" / "Phd".
# This dict solves that: look up by uppercase, return the canonical form.
_ACRONYM_CANON: dict[str, str] = {a.upper(): a for a in _ACRONYMS}
# Institution suffix tail: matches "Charles Sturt University" / "RMIT" /
# "Federation Uni" / "ACU Online Courses" etc.  Used by both the
# pipe/dash/colon branch and the comma branch of _TITLE_SUFFIX below.
_INSTITUTION_TAIL = (
    # Full institutional name ending in a recognisable institution keyword.
    # Order matters inside the keyword group: longer words ("University")
    # must come before shorter prefixes ("Uni") so the regex engine consumes
    # them first and never strips just the "Uni" prefix from "University".
    r"[A-Z][A-Za-z& ]{1,40}\s+(?:University|Uni|College|Institute|Academy|School)\b|"
    # Explicit acronym/short-name list. Case-insensitive match handles
    # "Aibi" (title-case) and "AIBI" (all-caps) uniformly.
    # Orchestrator._strip_provider_name_from_title() is the second layer that
    # catches any short names not in this list using the actual uni_name.
    # NOTE: do NOT add "University of [Place]" patterns here.
    # Universities whose CMS appends their full "University of X" name should
    # declare it in their per-uni YAML under:
    #   extraction.course_name.strip_title_suffixes
    # Patching global code for a per-uni CMS quirk creates cross-university
    # regression risk.  The per-uni YAML is scoped, defaults to no-op, and
    # cannot affect other universities.
    r"USQ|CSU|UTS|ANU|UNSW|RMIT|MIT|KBS|AIBI|ACAP|AIT|ASA|VIT|QIBT|SAIBT|PIBT|ACU|"
    # Page-type qualifiers: "Online Courses", "ACU Online Courses", etc.
    # These appear as browser <title> suffixes on aggregator / online-study
    # portal pages and must be stripped to recover the bare course name.
    # Pattern: optional institution short-name (1-6 uppercase letters) followed
    # by "Online Courses" (singular or plural, with or without institution prefix).
    r"(?:[A-Z]{1,6}\s+)?Online\s+Courses?"
)
_TITLE_SUFFIX = re.compile(
    # Two separator branches:
    #   (a) pipe/dash/colon/bullet — single institution token after separator
    #   (b) comma — optional breadcrumb segments may sit between the course
    #       name and the trailing institution.  La Trobe (2026-05-13) emits
    #       "Master of Teaching Nexus (Secondary), Courses and degrees,
    #       La Trobe University" — the breadcrumb "Courses and degrees"
    #       must be consumed in the same .sub() pass as the institution
    #       suffix, otherwise the iterative loop in _clean strips only
    #       ", La Trobe University" and leaves ", Courses and degrees"
    #       behind (which doesn't end in an institution keyword and
    #       therefore can't be matched on the next iteration).
    #
    # Comma is safe to add as a separator because both branches REQUIRE
    # the tail to end with a recognised institution keyword — ordinary
    # commas in a course name (e.g. "Bachelor of Business, Commerce and
    # Economics") never end with "University" / "College" / "Institute"
    # so they cannot accidentally trigger truncation.
    r"\s*(?:"
    # (a) pipe / dash / colon / bullet
    r"[\|\-–—:•]\s*(?:" + _INSTITUTION_TAIL + r")"
    r"|"
    # (b) comma + optional breadcrumb segments + institution.
    # The breadcrumb capture `(?:[^,|\-–—:•\n]+,\s*)*` eats "<text>, "
    # tokens that contain no other separator, then the institution
    # tail closes out at end-of-string.
    r",\s*(?:[^,|\-–—:•\n]+,\s*)*(?:" + _INSTITUTION_TAIL + r")"
    r")\s*$",
    re.IGNORECASE,
)
# No-dash variant: same as _TITLE_SUFFIX but the leading separator class
# excludes dashes (-, –, —). Used when the title already contains a
# primary pipe/colon separator. In that case, any trailing "- <text
# ending in University/Institute/...>" is almost certainly part of the
# course's major / specialisation name (e.g. Curtin "Doctor of Philosophy
# - National Drug Research Institute | Curtin University") rather than a
# second institution suffix, so the dash branch must NOT fire and eat
# the major. The 2026-05-18 fix; see replit.md.
_TITLE_SUFFIX_NO_DASH = re.compile(
    r"\s*(?:"
    r"[\|:•]\s*(?:" + _INSTITUTION_TAIL + r")"
    r"|"
    r",\s*(?:[^,|\-–—:•\n]+,\s*)*(?:" + _INSTITUTION_TAIL + r")"
    r")\s*$",
    re.IGNORECASE,
)
_DEGREE_QUAL_IN_TITLE_RE = re.compile(
    r"^\s*(?:master|bachelor|graduate|diploma|certificate|doctor|phd|mba\b|msc\b|bsc\b|bed\b)",
    re.I,
)
_NON_COURSE_PREFIX = re.compile(
    r"^\s*(?:home|study|courses?|programs?)\s*[/>\\:|–-]\s*", re.I
)
# Issue 3a: AQF code prefixes on VIT/SMIC vocational course names.
# Pages set <h1>SIT40521 - Certificate IV in Kitchen Management</h1>
# or (SMIC Template A h3) "BSB60120 : Advanced Diploma of Business - CRICOS 106813D"
# or (SMIC Template B h1) "SIS30321 Certificate III in Fitness" (space separator only).
# The code (3 uppercase letters + 5 digits) must be stripped before
# _smart_case runs, otherwise it becomes "Sit40521 - " in the output.
# Pattern matches:
#   SIT40521 -   (dash, original case)
#   BSB60120 :   (colon, SMIC Template A)
#   SIS30321 C   (space + uppercase lookahead, SMIC Template B — no punctuation separator)
_AQF_PREFIX_RE = re.compile(
    r"^[A-Za-z]{3}\d{5}\s*(?:[-–—:]\s*|\s+(?=[A-Z]))"
)
# CRICOS codes appear as a trailing suffix on some Australian vocational
# course pages, e.g. "Advanced Diploma of Business - CRICOS 106813D".
# Strip them so the stage name is the clean course title.
_CRICOS_SUFFIX_RE = re.compile(r"\s*[-–—]\s*CRICOS\s+\w+\s*$", re.IGNORECASE)


_SLUG_LIKE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+){2,}$")


def _url_mba_spec_name(url: str) -> str | None:
    """Return 'MBA – Spec Name' when the URL is an MBA specialisation sub-page.

    Detects the pattern  /<catalogue>/<mba-parent>/<specialisation>
    e.g. /courses/mba-master-of-business-administration/women-in-leadership
    → 'MBA – Women in Leadership'

    Returns None when the URL does not match the pattern.
    """
    try:
        segs = [s for s in urlparse(url).path.lower().split("/") if s]
    except Exception:
        return None
    # Need at least  <catalogue> / <mba-parent> / <spec>
    if len(segs) < 3:
        return None
    catalogue, parent, spec = segs[-3], segs[-2], segs[-1]
    if catalogue not in _CATALOGUE_SEGS:
        return None
    if not parent.startswith("mba"):
        return None
    if spec.startswith("mba"):
        return None  # the spec slug itself begins with mba → it IS the main MBA page
    spec_name = _smart_case(_unslug(spec))
    return f"MBA \u2013 {spec_name}"


def _looks_like_slug(text: str) -> bool:
    """Detect URL-style slugs (``bachelor-of-business``).

    Triggered when the cleaned candidate is a single all-lowercase token
    with two or more hyphens — that pattern is unambiguous (real titles use
    spaces, not hyphens, between distinct words). One-hyphen survivors like
    ``co-op`` or ``part-time`` are deliberately *not* matched so we never
    mangle legitimate compound words.
    """
    return bool(_SLUG_LIKE.fullmatch(text.strip()))


def _unslug(text: str) -> str:
    """Replace hyphens with spaces so the slug can flow through ``_smart_case``."""
    return text.replace("-", " ")


def _smart_case(text: str) -> str:
    words = re.split(r"(\s+)", text.strip())
    out: list[str] = []
    for i, w in enumerate(words):
        if w.isspace() or not w:
            out.append(w)
            continue
        upper = w.upper().strip(",.;:()")
        bare = w.strip(",.;:()")
        canon = _ACRONYM_CANON.get(upper)
        if canon is not None:
            out.append(w.replace(bare, canon))
            continue
        lower = bare.lower()
        if i > 0 and lower in _PREPOSITIONS:
            out.append(w.replace(bare, lower))
            continue
        if bare and bare[0].isalpha():
            out.append(w.replace(bare, bare[0].upper() + bare[1:].lower()))
        else:
            out.append(w)
    return "".join(out)


def _clean(raw: str) -> str | None:
    if not raw:
        return None
    txt = re.sub(r"\s+", " ", raw).strip()
    # Issue 3a: strip AQF code prefix (e.g. "SIT40521 - ", "BSB60120 : ",
    # "SIS30321 Certificate…") before any other processing.
    txt = _AQF_PREFIX_RE.sub("", txt).strip()
    # Strip trailing CRICOS code (e.g. "- CRICOS 106813D") that some Australian
    # vocational colleges append to the course name in the h3 element.
    txt = _CRICOS_SUFFIX_RE.sub("", txt).strip()
    txt = _NON_COURSE_PREFIX.sub("", txt)
    # Apply suffix strip iteratively. Some universities (Federation) emit
    # browser <title> tags with the institution name DUPLICATED, e.g.
    # "Bachelor of IT (Business Analysis) | Federation University | Federation University".
    # _TITLE_SUFFIX is anchored at end-of-string so a single .sub() removes only
    # the trailing copy and leaves the inner one behind. Loop until stable
    # (capped at 5 iterations as a safety belt — real titles never carry more
    # than 2-3 stacked suffixes) so duplicated provider tags are fully cleaned.
    #
    # Regex choice: when the original title contains a primary pipe / colon
    # separator (the canonical page-title separator), suppress the dash branch
    # of the suffix regex. Otherwise titles like Curtin's "Doctor of Philosophy
    # - National Drug Research Institute | Curtin University" lose the major
    # on the second iteration ("- National Drug Research Institute" matches
    # the dash + institution-tail pattern even though Institute here is part
    # of the course's major name, not a second provider suffix). When no
    # pipe/colon is present, the dash branch is still needed for tails like
    # "Bachelor of Business - AIBI" / "- Charles Sturt University".
    pattern = _TITLE_SUFFIX_NO_DASH if any(c in txt for c in "|:•") else _TITLE_SUFFIX
    for _ in range(5):
        new = pattern.sub("", txt).strip(" -|·•")
        if new == txt:
            break
        txt = new
    if not txt or len(txt) < 3 or len(txt) > 200:
        return None
    # Slug like "bachelor-of-business" → "Bachelor of Business". Done before
    # ``_smart_case`` so the prepositions/acronym rules apply uniformly.
    if _looks_like_slug(txt):
        txt = _unslug(txt)
    return _smart_case(txt)


def _from_lancashire_banner(html: str) -> str | None:
    """University of Central Lancashire (UCLan) hero-banner structural extractor.

    Lancashire course pages split the degree title across two sibling elements
    inside ``div.hero-banner__title-and-tags``:

        <h1 class="hero-banner__title">Midwifery</h1>
        <div class="hero-banner__tags">
          <span class="hero-banner__tag">MSc/PGDip/PGCert</span>
        </div>

    The bare H1 carries only the plain subject name; the qualification lives in
    a ``span.hero-banner__tag`` sibling. Neither H1 extraction nor
    ``prefer_title_over_h1`` can recover the full name
    "MSc/PGDip/PGCert Midwifery". This pre-pass combines them.

    NOTE: ``_smart_case`` / ``_clean`` is intentionally skipped on the combined
    result.  Slash-separated qualifiers like "MSc/PGDip/PGCert" are treated as
    a single whitespace-delimited token by ``_smart_case`` and would be
    lowercased to "Msc/pgdip/pgcert".  The CMS already formats the
    qualification correctly so no case transformation is needed.

    The ``hero-banner__title`` / ``hero-banner__tag`` class names are
    Lancashire-specific (BEM component style) and are unlikely to collide with
    other universities, so this check is unconditional — it simply returns None
    when absent.
    """
    try:
        soup_local = BeautifulSoup(html, "html.parser")
        h1 = soup_local.select_one("h1.hero-banner__title")
    except Exception:  # noqa: BLE001
        return None
    if not h1:
        return None
    # Walk up to the title-and-tags container to find the sibling tag span.
    container = h1.find_parent(class_="hero-banner__title-and-tags")
    tag_span = (
        container.select_one("span.hero-banner__tag")
        if container
        else soup_local.select_one("span.hero-banner__tag")
    )
    h1_text = h1.get_text(" ", strip=True)
    if not h1_text:
        return None
    tag_text = tag_span.get_text(" ", strip=True) if tag_span else ""
    combined = f"{tag_text} {h1_text}".strip() if tag_text else h1_text
    return combined or None


def _from_ltu_banner(html: str) -> str | None:
    """Leeds Trinity University banner-title structural extractor.

    LTU course pages split the degree title across three sibling elements
    inside ``div.banner-title-box``:

        <div class="banner-title__lead">Undergraduate</div>
        <h1 class="banner-title__main">Nursing (Mental Health)</h1>
        <div class="banner-title__sub">BSc (Hons)</div>

    The bare ``<h1>`` (banner-title__main) only carries the plain subject
    name with no degree qualifier — this is what fed the earlier
    category-landing-page false-positive (see skip_degree_qualifier_check
    in leedstrinity_2220.yaml) and left the staged course_name missing the
    "BSc (Hons)" prefix entirely. The award text lives in a sibling
    ``banner-title__sub`` div, not in the H1 or the ``<title>`` tag, so
    neither the default h1 extraction nor prefer_title_over_h1 can recover
    it. This structural pre-pass combines sub + main into the full degree
    title (e.g. "BSc (Hons) Nursing (Mental Health)").

    The ``banner-title__*`` class names are LTU-specific (BEM-style, tied to
    their "lt-section-course-banner" component) and unlikely to collide with
    other universities, so this check is unconditional like the BCU panel
    pre-pass in degree_level.py — it simply returns None when absent.
    """
    main = soup_local = None
    try:
        soup_local = BeautifulSoup(html, "html.parser")
        main = soup_local.select_one("h1.banner-title__main")
    except Exception:  # noqa: BLE001
        return None
    if not main or not soup_local:
        return None
    box = main.find_parent(class_="banner-title-box") or main.find_parent()
    if not box:
        return None
    sub = box.select_one(".banner-title__sub")
    name_text = main.get_text(" ", strip=True)
    if not name_text:
        return None
    sub_text = sub.get_text(" ", strip=True) if sub else ""
    combined = f"{sub_text} {name_text}".strip() if sub_text else name_text
    return combined or None


async def extract(html: str, url: str) -> list[ExtractionResult]:  # noqa: ARG001
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[tuple[str, str, float]] = []

    # Lancashire hero-banner pre-pass: combines span.hero-banner__tag
    # ("MSc/PGDip/PGCert") + h1.hero-banner__title ("Midwifery") without
    # running through _smart_case (slashes break the acronym canon lookup).
    _lancashire_name = _from_lancashire_banner(html)
    if _lancashire_name:
        _raw = re.sub(r"\s+", " ", _lancashire_name).strip()
        if _raw and 3 <= len(_raw) <= 200:
            return [
                ExtractionResult(
                    field_key="course_name",
                    value=_raw,
                    normalized={"course_name": _raw},
                    confidence=0.95,
                    method="course_name.lancashire_banner",
                    snippet=_raw[:160],
                )
            ]

    _ltu_name = _from_ltu_banner(html)
    if _ltu_name:
        cleaned = _clean(_ltu_name)
        if cleaned:
            try:
                from app.services.scraper.course_name_cleaner import clean_course_name_with_config
                _cn_cleaned, _ = clean_course_name_with_config(cleaned)
                if _cn_cleaned != cleaned:
                    cleaned = _cn_cleaned
            except Exception:  # noqa: BLE001
                pass
            return [
                ExtractionResult(
                    field_key="course_name",
                    value=cleaned,
                    normalized={"course_name": cleaned},
                    confidence=0.95,
                    method="course_name.ltu_banner",
                    snippet=_ltu_name[:160],
                )
            ]

    # Per-uni YAML option: h1_css_selector — use a targeted CSS selector instead
    # of a bare soup.find("h1") when the page has multiple H1 elements and the
    # first is not the course title.  Canonical case: Lancaster University whose
    # cookie-consent modal injects <div id="biccy-prompt"><h1>Our use of
    # cookies</h1></div> before the main content, causing bare soup.find("h1")
    # to return the wrong element.  Falls back to soup.find("h1") if the selector
    # matches nothing so existing behaviour is preserved for all other unis.
    _h1_css: str | None = None
    _prefer_title = False
    try:
        from app.services.scraper.config.context import (  # noqa: PLC0415
            get_uni_config as _get_uni_config_cn,
        )
        _cn_cfg = _get_uni_config_cn()
        if _cn_cfg is not None and _cn_cfg.extraction is not None:
            _prefer_title = bool(
                getattr(_cn_cfg.extraction.course_name, "prefer_title_over_h1", False)
            )
            _h1_css = getattr(_cn_cfg.extraction.course_name, "h1_css_selector", None) or None
    except Exception:  # noqa: BLE001
        pass

    if _h1_css:
        h1 = soup.select_one(_h1_css) or soup.find("h1")
    else:
        h1 = soup.find("h1")
    if h1:
        candidates.append(("h1", h1.get_text(" ", strip=True), 0.9))
    title = soup.find("title")
    if title:
        candidates.append(("title", title.get_text(" ", strip=True), 0.6))

    # Per-uni YAML option: prefer_title_over_h1 — use the page <title> as the
    # primary name source for universities (e.g. Bath Spa) whose CMS places
    # only a bare subject name in H1 ("Business and Management") while the full
    # degree name ("Business and Management degree - BA (Hons)") appears only in
    # the <title>.  When True, swap title to front before any other promotion
    # logic runs.  strip_title_suffixes in YAML removes the provider suffix.

    if _prefer_title and len(candidates) == 2:
        # Promote title (conf 0.85) over H1 (conf 0.6) so the full degree name
        # from the <title> is returned instead of the bare subject H1.
        candidates = [("title", candidates[1][1], 0.85), ("h1", candidates[0][1], 0.6)]
    elif len(candidates) == 2:
        # When both H1 and title are found: if the title (after cleaning) starts
        # with a degree qualifier (e.g. "MBA – ...") but the H1 does not, the
        # page is a specialisation sub-page where JS adds the parent degree prefix
        # only to the <title> (e.g. KBS MBA specialisations). Promote the title
        # so the full name like "MBA – Tourism and Hospitality Leadership" is used
        # instead of the bare "Tourism and Hospitality Leadership" from the H1.
        h1_raw, title_raw = candidates[0][1], candidates[1][1]
        h1_clean = _clean(h1_raw) or ""
        title_clean = _clean(title_raw) or ""
        if (
            title_clean
            and _DEGREE_QUAL_IN_TITLE_RE.search(title_clean)
            and not _DEGREE_QUAL_IN_TITLE_RE.search(h1_clean)
        ):
            candidates = [("title", title_raw, 0.85), ("h1", h1_raw, 0.6)]

    # Pre-compute whether this URL is an MBA specialisation sub-page.
    # If yes, any candidate name that lacks the "MBA" prefix will be
    # upgraded to "MBA – <name>" (Playwright rewrites <title> dynamically
    # and drops the "MBA –" prefix, so we cannot rely on page content alone).
    mba_prefix_name = _url_mba_spec_name(url)

    # Per-university literal suffix strips defined in the uni's YAML under
    # extraction.course_name.strip_title_suffixes.  Applied to the RAW
    # candidate text before _clean() runs, so operators can neutralise CMS
    # provider-name appendages without touching global scraping code.
    _strip_suffixes: list[str] = []
    try:
        from app.services.scraper.config.context import get_uni_config
        _uni_cfg = get_uni_config()
        _strip_suffixes = _uni_cfg.extraction.course_name.strip_title_suffixes
    except Exception:
        pass

    for method, raw, conf in candidates:
        # Apply per-uni suffix strips before any regex cleaning.  CMS branding
        # varies in case and whitespace and can be appended more than once, so
        # match flexibly and repeat until the candidate is stable.
        _raw = raw
        while _raw and _strip_suffixes:
            _matched_suffix = False
            for _suffix in sorted(_strip_suffixes, key=len, reverse=True):
                _suffix_pattern = re.escape(_suffix.strip()).replace(r"\ ", r"\s+")
                _match = re.search(
                    rf"\s*{_suffix_pattern}\s*$",
                    _raw,
                    flags=re.IGNORECASE,
                )
                if _match and _match.start() > 0:
                    _raw = _raw[: _match.start()].rstrip()
                    _matched_suffix = True
                    break
            if not _matched_suffix:
                break
        cleaned = _clean(_raw)
        if cleaned:
            # If this is an MBA specialisation sub-page and the extracted
            # name is missing the degree prefix, add it now.
            if mba_prefix_name and not cleaned.upper().startswith("MBA"):
                cleaned = f"MBA \u2013 {cleaned}"
            # Universal course-name cleanup — strip university-name suffixes
            # using YAML aliases and separator patterns before staging.
            try:
                from app.services.scraper.course_name_cleaner import clean_course_name_with_config
                _cn_cleaned, _ = clean_course_name_with_config(cleaned)
                if _cn_cleaned != cleaned:
                    cleaned = _cn_cleaned
            except Exception:
                pass
            return [
                ExtractionResult(
                    field_key="course_name",
                    value=cleaned,
                    normalized={"course_name": cleaned},
                    confidence=conf,
                    method=f"course_name.{method}",
                    snippet=raw[:160],
                )
            ]

    # Last-resort: derive name purely from the URL slug (e.g. for JS-heavy
    # pages where both H1 and title are missing or empty after rendering).
    if mba_prefix_name:
        return [
            ExtractionResult(
                field_key="course_name",
                value=mba_prefix_name,
                normalized={"course_name": mba_prefix_name},
                confidence=0.5,
                method="course_name.url_mba_spec",
                snippet=url,
            )
        ]
    return []
