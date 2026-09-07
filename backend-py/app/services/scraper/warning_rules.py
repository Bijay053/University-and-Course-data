"""Pure predicates for operator-facing scraper warnings."""


def normalize_skip_reason_key(reason: str | None) -> str:
    """Return the stable key exposed in completion summaries.

    ``stage_course`` prefixes guard failures with ``"rejected: "`` for human
    logs.  Completion payloads are machine-readable and must expose the guard
    reason itself so clients can classify expected policy exclusions.
    """
    value = (reason or "unknown").strip().lower()
    if value.startswith("rejected:"):
        value = value.removeprefix("rejected:").strip()
    return value.replace(" ", "_")[:40]


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