#!/usr/bin/env python3
"""Migration 040 — course_sub_categories table + canonical seed data.

Creates:
  - course_sub_categories: persistent, auto-growing sub-category vocabulary.
    Each row is a (category, sub_category) pair valid for course classification.
    The table is pre-seeded from the frontend course-constants.ts canonical list.
    New Gemini-generated sub-categories are added automatically at approve time
    (auto_added=True) via app.services.sub_category_matcher.resolve_sub_category().

Why this migration exists:
  The table is used by stage_course.py to canonicalise free-text sub_category
  values extracted by Gemini. If the table is absent, every staging attempt
  raises "relation course_sub_categories does not exist" and the course is NOT
  staged (data loss). Previously, the table was created by a Drizzle schema push
  on Replit but was not tracked as a numbered migration — meaning fresh local
  clones would hit the error.

Apply on dev:
    PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_040.py

Apply on prod:
    cd /root/University-and-Course-data
    PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_040.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text
from app.database import AsyncSessionLocal

# Canonical taxonomy — mirrors artifacts/university-portal/src/lib/course-constants.ts
# Keep in sync with the frontend whenever categories or sub-categories are added.
_SEED: list[tuple[str, str]] = [
    # Agriculture & Environmental Studies
    ("Agriculture & Environmental Studies", "Agriculture"),
    ("Agriculture & Environmental Studies", "Animal Sciences"),
    ("Agriculture & Environmental Studies", "Aquaculture & Fisheries"),
    ("Agriculture & Environmental Studies", "Forestry"),
    ("Agriculture & Environmental Studies", "Horticulture"),
    ("Agriculture & Environmental Studies", "Plant & Crop Sciences"),
    ("Agriculture & Environmental Studies", "Biodiversity & Conservation"),
    ("Agriculture & Environmental Studies", "Climate Studies & Meteorology"),
    ("Agriculture & Environmental Studies", "Earth Sciences"),
    ("Agriculture & Environmental Studies", "Ecology"),
    ("Agriculture & Environmental Studies", "Environmental Sciences"),
    ("Agriculture & Environmental Studies", "Geology"),
    ("Agriculture & Environmental Studies", "Hydrology & Water Management"),
    ("Agriculture & Environmental Studies", "Natural Resource Management"),
    ("Agriculture & Environmental Studies", "Sustainable Development"),
    ("Agriculture & Environmental Studies", "Toxicology"),
    ("Agriculture & Environmental Studies", "Food Science"),
    ("Agriculture & Environmental Studies", "Veterinary Science"),
    ("Agriculture & Environmental Studies", "Development Policies"),
    # Architecture, Building & Design
    ("Architecture, Building & Design", "Architecture"),
    ("Architecture, Building & Design", "Design"),
    ("Architecture, Building & Design", "Building"),
    ("Architecture, Building & Design", "Fashion Design"),
    ("Architecture, Building & Design", "Graphic Design"),
    ("Architecture, Building & Design", "User Experience Design"),
    ("Architecture, Building & Design", "Industrial Design"),
    ("Architecture, Building & Design", "Interior Design"),
    # Arts, Humanities & Social Sciences
    ("Arts, Humanities & Social Sciences", "Arts"),
    ("Arts, Humanities & Social Sciences", "Humanities"),
    ("Arts, Humanities & Social Sciences", "Social Sciences"),
    ("Arts, Humanities & Social Sciences", "Creative Writing"),
    ("Arts, Humanities & Social Sciences", "General Studies & Classics"),
    ("Arts, Humanities & Social Sciences", "History"),
    ("Arts, Humanities & Social Sciences", "Liberal Arts"),
    ("Arts, Humanities & Social Sciences", "Literature"),
    ("Arts, Humanities & Social Sciences", "Linguistics"),
    ("Arts, Humanities & Social Sciences", "Philosophy & Ethics"),
    ("Arts, Humanities & Social Sciences", "Gender & Sexuality Studies"),
    ("Arts, Humanities & Social Sciences", "International Relations"),
    ("Arts, Humanities & Social Sciences", "Political Science"),
    ("Arts, Humanities & Social Sciences", "Psychology"),
    ("Arts, Humanities & Social Sciences", "Public Policy"),
    ("Arts, Humanities & Social Sciences", "Sociology"),
    ("Arts, Humanities & Social Sciences", "Leadership"),
    # Business & Management
    ("Business & Management", "Accounting"),
    ("Business & Management", "Business"),
    ("Business & Management", "Actuarial Science"),
    ("Business & Management", "Agribusiness"),
    ("Business & Management", "Human Resource Management"),
    ("Business & Management", "Auditing"),
    ("Business & Management", "Enterprise Resource Planning Management"),
    ("Business & Management", "Business Administration"),
    ("Business & Management", "Business Intelligence & Analytics"),
    ("Business & Management", "Commerce"),
    ("Business & Management", "Finance"),
    ("Business & Management", "Marketing"),
    ("Business & Management", "Management"),
    ("Business & Management", "Project Management"),
    ("Business & Management", "Economics"),
    ("Business & Management", "Quality Assurance & Management"),
    ("Business & Management", "Manufacturing"),
    ("Business & Management", "Real Estate & Trading"),
    # Computer Science & IT
    ("Computer Science & IT", "Artificial Intelligence"),
    ("Computer Science & IT", "Information Technology (IT)"),
    ("Computer Science & IT", "Business Information Systems"),
    ("Computer Science & IT", "Computer Science"),
    ("Computer Science & IT", "Cyber Security"),
    ("Computer Science & IT", "Networking"),
    ("Computer Science & IT", "Data Science & Big Data"),
    ("Computer Science & IT", "Software Engineering"),
    ("Computer Science & IT", "Video Games & Multimedia"),
    ("Computer Science & IT", "Web Technologies & Cloud Computing"),
    ("Computer Science & IT", "3D Arts and Animation"),
    # Education & Social Work
    ("Education & Social Work", "Education"),
    ("Education & Social Work", "Coaching"),
    ("Education & Social Work", "Counselling"),
    ("Education & Social Work", "Early Childhood Education"),
    ("Education & Social Work", "Educational Research"),
    ("Education & Social Work", "Pedagogy"),
    ("Education & Social Work", "Teaching"),
    ("Education & Social Work", "Social Work"),
    # Engineering & Technology
    ("Engineering & Technology", "Aerospace Engineering"),
    ("Engineering & Technology", "Automotive Engineering"),
    ("Engineering & Technology", "Bio & Biomedical Engineering"),
    ("Engineering & Technology", "Chemical Engineering"),
    ("Engineering & Technology", "Civil Engineering & Construction"),
    ("Engineering & Technology", "Electrical Engineering"),
    ("Engineering & Technology", "Electronics Engineering"),
    ("Engineering & Technology", "Energy & Power Engineering"),
    ("Engineering & Technology", "Environmental Engineering"),
    ("Engineering & Technology", "General Engineering & Technology"),
    ("Engineering & Technology", "Industrial & Systems Engineering"),
    ("Engineering & Technology", "Marine Engineering"),
    ("Engineering & Technology", "Engineering Management"),
    ("Engineering & Technology", "Materials Science & Engineering"),
    ("Engineering & Technology", "Mechanical Engineering"),
    ("Engineering & Technology", "Mechatronics"),
    ("Engineering & Technology", "Mining Oil & Gas"),
    ("Engineering & Technology", "Robotics"),
    ("Engineering & Technology", "Aviation"),
    ("Engineering & Technology", "Sustainable Energy"),
    ("Engineering & Technology", "Transportation"),
    # Languages & Culture
    ("Languages & Culture", "Area & Cultural Studies"),
    ("Languages & Culture", "Islamic Studies"),
    ("Languages & Culture", "Languages"),
    ("Languages & Culture", "TESOL"),
    ("Languages & Culture", "Christian Studies"),
    # Law & Criminology
    ("Law & Criminology", "Business Law"),
    ("Law & Criminology", "Criminology"),
    ("Law & Criminology", "Law"),
    ("Law & Criminology", "Civil & Private Law"),
    ("Law & Criminology", "Criminal Law"),
    ("Law & Criminology", "International Law"),
    ("Law & Criminology", "Legal Studies"),
    ("Law & Criminology", "Patent & Intellectual Property Law"),
    ("Law & Criminology", "Public Law"),
    ("Law & Criminology", "Terrorism & Security"),
    # Maths & Sciences
    ("Maths & Sciences", "Applied Mathematics"),
    ("Maths & Sciences", "Science"),
    ("Maths & Sciences", "Astronomy & Space Sciences"),
    ("Maths & Sciences", "Biochemistry"),
    ("Maths & Sciences", "Biotechnology"),
    ("Maths & Sciences", "Chemistry"),
    ("Maths & Sciences", "Financial Mathematics"),
    ("Maths & Sciences", "Dermal Science"),
    ("Maths & Sciences", "Genetics"),
    ("Maths & Sciences", "Physics"),
    ("Maths & Sciences", "Statistics"),
    ("Maths & Sciences", "Biology"),
    ("Maths & Sciences", "Mathematics"),
    # Media & Communications
    ("Media & Communications", "Journalism"),
    ("Media & Communications", "Communications"),
    ("Media & Communications", "Media Management"),
    ("Media & Communications", "Media Studies & Mass Media"),
    ("Media & Communications", "Public Relations"),
    ("Media & Communications", "Media Marketing"),
    ("Media & Communications", "Translation & Interpreting"),
    ("Media & Communications", "Film Photography & Media"),
    # Medicine & Health
    ("Medicine & Health", "Biomedicine"),
    ("Medicine & Health", "Biomedical Sciences"),
    ("Medicine & Health", "Clinical Psychology"),
    ("Medicine & Health", "Complementary & Alternative Medicine"),
    ("Medicine & Health", "Dentistry"),
    ("Medicine & Health", "Health Sciences"),
    ("Medicine & Health", "Human Medicine"),
    ("Medicine & Health", "Midwifery"),
    ("Medicine & Health", "Nursing"),
    ("Medicine & Health", "Nutrition & Dietetics"),
    ("Medicine & Health", "Pharmacy"),
    ("Medicine & Health", "Physiotherapy"),
    ("Medicine & Health", "Public Health"),
    ("Medicine & Health", "Pharmaceutical"),
    ("Medicine & Health", "Laboratory Medicine"),
    ("Medicine & Health", "Community Services"),
    ("Medicine & Health", "Community Health"),
    ("Medicine & Health", "Ageing & Aged Care"),
    ("Medicine & Health", "Veterinary Medicine"),
    ("Medicine & Health", "Psychotherapy"),
    ("Medicine & Health", "Health & Social Care"),
    # Music & Performing Arts
    ("Music & Performing Arts", "Music"),
    ("Music & Performing Arts", "Music History"),
    ("Music & Performing Arts", "Visual Arts"),
    # Sports & Personal Services
    ("Sports & Personal Services", "Sports Management"),
    ("Sports & Personal Services", "Sports Science"),
    ("Sports & Personal Services", "Beauty Therapy"),
    ("Sports & Personal Services", "Exercise Sciences"),
    # Tourism & Hospitality
    ("Tourism & Hospitality", "Culinary Arts"),
    ("Tourism & Hospitality", "Event Management"),
    ("Tourism & Hospitality", "Hospitality Management"),
    ("Tourism & Hospitality", "Tourism"),
]

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS course_sub_categories (
    id          SERIAL PRIMARY KEY,
    category    TEXT        NOT NULL,
    sub_category TEXT       NOT NULL,
    auto_added  BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_course_sub_cat UNIQUE (category, sub_category)
)
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_course_sub_cat_category
    ON course_sub_categories (category)
"""


async def run() -> None:
    async with AsyncSessionLocal() as db:
        async with db.begin():
            # 1. Create table
            await db.execute(text(_CREATE_TABLE))
            await db.execute(text(_CREATE_INDEX))
            print("Table and index created (or already existed).")

            # 2. Seed canonical rows — INSERT … ON CONFLICT DO NOTHING
            #    so re-runs are safe and auto_added=True rows are never touched.
            inserted = 0
            for category, sub_category in _SEED:
                result = await db.execute(
                    text("""
                        INSERT INTO course_sub_categories (category, sub_category, auto_added)
                        VALUES (:cat, :sub, FALSE)
                        ON CONFLICT (category, sub_category) DO NOTHING
                    """),
                    {"cat": category, "sub": sub_category},
                )
                inserted += result.rowcount

            total = len(_SEED)
            skipped = total - inserted
            print(
                f"Seed: inserted {inserted}/{total} canonical rows "
                f"({skipped} already existed — skipped)."
            )

        # Verify
        async with db.begin():
            count = (
                await db.execute(text("SELECT COUNT(*) FROM course_sub_categories"))
            ).scalar()
            print(f"Total rows in course_sub_categories: {count}")


if __name__ == "__main__":
    asyncio.run(run())
