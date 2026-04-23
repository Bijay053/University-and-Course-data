"""International eligibility extractor.

Detects whether a course is open to international students based on the
page text. Mirrors the Node ``extractInternationalSection`` heuristic:
positive keywords ("international students may apply", "CRICOS",
"open to international") win; explicit negatives ("not available to
international", "domestic only") flip the answer to False.
"""
from __future__ import annotations

import re

from app.services.scraper.extractors._text import html_to_text
from app.services.scraper.extractors.base import ExtractionResult

_POS = re.compile(
    r"\b(?:international\s+students?\s+(?:may\s+apply|are\s+welcome|eligible|can\s+apply|enrol)"
    r"|open\s+to\s+international(?:\s+students?)?"
    r"|cricos\s+(?:code|registered)?\s*[:#]?\s*\d{6}[a-z]?"
    r"|available\s+to\s+international\s+students?"
    r"|international\s+student\s+(?:fees?|tuition))\b",
    re.I,
)
_NEG = re.compile(
    r"\b(?:not\s+available\s+to\s+international(?:\s+students?)?"
    r"|domestic\s+(?:students?\s+)?only"
    r"|australian\s+citizens?\s+only"
    r"|home\s+students?\s+only"
    r"|no\s+international\s+(?:applications?|enrolments?))\b",
    re.I,
)


async def extract(html: str, url: str) -> list[ExtractionResult]:  # noqa: ARG001
    if not html:
        return []
    text = html_to_text(html)
    if not text:
        return []

    neg_match = _NEG.search(text)
    if neg_match:
        return [
            ExtractionResult(
                field_key="international_eligible",
                value=False,
                normalized={"international_eligible": False, "eligibility_status": "rejected"},
                confidence=0.9,
                method="eligibility.negative",
                snippet=neg_match.group(0),
            )
        ]

    pos_match = _POS.search(text)
    if pos_match:
        return [
            ExtractionResult(
                field_key="international_eligible",
                value=True,
                normalized={"international_eligible": True, "eligibility_status": "approved"},
                confidence=0.85,
                method="eligibility.positive",
                snippet=pos_match.group(0),
            )
        ]

    return []
