"""Deterministic document scope matching; no institution-specific defaults."""
import re
import unicodedata


def text(value):
    return " ".join(str(value or "").split())


def title_key(value):
    value = unicodedata.normalize("NFKC", text(value)).casefold().replace("&", " and ")
    for pattern, replacement in [
        (r"\bb\.?\s?sc\b\.?", "bachelor science"),
        (r"\bm\.?\s?sc\b\.?", "master science"),
        (r"\bb\.?\s?a\b\.?", "bachelor arts"),
        (r"\bm\.?\s?a\b\.?", "master arts"),
        (r"\bhons\b", "honours"),
        (r"\bhonors\b", "honours"),
    ]:
        value = re.sub(pattern, replacement, value)
    return tuple(sorted(word for word in re.findall(r"\w+", value) if word != "of"))


def named_clause(row, clause):
    """Accept harmless formatting/award-order differences, not fuzzy titles."""
    label = re.split(r"\brequires?\b|\bIELTS\b|[:|—–]", text(clause), maxsplit=1, flags=re.I)[0]
    return len(text(row.course_name)) >= 5 and title_key(label) == title_key(row.course_name)


def stored_level(row):
    """Use the stored qualification, never infer a level from a course title."""
    level = text(getattr(row, "degree_level", "")).casefold()
    if level in {"undergraduate", "bachelor", "bachelors", "bachelor's", "bachelor degree"}:
        return "undergraduate"
    if level in {"postgraduate taught", "taught postgraduate", "master", "masters", "master's",
                 "graduate diploma", "graduate certificate"}:
        return "postgraduate_taught"
    return None


def declared_scope(clause):
    value = text(clause)
    if re.search(r"\bstandard English language requirement\b", value, re.I) and re.search(
        r"\bundergraduate or taught postgraduate qualification", value, re.I
    ) and re.search(r"\bIELTS\b", value, re.I):
        return "undergraduate and postgraduate taught"
    match = re.search(
        r"\b(?:all|every)\s+([a-z][a-z &-]{1,65}?)\s+(?:courses|programmes|programs)\b",
        text(clause), re.I,
    )
    return text(match[1]).casefold() if match else None


def scope_matches(row, clause, group):
    if group in {"health", "postgraduate_research"}:
        return False
    scope = declared_scope(clause)
    if scope == "undergraduate and postgraduate taught":
        return stored_level(row) in {"undergraduate", "postgraduate_taught"}
    if scope == "undergraduate":
        return stored_level(row) == "undergraduate"
    if scope in {"postgraduate taught", "taught postgraduate"}:
        return stored_level(row) == "postgraduate_taught"
    subjects = {text(getattr(row, key, "")).casefold() for key in ("category", "sub_category")}
    return bool(scope and scope in subjects and len(scope) >= 4)


def exception_blocks(row, clause, group):
    """Only explicitly unrelated protected-group exceptions can be set aside."""
    if not re.search(r"\b(?:except\w*|excluding|not apply|not applicable|higher|vary|different|specific)\b",
                     clause, re.I):
        return False
    # Narrow exception statements for protected groups do not invalidate every
    # unrelated undergraduate row. Unknown or mixed exceptions stay for review.
    exception = re.search(
        r"(?:except(?:ions?\s+apply\s+to)?|excluding)\s+(?:all\s+)?"
        r"((?:health|nursing|postgraduate research|research)"
        r"(?:\s*(?:,|and|&)\s*(?:health|nursing|postgraduate research|research))*)"
        r"\s+(?:courses|programmes|programs|degrees)\b",
        clause, re.I,
    )
    if not exception or group in {"health", "postgraduate_research", "unknown"}:
        return True
    tail = clause[exception.end():]
    # Do not silently discard further qualifications in the exception.
    return bool(re.search(r"\b(?:except|excluding|and|also|other|including)\b", tail, re.I))
