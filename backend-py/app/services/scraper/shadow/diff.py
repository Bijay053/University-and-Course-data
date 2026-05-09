"""Diff two sets of extraction results for shadow-mode equivalence checking.

Normalization contract
----------------------
Fields that are stripped before comparison (non-load-bearing):

  Timestamps:     scraped_at, staged_at, created_at, updated_at
  Run IDs:        id, scrape_job_id, course_id, run_id
  Cost / timing:  gemini_tokens, elapsed_ms, cost_usd
  Noise:          raw_html, raw_text (too large; not content)

Fields that MUST match exactly (load-bearing):

  Extracted content:  course_name, level, degree_level, duration, duration_term,
                      annual_tuition_fee, international_fee, domestic_fee,
                      currency, fee_term, ielts_overall, ielts_writing,
                      ielts_reading, ielts_speaking, ielts_listening,
                      pte_overall, toefl_ibt, cae_score, intake_months,
                      location, study_mode, category, sub_category,
                      course_website, domestic_only, page_title

  Provenance:         extraction_method JSONB (keys sorted, :null suffixes included)

  Staging decision:   staged (True/False), rejection_reason (if not staged)

  Course set:         no additions, no drops relative to old path

JSONB normalization: dict keys are sorted before comparison; list fields
(intake_months) are sorted. This makes the diff immune to JSONB insertion order.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


_IGNORE_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "scrape_job_id",
        "course_id",
        "run_id",
        "scraped_at",
        "staged_at",
        "created_at",
        "updated_at",
        "gemini_tokens",
        "elapsed_ms",
        "cost_usd",
        "raw_html",
        "raw_text",
    }
)

_SORT_LIST_KEYS: frozenset[str] = frozenset({"intake_months", "location"})


def _normalise_value(key: str, val: Any) -> Any:
    """Normalise a single payload value for stable comparison."""
    if key in _SORT_LIST_KEYS and isinstance(val, list):
        return sorted(str(v) for v in val)
    if key == "extraction_method" and isinstance(val, dict):
        return dict(sorted(val.items()))
    if isinstance(val, dict):
        return dict(sorted(val.items()))
    return val


def _normalise_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip non-load-bearing fields and normalise ordering."""
    return {
        k: _normalise_value(k, v)
        for k, v in payload.items()
        if k not in _IGNORE_PAYLOAD_KEYS
    }


# ---------------------------------------------------------------------------
# Result representation (what the orchestrator passes in)
# ---------------------------------------------------------------------------

@dataclass
class ExtractionResult:
    """Normalised view of one course's extraction result."""

    url: str
    name: str
    staged: bool
    rejection_reason: str | None
    payload: dict[str, Any]  # normalised, load-bearing fields only
    error: str | None = None

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "ExtractionResult":
        """Build from the dict returned by _extract_only() / extract_course()."""
        url = raw.get("url") or raw.get("course_website") or ""
        name = raw.get("name") or raw.get("course_name") or url
        error = raw.get("error")

        raw_payload: dict = raw.get("payload") or {}
        staged = not bool(error) and not raw_payload.get("domestic_only")
        rejection_reason = error or raw_payload.get("_rejection_reason")

        return cls(
            url=url,
            name=name,
            staged=staged,
            rejection_reason=rejection_reason,
            payload=_normalise_payload(raw_payload),
            error=error,
        )


# ---------------------------------------------------------------------------
# Diff report
# ---------------------------------------------------------------------------

@dataclass
class FieldDiff:
    field: str
    old_val: Any
    new_val: Any

    def as_dict(self) -> dict:
        return {"field": self.field, "old": self.old_val, "new": self.new_val}


@dataclass
class CourseDiff:
    url: str
    name: str
    field_diffs: list[FieldDiff]

    def as_dict(self) -> dict:
        return {
            "url": self.url,
            "name": self.name,
            "field_diffs": [fd.as_dict() for fd in self.field_diffs],
        }


@dataclass
class DiffReport:
    """Complete equivalence report for one shadow run."""

    matched: int = 0
    old_only: list[str] = field(default_factory=list)   # URLs staged by old, absent in new
    new_only: list[str] = field(default_factory=list)   # URLs staged by new, absent in old
    staging_disagreements: list[dict] = field(default_factory=list)  # staged vs skipped
    field_regressions: list[CourseDiff] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not (
            self.old_only
            or self.new_only
            or self.staging_disagreements
            or self.field_regressions
        )

    @property
    def summary(self) -> str:
        if self.is_clean:
            return f"CLEAN — {self.matched} courses matched exactly"
        parts = []
        if self.old_only:
            parts.append(f"{len(self.old_only)} old-only drops")
        if self.new_only:
            parts.append(f"{len(self.new_only)} new-only additions")
        if self.staging_disagreements:
            parts.append(f"{len(self.staging_disagreements)} staging disagreements")
        if self.field_regressions:
            n_fields = sum(len(c.field_diffs) for c in self.field_regressions)
            parts.append(f"{len(self.field_regressions)} courses with {n_fields} field diffs")
        return "DIFF — " + ", ".join(parts)

    def as_dict(self) -> dict:
        return {
            "is_clean": self.is_clean,
            "summary": self.summary,
            "matched": self.matched,
            "old_only": self.old_only,
            "new_only": self.new_only,
            "staging_disagreements": self.staging_disagreements,
            "field_regressions": [c.as_dict() for c in self.field_regressions],
        }


# ---------------------------------------------------------------------------
# Core diff function
# ---------------------------------------------------------------------------

def diff_staged_runs(
    old_results: list[dict[str, Any]],
    new_results: list[dict[str, Any]],
) -> DiffReport:
    """Compare two sets of raw extraction results and return a DiffReport.

    Args:
        old_results: list of dicts from _extract_only() for the old code path
        new_results: list of dicts from _extract_only() for the new code path

    Returns:
        DiffReport with is_clean=True iff outputs are semantically equivalent.

    The comparison is by course URL (join key). Courses that errored in both
    paths with the same error code are considered matched. Differences in error
    message wording are normalised out.
    """
    old_by_url: dict[str, ExtractionResult] = {}
    for r in old_results:
        er = ExtractionResult.from_raw(r)
        if er.url:
            old_by_url[er.url] = er

    new_by_url: dict[str, ExtractionResult] = {}
    for r in new_results:
        er = ExtractionResult.from_raw(r)
        if er.url:
            new_by_url[er.url] = er

    report = DiffReport()

    all_urls = set(old_by_url) | set(new_by_url)

    for url in sorted(all_urls):
        old = old_by_url.get(url)
        new = new_by_url.get(url)

        if old is None:
            report.new_only.append(url)
            continue
        if new is None:
            report.old_only.append(url)
            continue

        # Staging decision disagreement
        if old.staged != new.staged:
            report.staging_disagreements.append(
                {
                    "url": url,
                    "name": old.name,
                    "old_staged": old.staged,
                    "new_staged": new.staged,
                    "old_reason": old.rejection_reason,
                    "new_reason": new.rejection_reason,
                }
            )
            continue

        # Both errored — compare error codes only (not full messages)
        if old.error and new.error:
            old_code = (old.error or "").split(":")[0]
            new_code = (new.error or "").split(":")[0]
            if old_code == new_code:
                report.matched += 1
            else:
                report.field_regressions.append(
                    CourseDiff(
                        url=url,
                        name=old.name,
                        field_diffs=[FieldDiff("error", old.error, new.error)],
                    )
                )
            continue

        # Field-level comparison
        all_keys = set(old.payload) | set(new.payload)
        diffs: list[FieldDiff] = []
        for key in sorted(all_keys):
            ov = old.payload.get(key)
            nv = new.payload.get(key)
            if ov != nv:
                diffs.append(FieldDiff(key, ov, nv))

        if diffs:
            report.field_regressions.append(
                CourseDiff(url=url, name=old.name, field_diffs=diffs)
            )
        else:
            report.matched += 1

    return report
