"""Pure helpers for New Zealand programme-point fee normalization."""

from __future__ import annotations

import re


def find_programme_points_for_fee(text: str, fee: int | float | None) -> int | None:
    """Return programme points printed beside the selected tuition fee."""
    if not text or not isinstance(fee, (int, float)) or fee <= 0:
        return None

    amount = int(round(float(fee)))
    formatted = f"{amount:,}"
    amount_pattern = rf"(?:NZ\$\s*|\$\s*|NZD\s*)?(?:{re.escape(formatted)}|{amount})"
    match = re.search(
        amount_pattern + r".{0,120}?\(\s*(\d{2,3})\s+points?\s*\)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None

    points = int(match.group(1))
    return points if 30 <= points <= 600 else None


def full_time_years_from_nz_points(points: int | None) -> float | None:
    """Convert NZ programme points to full-time equivalent years."""
    if not isinstance(points, int) or not 30 <= points <= 600:
        return None
    return points / 120.0