"""SQL fragments for literal and typo-tolerant public course/location search."""
import re


def literal_like(value: str) -> str:
    """Escape PostgreSQL LIKE wildcards; user text is a literal search term."""
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def name_match(column: str, query: str, prefix: str) -> tuple[str, dict]:
    """Require every meaningful query token, not just a shared award word."""
    normalized = query.strip().lower()
    tokens = list(dict.fromkeys(re.findall(r"[^\W_]+", normalized)))
    meaningful = [t for t in tokens if t not in {"a", "an", "the", "of", "in", "and", "for", "with", "at", "to"}]
    tokens = meaningful or tokens
    if not tokens:
        return "FALSE", {}
    clauses = []
    params = {}
    for i, token in enumerate(tokens):
        key = f"{prefix}_{i}"
        params[key] = token
        params[f"{key}_like"] = literal_like(token)
        params[f"{key}_word"] = r"\m" + re.escape(token) + r"\M"
        # Stemming and prefix/substring matching handle normal variants;
        # fuzzy matching is only useful for words long enough to be distinctive.
        fuzzy = f" OR strict_word_similarity(:{key}, lower({column})) >= 0.5" if len(token) >= 4 else ""
        literal = f"lower({column}) ILIKE :{key}_like" if len(token) >= 4 else f"lower({column}) ~ :{key}_word"
        clauses.append(
            f"(to_tsvector('english', coalesce({column}, '')) @@ plainto_tsquery('english', :{key}) "
            f"OR {literal}{fuzzy})"
        )
    return "(" + " AND ".join(clauses) + ")", params


def location_match(column: str) -> str:
    """Match real campus/city text without borrowing a university name."""
    return (
        f"(lower({column}) ILIKE :loc "
        f"OR (length(:loc_trgm) >= 4 AND "
        f"strict_word_similarity(:loc_trgm, lower({column})) >= 0.55))"
    )
