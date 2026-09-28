"""Validate BCU course-facts locations without accepting page-wide text."""

import re

BCU_KEYFACT_LOCATIONS = frozenset({
    "alexander stadium",
    "birmingham",
    "bournville",
    "city centre",
    "city south",
    "margaret street",
    "rbc digbeth",
    "royal birmingham conservatoire",
    "school of jewellery",
    "wuhan textile university / bcu",
    "online",
    "distance learning",
    "uk campus",
})


def is_bcu_keyfact_location(value: str) -> bool:
    """Accept exact panel sites or combinations, but no partial-name matches."""
    normalized = value.strip().casefold()
    if normalized in BCU_KEYFACT_LOCATIONS:
        return True
    parts = [part.strip() for part in re.split(r"\s*(?:,| / )\s*", normalized)]
    return len(parts) > 1 and all(part in BCU_KEYFACT_LOCATIONS for part in parts)