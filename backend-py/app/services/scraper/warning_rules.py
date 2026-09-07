"""Pure predicates for operator-facing scraper warnings."""


def should_emit_category_pages_warning(
    *,
    category_count: int,
    total_count: int,
    skip_degree_qualifier_check: bool,
) -> bool:
    """Return whether URL-shape evidence is strong enough for a critical warning."""
    if skip_degree_qualifier_check or total_count <= 0:
        return False
    return category_count / total_count > 0.70 and total_count <= 30