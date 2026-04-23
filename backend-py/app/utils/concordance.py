"""Fuzzy-match helpers (rapidfuzz) for course-name and university-name dedupe."""
from __future__ import annotations

from rapidfuzz import fuzz, process


def best_match(needle: str, haystack: list[str], cutoff: int = 85) -> str | None:
    if not needle or not haystack:
        return None
    result = process.extractOne(needle, haystack, scorer=fuzz.WRatio, score_cutoff=cutoff)
    return result[0] if result else None


def is_same_name(a: str, b: str, cutoff: int = 92) -> bool:
    if not a or not b:
        return False
    if a.strip().lower() == b.strip().lower():
        return True
    return fuzz.WRatio(a, b) >= cutoff
