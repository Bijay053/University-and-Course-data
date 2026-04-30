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

class DiscoveryConfig(BaseModel):
    """Safe to replay against unknown universities (Tier-3 playbook matching)."""

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
    bfs_page_budget: Optional[int] = Field(
        default=None,
        description=(
            "Override the default BFS page budget (12 fast / 25 full).  "
            "Raise for sites with many listing pages (e.g. UOW ~62 pages)."
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
        default=False,
        description=(
            "When true, courses with all-online delivery are dropped during staging. "
            "Rarely needed — most international portals already exclude pure-online."
        ),
    )


class FiltersConfig(BaseModel):
    domestic_only: DomesticOnlyFilter = Field(
        default_factory=DomesticOnlyFilter,
    )
    online_only: OnlineOnlyFilter = Field(
        default_factory=OnlineOnlyFilter,
    )


class LocationCleaningConfig(BaseModel):
    strip_patterns: list[str] = Field(
        default_factory=list,
        description=(
            "Regex patterns applied to raw location strings before parsing. "
            "Each matching fragment is stripped.  Order matters — patterns are "
            "applied left to right.  "
            "E.g. ACAP: [r'^\\s*\\^.*$'] strips '^ ^Available in Perth' cruft."
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


class TextCleaningConfig(BaseModel):
    location: LocationCleaningConfig = Field(
        default_factory=LocationCleaningConfig,
    )
    duration: DurationCleaningConfig = Field(
        default_factory=DurationCleaningConfig,
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
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    text_cleaning: TextCleaningConfig = Field(default_factory=TextCleaningConfig)
    staging: StagingConfig = Field(default_factory=StagingConfig)


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
