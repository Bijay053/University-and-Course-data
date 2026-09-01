"""Universal course-name suffix stripper.

Single source of truth for removing university-name suffixes from scraped
course titles.  Used by:
  - extractors/course_name.py          (during initial extraction)
  - orchestrator.py                    (after extraction, pre-staging)
  - /api/scrape/clean-course-names     (backfill endpoint for staged rows)

Separator patterns supported (all case-insensitive, anchored at the END):
  Course Name | University Name
  Course Name - University Name
  Course Name – University Name   (en-dash)
  Course Name — University Name   (em-dash)
  Course Name at University Name
  Course Name @ University Name
  Course Name : University Name
"""

from __future__ import annotations

import re
from typing import Sequence

# Separator classes matched before the university token.
# Captures pipe, dashes, en/em-dash, colon, bullet, @, and the word "at"
# (word-bounded so "Biostatistics" is never partially matched).
_SEP_PAT = r"(?:\s*[\|\-\u2013\u2014:•@]\s*|\s+at\s+)"

# Minimum acceptable result length (guards against stripping the whole title).
_MIN_LEN = 5


def clean_course_name(
    name: str,
    *,
    university_name: str = "",
    aliases: Sequence[str] = (),
    extra_tokens: Sequence[str] = (),
) -> tuple[str, str | None]:
    """Strip university-name suffixes from a course title.

    Returns ``(cleaned_name, stripped_suffix | None)``.
    If nothing was stripped, ``cleaned_name`` is identical to ``name`` and
    ``stripped_suffix`` is ``None``.

    Parameters
    ----------
    name:
        Raw course name as extracted from the page.
    university_name:
        Full university name from the database record
        (e.g. "University of East London").
    aliases:
        Extra name strings from YAML ``extraction.course_name.university_aliases``
        (e.g. ``["University of East London", "UEL"]``).
    extra_tokens:
        Supplemental tokens such as a domain-derived short name
        (e.g. ``"uel"`` from ``uel.ac.uk``).
    """
    if not name:
        return name, None

    # Collect all tokens; deduplicate (case-insensitive), preserve order.
    seen: set[str] = set()
    all_tokens: list[str] = []

    for raw in list(aliases) + ([university_name] if university_name else []) + list(extra_tokens):
        t = (raw or "").strip()
        if t and len(t) >= 2 and t.lower() not in seen:
            seen.add(t.lower())
            all_tokens.append(t)

    # Sort longest-first: "University of East London" tried before "University" / "uel".
    all_tokens.sort(key=len, reverse=True)

    for token in all_tokens:
        pat = re.compile(
            _SEP_PAT + re.escape(token) + r"\s*$",
            re.IGNORECASE,
        )
        m = pat.search(name)
        if m and m.start() > 0:
            candidate = name[: m.start()].strip(" \t\u2013\u2014|:•@-")
            if candidate and len(candidate) >= _MIN_LEN:
                return candidate, name[m.start():]

    return name, None


def clean_course_name_with_config(
    name: str,
    *,
    university_name: str = "",
    scrape_url: str = "",
    aliases: Sequence[str] = (),
) -> tuple[str, str | None]:
    """Convenience wrapper that augments :func:`clean_course_name` with
    per-uni YAML aliases and a domain-derived short token.

    YAML aliases are read from the current contextvar (set by the orchestrator
    at the start of every scrape job).  ``university_name`` and ``scrape_url``
    may be supplied by the orchestrator directly; if omitted, they are pulled
    from the contextvar too.
    """
    from urllib.parse import urlparse as _up

    configured_aliases: list[str] = []
    extra_tokens: list[str] = []

    try:
        from app.services.scraper.config.context import get_uni_config
        cfg = get_uni_config()
        if cfg is not None:
            configured_aliases = list(
                cfg.extraction.course_name.university_aliases
            )
        if not university_name and getattr(cfg, "name", None):
            university_name = cfg.name
        if not scrape_url and getattr(cfg, "scrape_url", None):
            scrape_url = cfg.scrape_url
    except Exception:
        pass

    if scrape_url:
        try:
            host = _up(scrape_url).netloc.lower().lstrip("www.")
            short = host.split(".")[0]
            if short and len(short) >= 2:
                extra_tokens.append(short)
        except Exception:
            pass

    return clean_course_name(
        name,
        university_name=university_name,
        aliases=[*aliases, *configured_aliases],
        extra_tokens=extra_tokens,
    )
