"""Common extractor protocol + result dataclass."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ExtractionResult:
    field_key: str
    value: Any | None = None
    normalized: Any | None = None
    confidence: float = 0.0
    snippet: str | None = None
    method: str = "unknown"
    extras: dict[str, Any] = field(default_factory=dict)


class Extractor(Protocol):
    field_key: str

    async def extract(self, html: str, url: str) -> list[ExtractionResult]: ...
