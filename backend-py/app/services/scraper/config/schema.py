"""Pydantic models for per-university scraper configuration.

Schema is split into two top-level sections as proposed:

  discovery:   Settings that are safe to replay against an unknown university
               during Tier-3 playbook matching (URL filters, sitemap options,
               subdomain probes).  These do not assume anything about the
               university's content structure.

  extraction:  Settings that are specific to how a known university structures
               its pages (fee pages, English requirements, text-cleaning
               patterns, filters).  These MUST NOT be replayed against unknown
               unis in Tier-3 because they encode knowledge about a specific
               site's layout.

This split enables the Week-3 tiered-fallback feature to load a known
university's ``discovery`` section and replay it against a new university
without accidentally importing extraction assumptions (e.g. a
``trust_vision_ocr: false`` override that was tuned to prevent ACAP-specific
hallucinations from polluting a brand-new university's scrape).
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


# ── Discovery ───────────────────────────────────────────────────────────────

class SearchStaxConfig(BaseModel):
    """Custom data-source provider: a SearchStax-hosted Solr course index.

    Some universities (e.g. University of Huddersfield) serve their course
    catalogue from a SearchStax Solr core — the same endpoint their live
    React SPA queries client-side.  When this block is present the
    orchestrator skips HTML discovery + per-course extraction entirely and
    builds fully-formed staged-course records straight from the Solr docs
    (see ``searchstax_hud.py``).  No page fetching is needed because the
    ``content`` field carries the full page text (IELTS, entry reqs, etc.).

    The ``token`` is the SearchStax read token the SPA ships to every
    browser, so it is not a server secret — but operators should rotate it
    periodically.  Prefer ``token_env`` to read it from an environment
    variable instead of committing it to YAML.
    """

    endpoint: str = Field(
        description="Full Solr select URL (e.g. '.../emselect').",
    )
    token: Optional[str] = Field(
        default=None,
        description=(
            "SearchStax read token used in the 'Authorization: Token <t>' "
            "header.  This is the public client token the SPA exposes; not a "
            "server secret.  Prefer token_env over hardcoding here."
        ),
    )
    token_env: Optional[str] = Field(
        default=None,
        description=(
            "Name of an environment variable to read the token from. When "
            "set and present, overrides the literal `token` field."
        ),
    )
    filter_query: str = Field(
        default="sectionType_s:course",
        description="Solr fq applied to restrict the result set to courses.",
    )
    page_size: int = Field(
        default=100,
        description="Number of Solr docs fetched per paginated request.",
    )
    max_courses: Optional[int] = Field(
        default=None,
        description="Optional cap on the number of course docs staged (debug).",
    )
    fee_year: int = Field(
        default=2025,
        description="Academic fee year written into every staged fee row.",
    )
    currency: str = Field(
        default="GBP",
        description="ISO currency code written into every staged fee row.",
    )
    central_fee_page: Optional[str] = Field(
        default=None,
        description=(
            "URL of the central fee schedule page used as the source_url for "
            "the international_fee evidence row (the fee is band-derived, not "
            "on the course page)."
        ),
    )


class ScrapyConfig(BaseModel):
    """Run a Scrapy spider for course URL discovery (or full extraction).

    The spider runs in a subprocess — isolating Scrapy's Twisted event loop
    from the asyncio/Celery stack.  Output items are written to a temp JSON
    lines file by Scrapy's built-in feed exporter, then read back by the
    bridge (``scrapy_bridge.py``) and fed into the normal staging pipeline.

    Spider files must live in ``backend-py/spiders/<spider>.py``.  Copy
    ``backend-py/spiders/template_spider.py`` as a starting point.

    Minimum item shape (discovery-only mode)::

        {"name": "Course Name", "url": "https://..."}

    Rich mode (bypasses per-course extraction, like SearchStax)::

        {"name": "...", "url": "...", "payload": {...}, "evidence": [...]}

    ``payload`` keys match the ``scraped_courses`` staging columns.
    """

    spider: str = Field(
        description=(
            "Spider filename without the .py extension. "
            "File must exist at backend-py/spiders/<spider>.py."
        ),
    )
    settings: dict = Field(
        default_factory=dict,
        description=(
            "Scrapy SETTINGS overrides passed via -s KEY=VALUE on the CLI. "
            "E.g. {'DOWNLOAD_DELAY': 1, 'CONCURRENT_REQUESTS': 4}."
        ),
    )
    max_courses: Optional[int] = Field(
        default=None,
        description="Optional cap on returned links (useful for debug runs).",
    )
    timeout_seconds: int = Field(
        default=600,
        description="Maximum seconds the spider subprocess may run before it is killed.",
    )


class DiscoveryConfig(BaseModel):
    """Safe to replay against unknown universities (Tier-3 playbook matching)."""

    searchstax: Optional[SearchStaxConfig] = Field(
        default=None,
        description=(
            "When present, route discovery + extraction through the SearchStax "
            "Solr provider instead of HTML crawling. See SearchStaxConfig."
        ),
    )
    scrapy: Optional[ScrapyConfig] = Field(
        default=None,
        description=(
            "When present, run the named Scrapy spider for discovery. "
            "Spider output feeds the normal staging pipeline. "
            "Falls through to BFS/sitemap if the spider returns 0 links. "
            "See ScrapyConfig and backend-py/spiders/template_spider.py."
        ),
    )
    fallback_subdomains: list[str] = Field(
        default_factory=list,
        description=(
            "Additional subdomains to probe when the primary URL yields <5 candidates. "
            "E.g. ['handbook.{domain}', 'courses.{domain}', 'international.{domain}']."
        ),
    )
    always_sitemap_supplement: bool = Field(
        default=False,
        description=(
            "Always merge sitemap results with BFS candidates even when BFS exceeded "
            "the fallback threshold.  Needed for JS-rendered SPAs (Torrens, CDU) and "
            "deep-faculty sites where BFS burns its page budget on info pages (AUT, ACU)."
        ),
    )
    block_url_patterns: list[str] = Field(
        default_factory=list,
        description=(
            "Regex patterns.  Any discovered URL matching one of these is dropped "
            "before extraction.  E.g. '/handbook/handbook-20' blocks old ACU handbooks."
        ),
    )
    allow_url_patterns: list[str] = Field(
        default_factory=list,
        description=(
            "Regex patterns (whitelist).  If non-empty, only URLs matching at least "
            "one pattern are kept.  Empty list = allow everything."
        ),
    )
    sitemap_url: Optional[str] = Field(
        default=None,
        description="Explicit sitemap URL.  Overrides the auto-detected sitemap.",
    )
    use_wayback: bool = Field(
        default=False,
        description="Fall back to Wayback Machine CDX when all other discovery fails.",
    )
    skip_browser_discovery: bool = Field(
        default=False,
        description=(
            "Skip the generic Playwright browser-discovery fallback entirely, "
            "even when BFS returns 0 links.  Enable for universities whose live "
            "site is Cloudflare Enterprise-walled at the datacenter IP level so "
            "the browser ALSO receives 403 / 'Just a moment…' — the 90-second "
            "discovery timeout fires with zero results and only wastes time.  "
            "JCU is the canonical example: set skip_browser_discovery=true and "
            "use_wayback=true so the orchestrator goes BFS → 0 → Wayback CDX "
            "instead of BFS → 0 → 90s browser hang → Wayback CDX."
        ),
    )
    bfs_page_budget: Optional[int] = Field(
        default=None,
        description=(
            "Override the default BFS page budget (12 fast / 25 full).  "
            "Raise for sites with many listing pages (e.g. UOW ~62 pages)."
        ),
    )
    max_candidates: Optional[int] = Field(
        default=None,
        description=(
            "Override the default candidate cap (20 fast / 200 full).  Raise "
            "when a sitemap publishes more allow_url_patterns-matching URLs "
            "than the default cap can hold — without this, late-listed "
            "courses get truncated (e.g. CQU sitemap has 199 unique HE "
            "courses but BFS already filled 54 slots, so 53 of the late "
            "alphabet — including cv82 master-of-engineering at index 168 "
            "of the allow-filtered sitemap — were dropped at the 200 cap)."
        ),
    )
    always_browser_discover: bool = Field(
        default=False,
        description=(
            "Run Playwright browser-based discovery in ADDITION to BFS (merging "
            "results) rather than only as a zero-result fallback.  Enable for "
            "Cloudflare-protected sites where BFS succeeds on HTTP-accessible "
            "faculties but silently misses faculties that return 403 for plain "
            "HTTP (e.g. UTAS arts-soc which returns 403 for curl but loads fine "
            "in a real browser).  Host-specific seed URLs in "
            "_HOST_EXTRA_SEEDS (browser_discover_generic.py) are consumed "
            "during this pass to sweep the full faculty A-Z catalogue."
        ),
    )
    seed_urls: list[str] = Field(
        default_factory=list,
        description=(
            "Course-listing page URLs injected into the browser BFS queue with "
            "the highest priority (+300 score bonus) BEFORE any generic nav "
            "crawl begins.  Use when you know the exact listing pages (e.g. "
            "/study/undergraduate/courses, /study/postgraduate/courses) and "
            "want the crawler to visit them first rather than guessing from the "
            "homepage.  Unlike extra_course_urls these are LISTING pages — the "
            "crawler follows links from them to reach individual course pages.  "
            "If enough course links are found from seeds, weak generic BFS pages "
            "are skipped automatically."
        ),
    )
    extra_course_urls: list[str] = Field(
        default_factory=list,
        description=(
            "Explicit course page URLs injected directly into the discovered "
            "link set after all discovery tiers complete.  Bypasses BFS, "
            "sitemap, browser-discover, and Wayback entirely.  Use as a "
            "surgical fallback for known-CRICOS courses that all discovery "
            "tiers consistently miss (e.g. a single URL that lives behind "
            "Cloudflare and is not reachable by any crawler)."
        ),
    )
    use_stealth_browser: bool = Field(
        default=False,
        description=(
            "Route browser discovery AND per-course HTML fetches through the "
            "patchright + Xvfb stealth stack (see stealth_browser.py).  Enable "
            "ONLY for hosts where regular headless Playwright fails the "
            "Cloudflare challenge with HTTP 403 / 'Just a moment...' (verified "
            "for Macquarie/www.mq.edu.au on 2026-05-25).  Stealth adds ~2-4s "
            "per page (Xvfb display + persistent-context launch) so do NOT "
            "enable fleet-wide.  Requires the `patchright` python package and "
            "the `xorg.xvfb` system dependency."
        ),
    )
    must_contain: list[str] = Field(
        default_factory=list,
        description=(
            "Substring whitelist (case-insensitive) applied AFTER block_url_patterns. "
            "If non-empty, any candidate URL that does not contain at least one of "
            "these substrings is dropped. Simpler than allow_url_patterns (no regex) "
            "for the common case where you just want '/courses/' or '/study/' in "
            "the path. Empty list (default) = no-op. Per-uni opt-in via YAML."
        ),
    )
    scrape_do_fallback: bool = Field(
        default=False,
        description=(
            "When True, route blocked page fetches through scrape.do after "
            "curl_cffi fails but before Wayback Machine.  scrape.do proxies "
            "the request through a residential IP so Cloudflare WAF + IP-level "
            "blocks are bypassed.  Costs API credits, so enable ONLY for "
            "universities where httpx + curl_cffi are both blocked.  Requires "
            "the SCRAPE_DO_TOKEN env variable/secret.  Default: False."
        ),
    )
    browser_time_budget_s: int = Field(
        default=90,
        description=(
            "Hard wall-clock time limit (seconds) for the browser-based discovery "
            "pass.  When elapsed time exceeds this value the nav BFS loop stops "
            "and returns whatever course links have been found so far.  Default 90 s. "
            "Raise for sites with many listing pages; lower for sites with fast "
            "JS hydration that rarely need more than a handful of nav visits."
        ),
    )
    browser_early_stop_courses: int = Field(
        default=100,
        description=(
            "Stop following nav pages once this many course links have been found. "
            "Avoids spending minutes on low-value navigation after a catalogue-size "
            "listing page has already been harvested.  Default 100.  Set higher "
            "for large universities with 200+ courses split across many listing pages."
        ),
    )
    block_nav_patterns: list[str] = Field(
        default_factory=list,
        description=(
            "Substring patterns (case-insensitive).  Any nav-candidate URL whose "
            "path contains one of these strings is discarded without visiting.  "
            "Extends the global blocklist of low-value paths (apprenticeships, fees, "
            "news, events, accommodation, etc.).  E.g. ['/open-evenings', '/scholarships']."
        ),
    )
    expected_min_courses: Optional[int] = Field(
        default=None,
        description=(
            "Minimum number of courses expected from discovery.  When set, the "
            "orchestrator emits a WARNING if the discovered count falls below this "
            "threshold (e.g. 'Discovery incomplete: expected 100+, found 12').  "
            "Does not block the job — use it as an early-warning signal that "
            "seed_urls or config changes are needed."
        ),
    )


# ── Extraction sub-configs ───────────────────────────────────────────────────

class FeesConfig(BaseModel):
    central_page: Optional[str] = Field(
        default=None,
        description="URL of the university-wide fee schedule page.",
    )
    fees_pdf_url: Optional[str] = Field(
        default=None,
        description="URL of the university-wide fee schedule PDF.",
    )
    force_central_fee_stage: bool = Field(
        default=False,
        description=(
            "When true, sets has_central_fee_page=True in every course payload "
            "so courses without an individual fee listing still pass the "
            "no_international_fee staging gate and land in the review queue. "
            "Use for universities (e.g. UTAS) that publish fees on a separate "
            "central schedule rather than on each course page."
        ),
    )
    default_currency: str = Field(
        default="AUD",
        description="ISO currency code used when no currency marker is found on the page.",
    )
    credit_points_per_unit: Optional[int] = Field(
        default=None,
        description=(
            "Number of credit points per unit of study.  When set, per-unit fees are "
            "multiplied by this value to derive the full-course fee.  "
            "None = use the extracted credit-point count from the page."
        ),
    )
    course_pdf_aliases: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Per-uni alias map: DB course name (lower-cased) → the course "
            "name as it actually appears in the fee schedule PDF. Used by "
            "match_course_in_pdf_table to augment / replace the DB course's "
            "token bag so fuzzy matching can clear the ≥2-distinctive-token "
            "floor when the PDF row carries a qualifier (e.g. "
            "'Master of Design (Non-Cognate)') the DB course name lacks. "
            "Empty by default — opt-in per university only. "
            "Match is case-insensitive on the key."
        ),
    )
    pdf_row_pattern: Optional[str] = Field(
        default=None,
        description=(
            "Per-uni regex override for the per-course PDF data row. "
            "When None (default), _pick_per_course_amounts uses the shared "
            "_PDF_DATA_ROW_RE pattern (CRICOS + duration + units + 3 dollar "
            "amounts — Torrens / ASA layout). Set this when a university's "
            "fee PDF uses a fundamentally different column layout (e.g. "
            "Federation: CRICOS + 2 dollar amounts, no per-unit / total). "
            "Pattern MUST define a named group ``cricos``; ``per_unit``, "
            "``annual``, ``total`` are looked up via ``groupdict().get()`` "
            "so missing groups are tolerated. Compiled with re.I."
        ),
    )
    pdf_fee_term: Optional[str] = Field(
        default=None,
        description=(
            "Per-uni override for the fee_term emitted by per-course PDF "
            "rows. When None (default), the term is auto-derived from the "
            "annual vs total comparison. Set to ``\"Annual\"`` for PDFs "
            "(e.g. Federation) whose row only carries an annual figure, or "
            "``\"Full Course\"`` for whole-of-programme schedules."
        ),
    )
    prefer_annual_over_total: bool = Field(
        default=False,
        description=(
            "When the per-course PDF row exposes BOTH an annual figure and "
            "a total-course figure (e.g. Torrens: $31,600 annual / $94,800 "
            "total over 3 years), store the annual value as "
            "``international_fee`` with ``fee_term=\"Annual\"`` instead of "
            "the total / ``\"Full Course\"`` default. The default (False) "
            "preserves the original behaviour of preferring the total "
            "course tuition. Enable per-uni only when annual fees are the "
            "value the operator wants displayed in the catalogue."
        ),
    )
    prefer_year_one_over_total: bool = Field(
        default=False,
        description=(
            "When the per-course PAGE publishes BOTH a 'Year 1 fee' "
            "(e.g. 'Indicative year 1 fee $47,116') and a 'Total course "
            "fee' (e.g. 'Total indicative course fee $141,348') label, "
            "store the year-1 amount as ``international_fee`` with "
            "``fee_term=\"Annual\"`` instead of the total / "
            "``\"Full Course\"`` default. The default (False) preserves "
            "the original page-regex preference for the larger total. "
            "Enable per-uni only when the year-1 figure is the value the "
            "operator wants displayed in the catalogue (e.g. Curtin "
            "publishes 3-year Bachelor degrees with both labels and the "
            "operator-facing column reads ``$X/Annual`` not ``$X/Full "
            "Course``). Independent of ``prefer_annual_over_total`` which "
            "covers the PDF-row path; this knob covers the page-regex "
            "extractor in ``extractors/fee.py``."
        ),
    )
    pdf_overrides_page_regex: bool = Field(
        default=False,
        description=(
            "When True, a successful per-course PDF row match OVERWRITES "
            "any existing payload values for ``international_fee``, "
            "``currency``, ``fee_term`` and ``fee_year`` even if those "
            "slots were already filled by an earlier extractor (page "
            "regex / Gemini prose / sibling cache). The default (False) "
            "keeps the historical course-page-wins behaviour where the "
            "PDF only fills empty slots. Enable for universities whose "
            "per-course HTML pages contain marketing prose with stale or "
            "non-tuition dollar amounts (e.g. Torrens: ``\"will cost an "
            "international student $82,800\"`` for a course whose "
            "official 2026 PDF schedule lists $31,600/Annual). Treats "
            "the central PDF schedule as the authoritative tuition "
            "source for the affected uni."
        ),
    )
    pdf_parser: Optional[str] = Field(
        default=None,
        description=(
            "Per-uni override for the per-course PDF parser strategy. "
            "When None (default), the legacy regex-based ``_pick_per_course_amounts`` "
            "is used over pypdf-extracted text — works well for PDFs whose course "
            "names fit on the same line as the CRICOS+fee data. Set to "
            "``\"columnar\"`` to switch to ``_pick_per_course_amounts_columnar``, "
            "which uses ``pdftotext -layout`` (poppler) and a CRICOS-anchored, "
            "column-position-aware row parser. The columnar parser correctly "
            "handles fee schedules where course titles wrap across 2-3 lines "
            "(e.g. Torrens: ``Diploma of Branded`` / ``Fashion Design`` on "
            "separate lines) — pypdf flattens these and the legacy line-anchored "
            "regex misses them entirely. Requires ``pdftotext`` to be on PATH; "
            "falls back to legacy on subprocess failure."
        ),
    )
    # ── Fee source preference / rejection ────────────────────────────────────
    reject_keywords: list[str] = Field(
        default_factory=list,
        description=(
            "Case-insensitive keywords that, when present in the text surrounding "
            "an extracted fee amount, indicate the fee is a *domestic* or "
            "CSP/HECS fee that must NOT be published as an international fee. "
            "Typical values: ``[\"Commonwealth Supported\", \"CSP\", \"HECS\", "
            "\"Domestic\", \"HECS-HELP\", \"Indicative domestic\"]``. "
            "When the winning fee amount's snippet matches ANY keyword, the fee "
            "is discarded and ``international_fee`` is left null (→ DQ warning). "
            "Empty by default — opt-in per university."
        ),
    )
    prefer_international: bool = Field(
        default=False,
        description=(
            "When True, if both an international fee AND a domestic/CSP fee were "
            "found on the same page, the pipeline always selects the international "
            "one even if the domestic value was extracted first or has higher "
            "confidence.  Pairs with ``reject_keywords`` to handle universities "
            "that publish domestic fees first and international fees below (e.g. "
            "JCU tab pages, Newcastle right-sidebar, Murdoch toggle). "
            "Default False preserves the existing 'highest confidence wins' logic."
        ),
    )
    follow_links: list[str] = Field(
        default_factory=list,
        description=(
            "When ``international_fee`` is still blank after all extractors and "
            "after fee rejection, scan the course HTML for ``<a>`` elements whose "
            "link text matches any of these phrases (case-insensitive), fetch the "
            "linked page, and re-run the fee extractor.  Mirrors the "
            "``extraction.english.follow_links`` mechanism.  Any matched fee is "
            "also filtered through ``reject_keywords`` so domestic/CSP amounts on "
            "the linked page are discarded automatically.  "
            "Typical values for JCU: "
            "``['fees and scholarships', 'international student fees', "
            "'fees for your course']``.  Empty by default — opt-in per university."
        ),
    )


class BandSpec(BaseModel):
    """Score values for one named English-requirement band (e.g. 'Band 2')."""

    ielts_overall: Optional[float] = Field(
        default=None,
        description="IELTS overall band score for this band level.",
    )
    ielts_each: Optional[float] = Field(
        default=None,
        description=(
            "Minimum score for each IELTS component (listening, reading, "
            "speaking, writing).  Stored in all four ielts_* per-band fields."
        ),
    )
    pte_overall: Optional[int] = Field(
        default=None,
        description="PTE Academic overall score for this band level.",
    )
    toefl_overall: Optional[int] = Field(
        default=None,
        description="TOEFL iBT overall score for this band level.",
    )


class EnglishConfig(BaseModel):
    central_page: Optional[str] = Field(
        default=None,
        description="URL of the university-wide English requirements page.",
    )
    requirements_pdf_url: Optional[str] = Field(
        default=None,
        description="URL of the English requirements PDF.",
    )
    trust_vision_ocr: bool = Field(
        default=True,
        description=(
            "Set to false for universities where Gemini vision consistently "
            "hallucinates IELTS/PTE scores from images (e.g. ACAP).  "
            "Disabling falls back to HTML extraction only."
        ),
    )
    trust_tier1_vision_ocr_english: bool = Field(
        default=False,
        description=(
            "Week 1 Prompt 7 Part B — default is now FALSE globally.  Tier-1 "
            "vision OCR (images NOT anchored inside an English / Entry "
            "Requirements DOM section) is treated as 'tier 5' evidence: the "
            "OCR still runs and is logged, but extracted values do not "
            "contribute to row finalisation.  Only tier-0 images (DOM-anchored) "
            "are promoted to tier-4 evidence and may write to the payload.  "
            "Set to true on a per-uni basis only for universities whose "
            "requirements live exclusively inside images that the DOM-section "
            "detector misses (e.g. ASAHE), and only after manual spot-check."
        ),
    )
    default_ielts: Optional[float] = Field(
        default=None,
        description=(
            "Institutional IELTS default to apply when no per-course value is found. "
            "Only set when the university publicly states a single entry standard."
        ),
    )
    default_pte: Optional[int] = Field(
        default=None,
        description="Institutional PTE Academic default (same conditions as default_ielts).",
    )
    default_toefl: Optional[int] = Field(
        default=None,
        description="Institutional TOEFL iBT default.",
    )
    test_blocklist: list[str] = Field(
        default_factory=list,
        description=(
            "Per-uni list of English-test names to drop from extracted results "
            "(case-insensitive match on test name: ielts / pte / toefl / "
            "cambridge / duolingo / kite). Use when a university page "
            "incidentally mentions a test it does not actually accept (e.g. a "
            "marketing page links 'KITE' as a competitor product, polluting "
            "the extracted English requirements). Empty by default."
        ),
    )
    follow_links: list[str] = Field(
        default_factory=list,
        description=(
            "Link-text patterns (case-insensitive, substring match) that the "
            "scraper should follow when IELTS / PTE / TOEFL scores are missing "
            "after the main course-page extraction.  The scraper looks for "
            "<a> elements whose visible text contains any listed phrase, fetches "
            "the linked page, and re-runs the English extractor against it. "
            "Typical values: ``[\"English Language Requirements\", "
            "\"Minimum English Requirements\", \"Entry Requirements\"]``. "
            "Fetched pages are added to the evidence store so the review panel "
            "shows the source URL. Empty by default — opt-in per university."
        ),
    )
    band_mapping: dict[str, BandSpec] = Field(
        default_factory=dict,
        description=(
            "Named band → score mapping for universities that publish English "
            "requirements as labelled bands rather than direct IELTS scores "
            "(e.g. JCU uses 'Band 1', 'Band 2', 'Band 3'). "
            "Keys are the exact band labels as they appear on the course page "
            "(case-insensitive match at runtime). "
            "Values are :class:`BandSpec` dicts with ``ielts_overall``, "
            "``ielts_each``, ``pte_overall``, ``toefl_overall``. "
            "When a course page contains a recognised band label and IELTS is "
            "still blank after all other extractors have run, the mapped scores "
            "are applied with ``method='yaml_band_mapping'``. "
            "Empty by default — opt-in per university."
        ),
    )
    band_reference_url: Optional[str] = Field(
        default=None,
        description=(
            "URL of the page that defines the band-to-score mapping "
            "(e.g. the university's admissions policy schedule).  Stored as "
            "``source_url`` in the evidence row so reviewers can verify the "
            "mapping without a code search."
        ),
    )


class DomesticOnlyFilter(BaseModel):
    enabled: bool = Field(
        default=False,
        description=(
            "When true, courses detected as domestic-only are dropped during staging. "
            "Enable for universities whose listing includes non-international courses "
            "without marking them as such (e.g. ACAP)."
        ),
    )


class OnlineOnlyFilter(BaseModel):
    enabled: bool = Field(
        default=True,
        description=(
            "When true, courses with all-online delivery are dropped during "
            "staging.  Default true mirrors the historical hard-coded reject "
            "in guards.should_stage_course.  Distance-education-heavy unis "
            "(e.g. CSU, OUA) opt out by setting this to false in their "
            "per-uni YAML."
        ),
    )


class IntakeConfig(BaseModel):
    rolling_enrollment_label: Optional[str] = Field(
        default=None,
        description=(
            "When set AND the page text contains any of "
            "rolling_enrollment_markers AND no intake months were "
            "extracted by any other path, write [label] into "
            "intake_months. Used for research degrees (PhD / MPhil) "
            "where the university accepts continuous enrolment "
            "rather than fixed semester intakes — e.g. Curtin's "
            "'Enrolment shall be continuous' wording. Off by default; "
            "opt-in per uni so undergraduate/postgrad pages without "
            "an extracted intake still raise the no_intake_months "
            "warning."
        ),
    )
    rolling_enrollment_markers: list[str] = Field(
        default_factory=list,
        description=(
            "Lower-cased substrings searched in the page body to "
            "trigger the rolling_enrollment_label fallback. Match is "
            "case-insensitive substring (no regex). Empty by default."
        ),
    )


class FiltersConfig(BaseModel):
    domestic_only: DomesticOnlyFilter = Field(
        default_factory=DomesticOnlyFilter,
    )
    online_only: OnlineOnlyFilter = Field(
        default_factory=OnlineOnlyFilter,
    )
    broken_cms_retry_strip_query: bool = Field(
        default=False,
        description=(
            "When the global broken-CMS short-circuit fires AND the URL "
            "carries a query string, retry the bare URL once before "
            "giving up. Use only when the uni serves a real, fully-"
            "international-eligible page at the bare URL but a "
            "YAML-driven query rewrite (e.g. CQU "
            "?audience=INTERNATIONAL on most Bachelors and Masters) "
            "returns a 200-OK 137-char branded error template. Off by "
            "default — preserves the strict skip-on-broken-CMS "
            "behaviour for every other uni."
        ),
    )


class LocationCleaningConfig(BaseModel):
    strip_patterns: list[str] = Field(
        default_factory=list,
        description=(
            "Regex patterns applied to raw location strings before parsing. "
            "Each matching fragment is stripped.  Order matters — patterns are "
            "applied left to right.  "
            "E.g. ACAP: [r'\\^\\s*\\^.*$'] strips '^ ^Available in Perth' cruft."
        ),
    )


class DurationCleaningConfig(BaseModel):
    split_on_slash: bool = Field(
        default=False,
        description=(
            "Split raw duration strings on '/' before parsing.  Needed for KBS/Torrens "
            "compound patterns like 'X years / Y subjects / Z trimesters'."
        ),
    )
    reject_sentence_patterns: list[str] = Field(
        default_factory=list,
        description=(
            "Regex patterns (case-insensitive) applied to each candidate sentence "
            "before it enters the duration tournament.  Any sentence matching one "
            "of these patterns is skipped entirely — it contributes no candidates. "
            "Also applied to the raw value text of any structural DOM match "
            "(strong/dt/th) so that a labeled 'Duration' cell whose value reads "
            "'up to N years' (a max-completion-time phrase) is not returned early "
            "as the program duration.\n\n"
            "Use when a university's pages expose maximum-completion-time or "
            "candidature-cap text using phrasing that the global "
            "_DURATION_RESEARCH_CAP_RE and _DURATION_ANTI_CONTEXT filters don't "
            "catch — e.g. 'up to 10 years to complete' or 'up to 35 months'.\n\n"
            "Patterns are compiled with re.IGNORECASE.  Empty by default — no "
            "sentences are rejected.  Do NOT replicate global anti-context patterns "
            "here; only add patterns that are specific to this university's CMS "
            "phrasing and would create false positives on other universities."
        ),
    )


class FieldOverride(BaseModel):
    """Hard-set a payload field when a course URL matches a regex.

    Use as a surgical last-mile fix when an extractor consistently writes
    a wrong value for a small set of pages and per-uni tweaks to the
    extractor are not justified. E.g. a single department page whose
    ``course_location`` always comes out wrong.
    """

    url_regex: str = Field(
        description="Regex applied (case-insensitive) to the course URL. Must match for the override to fire.",
    )
    field: str = Field(
        description="Payload key to overwrite (e.g. 'course_location', 'study_mode', 'duration_term').",
    )
    value: Optional[str] = Field(
        default=None,
        description="Value to write. Use null/empty to clear the field instead.",
    )


class UrlRewrite(BaseModel):
    """Per-host URL rewriting applied right before each course page is fetched.

    Many universities gate the international-student view (fees, IELTS,
    intakes, campus list) behind a query flag like ``?international=true``
    or ``?audience=INTERNATIONAL``. Without that flag the page renders the
    domestic CSP view and every fee comes out at the domestic price.

    Each entry matches a request when ``host`` matches the URL netloc
    (case-insensitive; bare and ``www.`` variants both match) AND, if
    ``path_contains`` is set, the URL path includes that substring. When
    a rewrite fires it parses ``append_query`` as a query string and adds
    each key only if NOT already present (idempotent).
    """

    host: str = Field(
        description=(
            "Hostname this rewrite applies to (e.g. 'www.cqu.edu.au'). "
            "The bare apex form (cqu.edu.au) is also matched automatically."
        ),
    )
    path_contains: Optional[str] = Field(
        default=None,
        description=(
            "Optional substring the URL path must contain for the rewrite to "
            "fire (e.g. '/courses/'). Useful when a uni mixes course and "
            "non-course pages on the same host. None = no path filter."
        ),
    )
    append_query: str = Field(
        description=(
            "Query string to merge into the URL (e.g. 'audience=INTERNATIONAL' "
            "or 'students=international&year=2026'). Each key is only added "
            "if it is not already present in the URL."
        ),
    )


class CourseNameConfig(BaseModel):
    strip_title_suffixes: list[str] = Field(
        default_factory=list,
        description=(
            "Literal substrings stripped from the raw H1/title text before the "
            "standard suffix-detection regex runs.  Use when a university's CMS "
            "appends a fixed provider string that the generic regex cannot match "
            "automatically.  Matching is case-sensitive and checked from the END "
            "of the raw text.  "
            "Example (UWA): [' : the University of Western Australia']"
        ),
    )
    university_aliases: list[str] = Field(
        default_factory=list,
        description=(
            "University name aliases that must be stripped from scraped course "
            "titles.  The full university name from the DB is always tried; "
            "these aliases add extra patterns (abbreviations, brand variants). "
            "Example (UEL): ['University of East London', 'UEL']. "
            "All aliases are matched case-insensitively against the separators "
            "' | ', ' - ', ' – ', ' — ', ' at ', ' @ ', ' : ' at the END of "
            "the course name."
        ),
    )
    remove_separator_suffix: list[str] = Field(
        default_factory=list,
        description=(
            "Additional separator strings (e.g. ['|', ' - ', ' at ']) checked "
            "before the default separator set.  Rarely needed — the default set "
            "already covers pipe, dash, en/em-dash, colon, 'at', '@', bullet. "
            "Provided for edge-case CMS variants."
        ),
    )


class TextCleaningConfig(BaseModel):
    location: LocationCleaningConfig = Field(
        default_factory=LocationCleaningConfig,
    )
    duration: DurationCleaningConfig = Field(
        default_factory=DurationCleaningConfig,
    )
    global_substring_blocklist: list[str] = Field(
        default_factory=list,
        description=(
            "Substrings (case-insensitive) stripped from EVERY string field on "
            "the staged payload immediately before DB write. Useful for stock "
            "boilerplate that pollutes multiple fields (e.g. 'Apply Now', "
            "'Find out more', a stray cookie banner fragment). Whitespace is "
            "collapsed after stripping. Empty by default."
        ),
    )
    field_overrides: list[FieldOverride] = Field(
        default_factory=list,
        description=(
            "List of per-URL hard overrides. Each entry has url_regex, field, "
            "and value. When the course URL matches the regex, the named "
            "payload field is set to the supplied value (overwriting whatever "
            "extractors wrote). Apply sparingly — extractor / YAML fixes are "
            "preferred. Empty by default."
        ),
    )


class StagingConfig(BaseModel):
    reject_if_missing: list[str] = Field(
        default_factory=lambda: ["course_name"],
        description=(
            "Fields that must be non-null/non-empty for a staged course to be accepted. "
            "A course missing any of these fields is rejected at the staging gate."
        ),
    )


# ── Top-level ExtractionConfig ───────────────────────────────────────────────

class ExtractionConfig(BaseModel):
    """Per-university only.  Must NOT be replayed against unknown unis in Tier-3."""

    fees: FeesConfig = Field(default_factory=FeesConfig)
    english: EnglishConfig = Field(default_factory=EnglishConfig)
    intake: IntakeConfig = Field(default_factory=IntakeConfig)
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    text_cleaning: TextCleaningConfig = Field(default_factory=TextCleaningConfig)
    course_name: CourseNameConfig = Field(default_factory=CourseNameConfig)
    staging: StagingConfig = Field(default_factory=StagingConfig)
    url_rewrites: list[UrlRewrite] = Field(
        default_factory=list,
        description=(
            "Per-host query-param rewrites applied to every course URL "
            "before fetch. Used to switch a site into its international-"
            "student view (e.g. ?audience=INTERNATIONAL on CQU). Empty "
            "by default."
        ),
    )
    default_course_location: Optional[str] = Field(
        default=None,
        description=(
            "Fallback value written to course_location when every extractor "
            "(regex, browser, Gemini, PDF) returns an empty string.  Useful "
            "for universities whose Cloudflare-protected pages occasionally "
            "deliver partial HTML that omits the Location panel — UTAS is the "
            "canonical example.  Set to the primary campus city/name (e.g. "
            "'Hobart') so that the online-only staging guard does not reject "
            "legitimate on-campus courses whose location text was simply "
            "missing from the crawled HTML."
        ),
    )
    campus_allowlist: list[str] = Field(
        default_factory=list,
        description=(
            "When non-empty, any staged course whose course_location does NOT "
            "contain at least one of these strings (case-insensitive) is flagged "
            "as a critical data-quality failure. Useful for universities with a "
            "fixed set of known campuses (e.g. JCU: Townsville, Cairns, Brisbane, "
            "Singapore). Leave empty (default) to disable allowlist checking."
        ),
    )
    prefer_blended_over_on_campus: bool = Field(
        default=False,
        description=(
            "When True, and the study-mode rule extractor emitted 'Online' at low "
            "confidence (≤0.50) AND a physical campus was also found in "
            "course_location, the pipeline sets study_mode='Blended' instead of "
            "the default 'On Campus'.  Use for universities that genuinely offer "
            "courses both online AND on-campus (e.g. JCU: 'Online: Jan, May' AND "
            "'Townsville: Jan, May' on the same page).  Default False preserves "
            "the original Bug-1 fix behaviour (low-conf online + campus → On Campus)."
        ),
    )
    skip_browser_rescue: bool = Field(
        default=False,
        description=(
            "When True, skip the sparse-static rescue browser refetch even "
            "when both international_fee and duration are blank after the "
            "Gemini-primary pass.  Enable for universities whose live site is "
            "protected by Cloudflare Enterprise — Playwright is also IP-blocked "
            "and the browser rescue wastes 10-30 s per course returning "
            "rendered=0B.  Notre Dame is the canonical example: the rescue "
            "adds ~30 s latency per course with zero benefit, inflating a "
            "210-course job from ~6 minutes to 106 minutes."
        ),
    )
    max_parallel_fetch: Optional[int] = Field(
        default=None,
        description=(
            "Cap the asyncio.Semaphore concurrency used during the extraction "
            "gather phase for this university.  When None (default), the global "
            "_MAX_PARALLEL_FETCH value (currently 4) is used.  Set to 2 or 1 "
            "for Cloudflare-heavy sites (e.g. UTAS) where 4 simultaneous "
            "browser sessions trigger aggressive 429 rate-limiting, causing "
            "10-minute stalls that multiply across every URL batch.  Lower "
            "values reduce throughput per-batch but eliminate the 600s "
            "cooldown penalties, typically making the overall run faster."
        ),
    )
    # ── YAML-driven browser interaction actions ───────────────────────────────
    # Allows clients to configure interactive browser behaviour without code changes.
    # Executed in order after the initial page load and settle delay, before
    # the HTML is captured for extraction.
    #
    # Supported action keys (all optional, only one per dict entry):
    #   click_text:  str  — click the first visible element whose text
    #                        matches (case-insensitive, partial OK if unique).
    #                        "International" uses the smart toggle JS.
    #   click_css:   str  — click the first element matching this CSS selector.
    #   wait_for:   dict  — wait for a condition; supported sub-keys:
    #                         text: str    — wait until page contains this text
    #                         selector: str — wait until selector is visible
    #   expand_text: str  — click accordion / <details> / "Show more" whose
    #                        trigger text contains this phrase.
    #   scroll_to:   str  — scroll to a CSS selector or anchor (e.g. "#fees").
    #
    # Example (JCU international fee tab):
    #   extraction:
    #     actions:
    #       - click_text: "International"
    #       - wait_for:
    #           text: "International Students"
    #
    # After clicking, the scraper waits for networkidle (up to 5s) + 1.2s settle.
    actions: list[dict] = Field(
        default_factory=list,
        description="Ordered list of browser interaction steps to execute after page load.",
    )


# ── Merged UniConfig ─────────────────────────────────────────────────────────

class UniConfig(BaseModel):
    """Fully-merged per-university configuration (defaults → per-uni YAML → DB overrides).

    Instances are created by ``loader.load_uni_config`` and stored in the
    ``current_uni_config`` contextvar for the duration of a scrape job.
    Extractors that have been migrated to config-driven behaviour call
    ``get_uni_config()`` to read it.
    """

    slug: str = Field(description="Short identifier derived from hostname, e.g. 'acu', 'aut'.")
    name: str = Field(description="Human-readable university name.")
    university_id: Optional[int] = Field(default=None)
    base_url: str = Field(description="Origin URL, e.g. 'https://www.acu.edu.au'.")
    scrape_url: str = Field(description="Discovery entry-point URL.")

    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)

    def for_tier3_replay(self) -> "UniConfig":
        """Return a config containing ONLY the discovery section.

        Safe to replay against unknown universities in Tier-3 playbook matching.

        The returned config has:
          - ``discovery``: copied from self (URL filters, sitemap options, etc.)
          - ``extraction``: bare defaults (no fees config, no english config, no
            filters, no text_cleaning overrides)

        Why this matters
        ----------------
        ACAP's ``filters.domestic_only.enabled = true`` was tuned specifically
        for ACAP's page structure, where "domestic students only" appears in the
        main content.  On an unknown university's page that same text might live
        in a sidebar, footer, or not exist at all — silently rejecting all courses
        and making the result look like a discovery failure.

        The ``for_tier3_replay()`` boundary makes this exclusion explicit in code
        rather than relying on developer discipline.  Any code that builds a Tier-3
        temporary playbook config MUST call this method — not use the raw UniConfig.

        The ``filters`` section is under ``extraction:`` (applied at extraction time
        for all currently implemented filters) and is excluded here along with the
        rest of ``extraction:``.  This choice is documented explicitly so it survives
        refactoring: if ``filters`` is ever moved to a third top-level section, that
        section must also be excluded from the Tier-3 replay payload.
        """
        return UniConfig(
            slug=self.slug,
            name=self.name,
            university_id=self.university_id,
            base_url=self.base_url,
            scrape_url=self.scrape_url,
            discovery=self.discovery.model_copy(),
            # extraction intentionally omitted — ExtractionConfig() defaults apply.
        )


# ── Advanced Scraping Recipe ─────────────────────────────────────────────────
# Stored in scrape_config.recipe (JSONB).  Covers all 17 portal-configurable
# features so operators can convert existing manual spiders into DB-stored
# recipes without touching YAML or Python.

class RecipeApiConfig(BaseModel):
    """JSON / REST API endpoint that serves the full course catalogue."""
    endpoint: str = Field(default="", description="Full URL of the JSON feed.")
    method: str = Field(default="GET", description="HTTP method (GET or POST).")
    headers: dict = Field(default_factory=dict, description="Extra request headers.")
    root_path: Optional[str] = Field(
        default=None,
        description=(
            "Dot-separated path into the JSON response to reach the course array. "
            "E.g. 'courses', 'data.items', 'results'."
        ),
    )
    course_url_template: Optional[str] = Field(
        default=None,
        description=(
            "Python str.format_map template built from JSON field values. "
            "E.g. 'https://courses.hud.ac.uk/2025-26/{study_mode}/{study_level}/{urltitle}'"
        ),
    )
    fields: dict = Field(
        default_factory=dict,
        description=(
            "Mapping of standard scraper field → JSON key name. "
            "Standard keys: course_name, degree_level, study_mode_raw, "
            "full_time, part_time, url_slug, duration, campus, description."
        ),
    )
    pagination: Optional[dict] = Field(
        default=None,
        description=(
            "Optional pagination config: {type: 'offset', page_param: 'page', "
            "size_param: 'limit', page_size: 100, max_pages: 50}."
        ),
    )


class RecipeFieldSelector(BaseModel):
    """Extraction rule for a single field on the course detail page."""
    xpath: Optional[str] = Field(default=None, description="XPath expression.")
    css: Optional[str] = Field(default=None, description="CSS selector.")
    regex: Optional[str] = Field(default=None, description="Regex (first capture group used).")
    attribute: Optional[str] = Field(
        default=None,
        description="HTML attribute to extract (default: text content).",
    )
    transform: list = Field(
        default_factory=list,
        description="List of transforms: 'strip', 'lower', 'upper', {'regex_replace': {pattern, replacement}}.",
    )


class RecipeFeeRule(BaseModel):
    """Fee band: apply this amount when course name contains one of the keywords."""
    amount: float = Field(description="Annual fee in fee_currency.")
    keywords: list[str] = Field(default_factory=list, description="Case-insensitive match substrings.")


class RecipeIeltsConfig(BaseModel):
    """Regex rules for extracting IELTS / English scores from page text."""
    overall_regex: Optional[str] = Field(
        default=None,
        description="Regex with one capture group for the overall score. E.g. r'(\\d\\.?\\d*)\\s*overall'.",
    )
    band_regex: Optional[str] = Field(
        default=None,
        description="Regex for per-band / each-component minimum.",
    )
    source_xpath: Optional[str] = Field(
        default=None,
        description="XPath to limit the text region searched for IELTS scores.",
    )


class RecipeIntakeConfig(BaseModel):
    """Rules for extracting intake / start-date months."""
    xpath: Optional[str] = Field(default=None, description="XPath for start-date text.")
    regex: Optional[str] = Field(default=None, description="Regex with month-name capture group.")
    month_map: dict = Field(
        default_factory=dict,
        description="Map raw text → canonical month name. E.g. {'Autumn': 'March', 'Spring': 'July'}.",
    )


class RecipeCampusConfig(BaseModel):
    """Campus / location normalization rules."""
    default_city: Optional[str] = Field(default=None, description="Used when no campus is found.")
    valid_campuses: list[str] = Field(
        default_factory=list,
        description="Allowlist — courses at other campuses are dropped when non-empty.",
    )
    online_only_reject: bool = Field(
        default=False,
        description="Drop courses whose only delivery is Online/Distance.",
    )


class RecipeConfig(BaseModel):
    """Full advanced scraping recipe — admin-configurable without code.

    Stored at scrape_config.recipe (JSONB).  The orchestrator reads this
    block before all other discovery tiers and routes accordingly.

    Covers the 17 features requested:
      1  seed_urls                  8  regex extraction rules
      2  api (JSON endpoint)        9  fee mapping rules
      3  api.root_path             10  ielts / english rules
      4  api.course_url_template   11  intake rules
      5  api.fields                12  campus / location rules
      6  selectors (detail page)   13  online_only_reject
      7  XPath / CSS selectors     14  must_contain / block_url_patterns
                                   15  expected_min_courses
                                   16  fallback_strategy
                                   17  minimum_completeness
    """

    # ── 1. Discovery strategy ──
    discovery_strategy: str = Field(
        default="auto",
        description="auto | json_api | bfs | browser | sitemap",
    )
    seed_urls: list[str] = Field(
        default_factory=list,
        description="Course-listing page URLs fed to browser BFS with +300 priority.",
    )
    extra_course_urls: list[str] = Field(
        default_factory=list,
        description="Individual course URLs injected directly after discovery.",
    )
    expected_min_courses: Optional[int] = Field(
        default=None, description="Alert threshold — raise a warning if fewer courses found."
    )
    expected_max_courses: Optional[int] = Field(
        default=None, description="Sanity cap — flag if more courses found than expected."
    )
    fallback_strategy: str = Field(
        default="bfs",
        description="Strategy used when json_api returns 0 results: bfs | browser | sitemap | none.",
    )

    # ── 2-5. JSON API endpoint ──
    api: Optional[RecipeApiConfig] = Field(
        default=None, description="JSON API config (used when discovery_strategy=json_api).",
    )

    # ── 14. URL filters ──
    must_contain: list[str] = Field(
        default_factory=list,
        description="Drop any discovered URL that does NOT contain one of these substrings.",
    )
    block_url_patterns: list[str] = Field(
        default_factory=list,
        description="Regex patterns — drop any discovered URL matching any of these.",
    )

    # ── 6-8. Course detail page selectors ──
    fetch_detail_page: bool = Field(
        default=True,
        description="Fetch the individual course page for selector-based extraction.",
    )
    selectors: dict = Field(
        default_factory=dict,
        description=(
            "Per-field extraction rules. Keys are standard field names "
            "(course_name, degree_level, duration, intake_month, description, "
            "international_fee, ielts_overall, study_mode, course_location, "
            "entry_requirements, academic_level, other_requirement). "
            "Values are RecipeFieldSelector dicts."
        ),
    )

    # ── 10. IELTS / English ──
    ielts: Optional[RecipeIeltsConfig] = Field(default=None)

    # ── 11. Intake ──
    intake: Optional[RecipeIntakeConfig] = Field(default=None)

    # ── 9. Fee rules ──
    fee_currency: str = Field(default="AUD", description="ISO currency code (GBP, AUD, NZD…).")
    fee_year: Optional[int] = Field(default=None, description="Academic year for fee data.")
    fee_rules_undergraduate: list[RecipeFeeRule] = Field(
        default_factory=list,
        description="UG fee bands, checked most-specific first.",
    )
    fee_rules_postgraduate: list[RecipeFeeRule] = Field(
        default_factory=list,
        description="PG fee bands, checked most-specific first.",
    )

    # ── 12-13. Campus ──
    campus: Optional[RecipeCampusConfig] = Field(default=None)

    # ── 15-17. Quality ──
    minimum_completeness: int = Field(
        default=85, description="Auto-publish threshold (0-100).",
    )
    required_fields: list[str] = Field(
        default_factory=list,
        description="Fields that must be non-empty for a course to be accepted.",
    )
