"""Guard the shared taxonomy contract across backend, seed, and portal."""
from __future__ import annotations

from pathlib import Path

from app.services.scraper.category import (
    CATEGORIES,
    _SUB_CATEGORY_MAP,
    infer_course_taxonomy,
)
from app.services.scraper.taxonomy import (
    COURSE_TAXONOMY,
    LEGACY_PARENT_ALIASES,
    TAXONOMY_PAIRS,
    canonical_parent,
)
from app.services.scraper.extractors.gemini_primary import _HARD_FIELDS
from scripts.apply_migration_040 import _SEED

ROOT = Path(__file__).resolve().parents[2]


def test_current_parents_drive_backend_and_database_seed():
    assert len(CATEGORIES) == 14
    assert tuple(COURSE_TAXONOMY) == CATEGORIES
    assert tuple(_SEED) == TAXONOMY_PAIRS
    assert {parent for parent, _ in _SEED} == set(CATEGORIES)


def test_classifier_sub_categories_all_exist_in_canonical_taxonomy():
    missing = {
        (parent, sub_category)
        for parent, sub_category, _ in _SUB_CATEGORY_MAP
        if sub_category not in COURSE_TAXONOMY[parent]
    }
    assert missing == set()


def test_portal_imports_shared_taxonomy_instead_of_copying_parent_names():
    source = (
        ROOT
        / "artifacts/university-portal/src/lib/course-constants.ts"
    ).read_text(encoding="utf-8")
    assert 'shared/course-taxonomy.json"' in source
    for parent in (*CATEGORIES, *LEGACY_PARENT_ALIASES):
        assert f'"{parent}": [' not in source


def test_legacy_aliases_only_target_current_parents():
    assert not (set(LEGACY_PARENT_ALIASES) & set(CATEGORIES))
    assert set(LEGACY_PARENT_ALIASES.values()) <= set(CATEGORIES)


def test_primary_ai_prompt_only_offers_canonical_parents():
    category_prompt = _HARD_FIELDS["category"]
    expected_options = ", ".join(f"'{parent}'" for parent in CATEGORIES)
    assert f"Pick the BEST match from: {expected_options}." in category_prompt


def test_every_previous_ai_parent_is_canonicalized_at_write_boundaries():
    for legacy, expected in LEGACY_PARENT_ALIASES.items():
        inferred = infer_course_taxonomy(
            "Operator Classified Course",
            category=legacy,
            sub_category="Operator Chosen Discipline",
        )
        assert inferred == {
            "category": expected,
            "sub_category": "Operator Chosen Discipline",
        }
        assert canonical_parent(legacy.lower()) == expected


def test_unknown_manual_parent_is_not_overwritten():
    assert canonical_parent("Operator Custom Category") == "Operator Custom Category"