"""Validate BCU course-facts locations without accepting page-wide text."""

BCU_KEYFACT_LOCATIONS = frozenset({
    "birmingham",
    "bournville",
    "city centre",
    "city south",
    "margaret street",
    "royal birmingham conservatoire",
    "school of jewellery",
    "wuhan textile university / bcu",
    "online",
    "distance learning",
    "uk campus",
})


def is_bcu_keyfact_location(value: str) -> bool:
    """Accept exact panel values or comma-separated combinations of them."""
    parts = [part.strip().casefold() for part in value.split(",")]
    return bool(parts) and all(part in BCU_KEYFACT_LOCATIONS for part in parts)