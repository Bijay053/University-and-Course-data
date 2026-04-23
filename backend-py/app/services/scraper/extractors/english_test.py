"""IELTS/PTE/TOEFL/Cambridge/Duolingo extractor — STUB.

Port from Node ``extractEnglishRequirement`` family. Each test should
emit its own ExtractionResult with field_key in
{ielts_overall, pte_overall, toefl_overall, cambridge_overall, duolingo_overall,
ielts_listening, ielts_speaking, ...}.
"""
from __future__ import annotations

from app.services.scraper.extractors.base import ExtractionResult


field_keys = (
    "ielts_overall",
    "pte_overall",
    "toefl_overall",
    "cambridge_overall",
    "duolingo_overall",
)


async def extract(html: str, url: str) -> list[ExtractionResult]:  # noqa: ARG001
    return []
