#!/usr/bin/env python3
"""Migration 031 — Phase 12: Country Intelligence Layer.

Creates:
  - country_patterns  — per-country scraping intelligence and learning state

Seeds rows for Australia, United Kingdom, United States of America, Canada,
New Zealand, and Europe (English-taught programmes).

Apply on production:
    cd /root/University-and-Course-data
    PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_031.py
"""
import asyncio
import json
import os

_raw_url = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:password@127.0.0.1:5432/university_portal",
)
# Strip sslmode and other params not supported by asyncpg driver
import re as _re
_raw_url = _re.sub(r"\?.*$", "", _raw_url)
DATABASE_URL = _raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)

_DDL = """
CREATE TABLE IF NOT EXISTS country_patterns (
    id                          SERIAL PRIMARY KEY,
    country                     TEXT NOT NULL UNIQUE,
    common_platforms            JSONB NOT NULL DEFAULT '[]',
    common_fee_patterns         JSONB NOT NULL DEFAULT '{}',
    common_intake_patterns      JSONB NOT NULL DEFAULT '[]',
    common_requirement_patterns JSONB NOT NULL DEFAULT '{}',
    common_pdf_patterns         JSONB NOT NULL DEFAULT '[]',
    preferred_strategy          TEXT NOT NULL DEFAULT 'bfs',
    known_risks                 JSONB NOT NULL DEFAULT '[]',
    success_count               INTEGER NOT NULL DEFAULT 0,
    avg_completeness            FLOAT,
    avg_confidence              FLOAT,
    last_scrape_at              TIMESTAMPTZ,
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

_IDX = "CREATE UNIQUE INDEX IF NOT EXISTS ix_country_patterns_country ON country_patterns(LOWER(country))"

# ── Seed rows ────────────────────────────────────────────────────────────────
_SEEDS: list[dict] = [
    {
        "country": "Australia",
        "common_platforms": ["courseloop", "course-search", "handbooks", "courseseeker"],
        "common_fee_patterns": {
            "currency": "AUD",
            "label_patterns": ["international fee", "tuition fee", "annual fee", "total fee"],
            "term": "annual",
            "cricos_code_required": True,
        },
        "common_intake_patterns": ["february", "march", "july", "november"],
        "common_requirement_patterns": {
            "english_tests": ["ielts", "toefl", "pte", "cambridge"],
            "academic": ["gpa", "atar", "honours", "bachelor"],
            "cricos": True,
        },
        "common_pdf_patterns": [
            "international guide", "fee schedule", "english requirements",
            "cricos", "handbook", "postgraduate guide",
        ],
        "preferred_strategy": "hybrid",
        "known_risks": [
            "Domestic-only fee pages need ?international=true query param or dedicated subdomain",
            "CRICOS code required for international student enrolment — scrape from course page or PDF",
            "Trimester vs semester naming varies widely — do not assume March=Sem1",
            "Course Seeker integration pages may differ from university's own pages",
        ],
    },
    {
        "country": "United Kingdom",
        "common_platforms": ["ucas", "sits", "tribal", "banner", "web-portal"],
        "common_fee_patterns": {
            "currency": "GBP",
            "label_patterns": ["international tuition", "tuition fee", "programme fee"],
            "term": "annual",
            "per_year": True,
        },
        "common_intake_patterns": ["september", "january", "october"],
        "common_requirement_patterns": {
            "english_tests": ["ielts", "toefl", "cambridge", "pearson"],
            "academic": ["honours", "2:1", "2:2", "a-levels", "ucas tariff"],
            "ucas": True,
        },
        "common_pdf_patterns": [
            "prospectus", "programme specification", "international fees",
            "entry requirements", "international student guide",
        ],
        "preferred_strategy": "bfs",
        "known_risks": [
            "Cloudflare protection common on UK uni sites — browser fallback may be needed",
            "UCAS course pages differ from university-hosted pages — prefer university source",
            "Degree classification language (2:1, 2:2) must not be confused with GPA",
            "Foundation year and integrated masters (MEng, MPhys) inflate course count",
        ],
    },
    {
        "country": "United States of America",
        "common_platforms": ["courseleaf", "acalog", "curriculog", "banner", "slate"],
        "common_fee_patterns": {
            "currency": "USD",
            "label_patterns": ["tuition", "per credit", "per credit hour", "total program cost"],
            "term": "per_credit",
            "alternate_term": "annual",
        },
        "common_intake_patterns": ["august", "january", "may", "june"],
        "common_requirement_patterns": {
            "english_tests": ["toefl", "ielts", "duolingo", "pte"],
            "academic": ["gpa", "gre", "gmat", "bachelor's degree", "credits"],
            "credits": True,
        },
        "common_pdf_patterns": [
            "catalog", "graduate catalog", "undergraduate catalog",
            "international student handbook", "tuition and fees",
        ],
        "preferred_strategy": "api",
        "known_risks": [
            "Courseleaf and Acalog expose catalog APIs — prefer structured API over HTML scraping",
            "Per-credit-hour fees require total-credit calculation for comparable annual fee",
            "Semester vs quarter system varies — August start ≠ full year fee",
            "State universities have separate in-state/out-of-state fee tables",
        ],
    },
    {
        "country": "Canada",
        "common_platforms": ["orbis", "banner", "coursedog", "academic-calendar"],
        "common_fee_patterns": {
            "currency": "CAD",
            "label_patterns": ["international tuition", "program fee", "per credit"],
            "term": "annual",
            "alternate_term": "per_term",
        },
        "common_intake_patterns": ["september", "january", "may"],
        "common_requirement_patterns": {
            "english_tests": ["ielts", "toefl", "cael", "duolingo"],
            "academic": ["gpa", "honours", "bachelor", "co-op", "credential"],
        },
        "common_pdf_patterns": [
            "international fees", "tuition fees", "graduate calendar",
            "undergraduate calendar", "program guide",
        ],
        "preferred_strategy": "bfs",
        "known_risks": [
            "Co-op and work-integrated programs often have different fee structures",
            "Quebec institutions may have French-first course pages",
            "Credential type terminology differs from AU/UK (diploma, certificate, applied degree)",
            "Academic calendar PDFs are authoritative but large — target per-program sections",
        ],
    },
    {
        "country": "New Zealand",
        "common_platforms": ["courseinfo", "my.massey", "waimea"],
        "common_fee_patterns": {
            "currency": "NZD",
            "label_patterns": ["international fees", "tuition fee", "annual fee"],
            "term": "annual",
            "nzqa": True,
        },
        "common_intake_patterns": ["february", "july"],
        "common_requirement_patterns": {
            "english_tests": ["ielts", "toefl", "pte"],
            "academic": ["nzqa level", "bachelor", "honours", "diploma"],
            "nzqa": True,
        },
        "common_pdf_patterns": [
            "international fees", "entry requirements", "programme information",
            "nzqa", "fees and funding",
        ],
        "preferred_strategy": "bfs",
        "known_risks": [
            "NZQA qualification framework levels must be mapped to degree_level correctly",
            "Semester 1 = February, Semester 2 = July — inverse of some AU universities",
            "Smaller universities may have limited structured data — PDF fallback important",
        ],
    },
    {
        "country": "Europe",
        "common_platforms": ["kuali", "studyfinder", "stud.ip", "moodle-catalogue"],
        "common_fee_patterns": {
            "currency": "EUR",
            "label_patterns": ["tuition fee", "programme fee", "enrolment fee"],
            "term": "annual",
            "ects": True,
        },
        "common_intake_patterns": ["september", "october", "february"],
        "common_requirement_patterns": {
            "english_tests": ["ielts", "toefl", "cambridge"],
            "academic": ["ects", "bachelor", "master", "european credit"],
            "ects": True,
        },
        "common_pdf_patterns": [
            "programme guide", "course catalogue", "admission requirements",
            "english taught", "international tuition",
        ],
        "preferred_strategy": "sitemap",
        "known_risks": [
            "English-taught programme filter essential — most pages may be in local language",
            "ECTS credit load varies — 60 ECTS = 1 academic year",
            "Fee pages often separate registration fee from tuition — sum both",
            "Intake terminology (academic year, teaching period) varies across countries",
        ],
    },
    {
        "country": "Unknown",
        "common_platforms": [],
        "common_fee_patterns": {"currency": "USD", "term": "annual"},
        "common_intake_patterns": [],
        "common_requirement_patterns": {"english_tests": ["ielts", "toefl"]},
        "common_pdf_patterns": ["fees", "requirements", "international"],
        "preferred_strategy": "bfs",
        "known_risks": [
            "Country not detected — applying generic extraction defaults",
        ],
    },
]

_UPSERT = """
INSERT INTO country_patterns (
    country, common_platforms, common_fee_patterns, common_intake_patterns,
    common_requirement_patterns, common_pdf_patterns, preferred_strategy,
    known_risks, success_count, avg_completeness, avg_confidence, updated_at
) VALUES (
    :country, :common_platforms::jsonb, :common_fee_patterns::jsonb,
    :common_intake_patterns::jsonb, :common_requirement_patterns::jsonb,
    :common_pdf_patterns::jsonb, :preferred_strategy, :known_risks::jsonb,
    0, NULL, NULL, NOW()
)
ON CONFLICT (country) DO UPDATE SET
    common_platforms            = EXCLUDED.common_platforms,
    common_fee_patterns         = EXCLUDED.common_fee_patterns,
    common_intake_patterns      = EXCLUDED.common_intake_patterns,
    common_requirement_patterns = EXCLUDED.common_requirement_patterns,
    common_pdf_patterns         = EXCLUDED.common_pdf_patterns,
    preferred_strategy          = EXCLUDED.preferred_strategy,
    known_risks                 = EXCLUDED.known_risks,
    updated_at                  = NOW()
"""


async def main() -> None:
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text

    engine = create_async_engine(DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        print("Creating country_patterns table …")
        await conn.execute(text(_DDL))
        await conn.execute(text(_IDX))
        print("Seeding country rows …")
        for row in _SEEDS:
            params = {
                "country": row["country"],
                "common_platforms": json.dumps(row["common_platforms"]),
                "common_fee_patterns": json.dumps(row["common_fee_patterns"]),
                "common_intake_patterns": json.dumps(row["common_intake_patterns"]),
                "common_requirement_patterns": json.dumps(row["common_requirement_patterns"]),
                "common_pdf_patterns": json.dumps(row["common_pdf_patterns"]),
                "preferred_strategy": row["preferred_strategy"],
                "known_risks": json.dumps(row["known_risks"]),
            }
            await conn.execute(text(_UPSERT), params)
            print(f"  ✓ {row['country']}")

    await engine.dispose()
    print("\nMigration 031 complete.")


if __name__ == "__main__":
    asyncio.run(main())
