"""Shared course taxonomy loaded from the repository's canonical JSON file."""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import MappingProxyType

_TAXONOMY_PATH = (
    Path(__file__).resolve().parents[4] / "shared" / "course-taxonomy.json"
)
_DATA = json.loads(_TAXONOMY_PATH.read_text(encoding="utf-8"))

COURSE_TAXONOMY = MappingProxyType(
    {
        parent: tuple(sub_categories)
        for parent, sub_categories in _DATA["categories"].items()
    }
)
CATEGORIES = tuple(COURSE_TAXONOMY)
DEFAULT_SUBCATEGORY_BY_CATEGORY = MappingProxyType(
    dict(_DATA["defaultSubcategories"])
)
LEGACY_PARENT_ALIASES = MappingProxyType(_DATA["legacyParentAliases"])
TAXONOMY_PAIRS = tuple(
    (parent, sub_category)
    for parent, sub_categories in COURSE_TAXONOMY.items()
    for sub_category in sub_categories
)

if set(DEFAULT_SUBCATEGORY_BY_CATEGORY) != set(CATEGORIES):
    raise ValueError("Every category must define exactly one default subcategory")
for _parent, _default_subcategory in DEFAULT_SUBCATEGORY_BY_CATEGORY.items():
    if _default_subcategory not in COURSE_TAXONOMY[_parent]:
        raise ValueError(
            f"Default subcategory {_default_subcategory!r} is not valid for {_parent!r}"
        )


def _parent_key(value: str) -> str:
    return re.sub(
        r"[^a-z0-9]+", " ", value.lower().replace("&", " and ")
    ).strip()


_CANONICAL_BY_KEY = MappingProxyType(
    {
        **{_parent_key(parent): parent for parent in CATEGORIES},
        **{
            _parent_key(legacy): canonical
            for legacy, canonical in LEGACY_PARENT_ALIASES.items()
        },
    }
)


def canonical_parent(value: str | None) -> str | None:
    """Canonicalize a current/legacy parent; preserve unknown manual values."""
    if value is None:
        return None
    clean = value.strip()
    if not clean:
        return None
    return _CANONICAL_BY_KEY.get(_parent_key(clean), clean)