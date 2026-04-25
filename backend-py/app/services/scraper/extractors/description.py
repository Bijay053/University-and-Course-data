"""Course description extractor.

Strategy (in priority order):
  1. <meta name="description"> / <meta property="og:description"> — most
     reliable single sentence, usually written by the university for SEO.
  2. First <p> inside the main heading block (h1 / h2 context).
  3. First substantive <p> inside <main> or <article>.
  4. First substantive <p> anywhere in the body.

A "substantive" paragraph is >= 40 chars and does not look like nav/footer
boilerplate (breadcrumbs, copyright lines, "Skip to content", etc.).

The result is truncated to 500 chars and trailing ellipsis added when
truncated.  Returns an empty list when nothing useful is found so the field
stays NULL rather than populating garbage.
"""
from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup

from .base import ExtractionResult

# ── Boilerplate patterns to reject ───────────────────────────────────────────
_BOILERPLATE = re.compile(
    r"(skip\s+to|cookie|privacy\s+policy|terms\s+(of|and)|copyright|all\s+rights\s+reserved"
    r"|breadcrumb|back\s+to\s+top|menu|navigation|search\s+results"
    r"|^\s*home\s*[›>/|]|^\s*\d+\s*$)",
    re.IGNORECASE,
)

_MIN_LEN = 40
_MAX_LEN = 500


def _clean(text: str) -> str:
    """Collapse whitespace and strip."""
    return re.sub(r"\s+", " ", text).strip()


def _ok(text: str) -> bool:
    """Return True if the candidate looks like a real description."""
    if len(text) < _MIN_LEN:
        return False
    if _BOILERPLATE.search(text):
        return False
    return True


def _truncate(text: str) -> str:
    if len(text) <= _MAX_LEN:
        return text
    return text[:_MAX_LEN].rsplit(" ", 1)[0] + "…"


async def extract(
    html: str,
    url: str = "",
    **_kwargs: Any,
) -> list[ExtractionResult]:
    """Return up to one ExtractionResult for the course description field."""
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")

    # 1. <meta name="description"> or og:description
    for attr, val in [("name", "description"), ("property", "og:description")]:
        tag = soup.find("meta", attrs={attr: val})
        if tag and tag.get("content"):
            text = _clean(str(tag["content"]))
            if _ok(text):
                return [
                    ExtractionResult(
                        field_key="description",
                        value=_truncate(text),
                        normalized={"description": _truncate(text)},
                        method="description.meta",
                        snippet=url,
                        confidence=0.95,
                    )
                ]

    # 2. First <p> near the primary heading
    h1 = soup.find(["h1", "h2"])
    if h1:
        for sib in h1.find_next_siblings():
            if sib.name in ("h1", "h2", "h3", "section", "main", "footer", "nav"):
                break
            if sib.name == "p":
                text = _clean(sib.get_text())
                if _ok(text):
                    return [
                        ExtractionResult(
                            field_key="description",
                            value=_truncate(text),
                            normalized={"description": _truncate(text)},
                            method="description.heading_p",
                            snippet=url,
                            confidence=0.80,
                        )
                    ]

    # 3. First substantive <p> inside <main> or <article>
    for container_tag in ("main", "article"):
        container = soup.find(container_tag)
        if container:
            for p in container.find_all("p"):
                text = _clean(p.get_text())
                if _ok(text):
                    return [
                        ExtractionResult(
                            field_key="description",
                            value=_truncate(text),
                            normalized={"description": _truncate(text)},
                            method="description.main_p",
                            snippet=url,
                            confidence=0.65,
                        )
                    ]

    # 4. First substantive <p> anywhere
    for p in soup.find_all("p"):
        text = _clean(p.get_text())
        if _ok(text):
            return [
                ExtractionResult(
                    field_key="description",
                    value=_truncate(text),
                    normalized={"description": _truncate(text)},
                    method="description.body_p",
                    snippet=url,
                    confidence=0.50,
                )
            ]

    return []
