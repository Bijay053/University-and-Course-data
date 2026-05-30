"""Phase 4B — Autonomous API Discovery: API response schema analyzer.

Given a ClassifiedAPI (api_type + sample_response), navigates to the first item
in the results array and maps response-field names to internal course-schema
fields using keyword heuristics and value-type inference.

Output is an ApiFieldMapping stored in auto_config under ``_field_mapping``.
The generic extractor (``generic_search_api.py``) reads this mapping to
translate API response fields into the staging layer's expected column names —
without any provider-specific code.

Field mapping key vocabulary
-----------------------------
Internal field       What we are looking for
---------------------------------------------------------------------------
course_name          course / programme / program title
url                  absolute or relative course detail page URL
degree_level         undergraduate / postgraduate / doctorate / etc.
fee_amount           international tuition fee (number or string with currency)
duration             e.g. "3 years", "18 months"
english_score        IELTS overall or equivalent
intake_months        comma-separated list of months, or date strings
study_mode           full-time / part-time / online / on-campus
location             city / campus name
description          long-form paragraph overview of the course
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# ── Internal schema fields ────────────────────────────────────────────────────
_INTERNAL_FIELDS = [
    "course_name",
    "url",
    "degree_level",
    "fee_amount",
    "duration",
    "english_score",
    "intake_months",
    "study_mode",
    "location",
    "description",
]

# ── Keyword rules ─────────────────────────────────────────────────────────────
# Each tuple: (pattern, internal_field, base_confidence)
# Patterns are matched against the full dot-path AND the leaf key name.
# Earlier rules in the list take precedence (break-on-first-match).
_NAME_RULES: list[tuple[re.Pattern, str, float]] = [
    # ─ course_name ──────────────────────────────────────────────────────────
    (re.compile(r"course[_\s]?(?:name|title)|programme[_\s]?(?:name|title)|program[_\s]?(?:name|title)", re.I), "course_name", 0.90),
    (re.compile(r"\btitle\b|\bheading\b|\bcoursename\b|\bprogrammename\b", re.I), "course_name", 0.75),
    (re.compile(r"\bname\b", re.I), "course_name", 0.55),

    # ─ url ──────────────────────────────────────────────────────────────────
    (re.compile(r"course[_\s]?url|programme[_\s]?url|program[_\s]?url|course[_\s]?link|permalink", re.I), "url", 0.90),
    (re.compile(r"\burl\b|\blink\b|\bhref\b|\bpath\b", re.I), "url", 0.70),

    # ─ fee_amount ────────────────────────────────────────────────────────────
    (re.compile(r"tuition[_\s]?fee|intl[_\s]?fee|international[_\s]?fee|fee[_\s]?international|fee[_\s]?amount|fee[_\s]?usd|fee[_\s]?gbp|fee[_\s]?aud", re.I), "fee_amount", 0.95),
    (re.compile(r"\btuition\b|\bfee\b|\bcost\b|\bprice\b", re.I), "fee_amount", 0.65),

    # ─ english_score ─────────────────────────────────────────────────────────
    (re.compile(r"ielts|english[_\s]?(?:score|requirement|test|band)|language[_\s]?(?:score|requirement)", re.I), "english_score", 0.95),

    # ─ duration ──────────────────────────────────────────────────────────────
    (re.compile(r"duration[_\s]?(?:year|month|week)?|course[_\s]?length|study[_\s]?length|length[_\s]?of[_\s]?(?:course|study)", re.I), "duration", 0.85),
    (re.compile(r"\bduration\b|\blength\b|\byears?\b", re.I), "duration", 0.60),

    # ─ degree_level ──────────────────────────────────────────────────────────
    (re.compile(r"degree[_\s]?(?:level|type)|study[_\s]?level|level[_\s]?of[_\s]?study|qualification[_\s]?(?:type|level)|award[_\s]?type", re.I), "degree_level", 0.90),
    (re.compile(r"\blevel\b|\bdegree\b|\bqualification\b|\baward\b", re.I), "degree_level", 0.65),

    # ─ intake_months ─────────────────────────────────────────────────────────
    (re.compile(r"intake|commencement|start[_\s]?date|entry[_\s]?date|semester|session[_\s]?start", re.I), "intake_months", 0.85),

    # ─ study_mode ────────────────────────────────────────────────────────────
    (re.compile(r"delivery[_\s]?mode|study[_\s]?mode|attendance[_\s]?(?:mode|type)|mode[_\s]?of[_\s]?(?:study|delivery|attendance)", re.I), "study_mode", 0.90),
    (re.compile(r"\bmode\b|\battendance\b", re.I), "study_mode", 0.60),

    # ─ location ──────────────────────────────────────────────────────────────
    (re.compile(r"\bcampus(?:es)?\b|\blocation(?:s)?\b|\bcity\b|\bsite\b", re.I), "location", 0.75),

    # ─ description ───────────────────────────────────────────────────────────
    (re.compile(r"overview|summary|description|synopsis|about|body|intro", re.I), "description", 0.80),
]

_HIGH_VALUE_FIELDS = {"course_name", "url", "degree_level", "fee_amount", "duration"}
_MIN_MAP_CONFIDENCE: float = 0.45


@dataclass
class ApiFieldMapping:
    """Mapping from API response fields to internal course schema fields."""

    # Internal field → dot-path into API response item (e.g. "title", "_source.name")
    field_mapping: dict[str, str] = field(default_factory=dict)
    # Dot-path to the results array in the full API response
    results_path: str = ""
    api_type: str = ""
    # Fraction of _HIGH_VALUE_FIELDS mapped with confidence ≥ 0.65
    overall_confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "field_mapping": self.field_mapping,
            "results_path": self.results_path,
            "api_type": self.api_type,
            "overall_confidence": self.overall_confidence,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ApiFieldMapping":
        return cls(
            field_mapping=d.get("field_mapping") or {},
            results_path=d.get("results_path") or "",
            api_type=d.get("api_type") or "",
            overall_confidence=d.get("overall_confidence") or 0.0,
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _navigate_results_path(results_path: str, body: Any) -> Any:
    """Follow dot-notation path into body; return None if not navigable."""
    node: Any = body
    if not results_path:
        return node
    for key in results_path.split("."):
        if isinstance(node, dict):
            node = node.get(key)
        else:
            return None
    return node


def _first_item(api_type: str, results_path: str, body: Any) -> dict | None:
    """Return the first dict item from the results array, or None."""
    arr = _navigate_results_path(results_path, body)
    if isinstance(arr, list):
        items = [x for x in arr if isinstance(x, dict)]
        return items[0] if items else None
    return None


def _flatten(obj: dict, prefix: str = "", depth: int = 0) -> list[tuple[str, Any]]:
    """Flatten nested dict to (dot-path, value) pairs (max 3 levels deep)."""
    result: list[tuple[str, Any]] = []
    for k, v in obj.items():
        path = f"{prefix}.{k}" if prefix else k
        result.append((path, v))
        if depth < 2 and isinstance(v, dict):
            result.extend(_flatten(v, path, depth + 1))
    return result


def _is_url_value(value: Any) -> bool:
    return isinstance(value, str) and (
        value.startswith("http") or value.startswith("/")
    )


def _is_numeric(value: Any) -> bool:
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        return bool(re.match(r"^\s*[\d,\s$£€A$NZ$]+\s*$", value))
    return False


def _is_degree_level_value(value: Any) -> bool:
    return isinstance(value, str) and bool(
        re.search(r"under|post|bachelor|master|phd|doctor|diploma|certif", value, re.I)
    )


# ── Public API ────────────────────────────────────────────────────────────────

def analyze_schema(classified_api: "ClassifiedAPI") -> ApiFieldMapping:  # type: ignore[name-defined]
    """Map API response fields to internal course schema.

    Parameters
    ----------
    classified_api:
        Output of ``api_classifier.classify_capture()``.

    Returns
    -------
    ApiFieldMapping
        May have ``overall_confidence == 0`` if fewer than 2 high-value fields
        were confidently mapped — the caller should skip storing it.
    """
    from .api_classifier import ClassifiedAPI  # local import to avoid cycles

    if not isinstance(classified_api, ClassifiedAPI):
        return ApiFieldMapping()

    body = classified_api.sample_response
    results_path = classified_api.results_path

    item = _first_item(classified_api.api_type, results_path, body)
    if item is None:
        log.debug(
            "[SCHEMA_ANALYZER] No result item found for %s",
            classified_api.endpoint_url[:60],
        )
        return ApiFieldMapping(
            api_type=classified_api.api_type,
            results_path=results_path,
        )

    all_paths = _flatten(item)

    # field_scores[internal_field] = [(api_path, confidence), ...]
    field_scores: dict[str, list[tuple[str, float]]] = {f: [] for f in _INTERNAL_FIELDS}

    for api_path, value in all_paths:
        leaf = api_path.rsplit(".", 1)[-1]
        matched = False

        for pattern, internal_field, base_conf in _NAME_RULES:
            if pattern.search(leaf) or pattern.search(api_path):
                conf = base_conf

                # Value-type boosts
                if internal_field == "url" and _is_url_value(value):
                    conf = min(conf + 0.10, 1.0)
                elif internal_field == "fee_amount" and _is_numeric(value):
                    conf = min(conf + 0.10, 1.0)
                elif internal_field == "description" and isinstance(value, str) and len(value) > 200:
                    conf = min(conf + 0.15, 1.0)
                elif internal_field == "degree_level" and _is_degree_level_value(value):
                    conf = min(conf + 0.10, 1.0)

                field_scores[internal_field].append((api_path, conf))
                matched = True
                break

        # Value-type fallback for unmatched paths
        if not matched:
            if _is_url_value(value):
                field_scores["url"].append((api_path, 0.40))
            elif isinstance(value, str) and len(value) > 400:
                field_scores["description"].append((api_path, 0.40))

    # For each internal field, pick the best unmapped API path
    mapping: dict[str, str] = {}
    confident_high_value: int = 0
    used_paths: set[str] = set()

    for internal_field in _INTERNAL_FIELDS:
        candidates = sorted(
            [(p, s) for p, s in field_scores[internal_field] if p not in used_paths],
            key=lambda x: x[1],
            reverse=True,
        )
        if candidates and candidates[0][1] >= _MIN_MAP_CONFIDENCE:
            best_path, best_conf = candidates[0]
            mapping[internal_field] = best_path
            used_paths.add(best_path)
            if internal_field in _HIGH_VALUE_FIELDS and best_conf >= 0.65:
                confident_high_value += 1
            log.debug(
                "[SCHEMA_ANALYZER] %s → %r (conf=%.2f)",
                internal_field,
                best_path,
                best_conf,
            )

    overall_confidence = round(confident_high_value / len(_HIGH_VALUE_FIELDS), 2)

    result = ApiFieldMapping(
        field_mapping=mapping,
        results_path=results_path,
        api_type=classified_api.api_type,
        overall_confidence=overall_confidence,
    )

    log.info(
        "[SCHEMA_ANALYZER] %s → %d/%d fields mapped, high-value conf=%.0f%%",
        classified_api.endpoint_url[:60],
        len(mapping),
        len(_INTERNAL_FIELDS),
        overall_confidence * 100,
    )
    return result
