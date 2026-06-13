"""Recovery mapper — score extracted values against the target course.

Given a list of extraction results and the target course's degree_level +
course_name, scores each value by:
  (a) degree-level keyword in the heading/section near the value
  (b) URL path segment matching the course's level (/undergraduate/ etc.)
  (c) course name / subject area match in the evidence snippet

Returns the highest-scoring match per field with a mapping_reason string.

Degree-level disqualification (strict):
- A result is rejected if the snippet's nearest degree-level heading clearly
  signals a DIFFERENT level than the target course, regardless of the URL.
- A result is also rejected if the URL path signals a different level AND the
  snippet provides NO level signal at all (URL acts as tie-breaker only when
  the snippet is silent).
"""
from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)

# Degree-level keyword buckets used for matching.
_LEVEL_BUCKETS: dict[str, list[str]] = {
    "undergraduate": [
        "undergraduate", "bachelor", "honours", "diploma", "certificate",
        "associate", "foundation", "bridging",
    ],
    "postgraduate": [
        "postgraduate", "master", "mba", "graduate certificate",
        "graduate diploma", "doctor", "phd", "doctorate", "research",
    ],
    "research": [
        "phd", "doctorate", "doctor", "research", "mphil",
    ],
    "online": [
        "online", "distance", "external", "flexible delivery",
    ],
}

# URL path segments that signal degree level
_PATH_LEVEL_PATTERNS: dict[str, list[str]] = {
    "undergraduate": ["undergraduate", "ug", "bachelor"],
    "postgraduate": ["postgraduate", "pg", "graduate", "master", "doctoral"],
    "research": ["research", "phd", "doctoral"],
    "online": ["online", "distance"],
}


def _normalise_level(degree_level: str | None) -> str:
    """Map a degree_level value to one of our internal buckets."""
    if not degree_level:
        return "unknown"
    low = degree_level.lower()
    for bucket, kws in _LEVEL_BUCKETS.items():
        for kw in kws:
            if kw in low:
                return bucket
    return "unknown"


def _url_level_bucket(url: str) -> str | None:
    """Return the level bucket implied by the URL path, or None."""
    try:
        from urllib.parse import urlparse
        path = urlparse(url).path.lower()
        for bucket, segs in _PATH_LEVEL_PATTERNS.items():
            if any(s in path for s in segs):
                return bucket
    except Exception:
        pass
    return None


def _snippet_level_bucket(snippet: str | None) -> str | None:
    """Return the predominant level bucket found in the snippet text, or None."""
    if not snippet:
        return None
    low = snippet.lower()
    scores: dict[str, int] = {}
    for bucket, kws in _LEVEL_BUCKETS.items():
        hit = sum(1 for kw in kws if kw in low)
        if hit:
            scores[bucket] = hit
    if not scores:
        return None
    return max(scores, key=lambda k: scores[k])


def _score_result(
    result: dict[str, Any],
    target_level_bucket: str,
    course_name: str,
) -> tuple[int, str]:
    """Score a single extraction result. Returns (score, reason_string)."""
    reasons: list[str] = []
    score = 0

    url = result.get("source_url", "")
    snippet = result.get("snippet") or ""

    # (a) Snippet level match
    snip_bucket = _snippet_level_bucket(snippet)
    if snip_bucket:
        if snip_bucket == target_level_bucket:
            score += 3
            reasons.append(f"snippet matches {snip_bucket}")
        else:
            score -= 2
            reasons.append(f"snippet signals {snip_bucket} (target={target_level_bucket})")

    # (b) URL path level match
    url_bucket = _url_level_bucket(url)
    if url_bucket:
        if url_bucket == target_level_bucket:
            score += 2
            reasons.append(f"url path matches {url_bucket}")
        else:
            score -= 1
            reasons.append(f"url path signals {url_bucket} (target={target_level_bucket})")

    # (c) Course name / subject area hint in snippet
    if course_name and snippet:
        _STOPWORDS = {"of", "in", "and", "the", "for", "with", "a", "an", "to", "at"}
        name_words = [
            w.lower() for w in re.findall(r"\b\w+\b", course_name)
            if w.lower() not in _STOPWORDS and len(w) > 3
        ]
        hits = sum(1 for w in name_words if w in snippet.lower())
        if hits >= 2:
            score += 1
            reasons.append(f"{hits} course-name words in evidence")

    # (d) Confidence bonus
    conf = result.get("confidence")
    if conf is not None:
        try:
            conf_f = float(conf)
            if conf_f >= 0.8:
                score += 2
                reasons.append(f"high confidence ({conf_f:.0%})")
            elif conf_f >= 0.5:
                score += 1
                reasons.append(f"medium confidence ({conf_f:.0%})")
        except (TypeError, ValueError):
            pass

    reason_str = "; ".join(reasons) if reasons else "no matching signals"
    return score, reason_str


def _is_disqualified(
    result: dict[str, Any],
    target_level_bucket: str,
) -> tuple[bool, str]:
    """Return (True, reason) if the result is definitively wrong for this course.

    Disqualification rules (strict):
    1. If the snippet contains a clear degree-level signal for a DIFFERENT level,
       reject immediately — snippet is the strongest signal.
    2. If the snippet has NO level signal but the URL path signals a different
       level, reject — URL acts as fallback when snippet is silent.
    3. If both or neither signal — not disqualified (may still score poorly).
    """
    if target_level_bucket == "unknown":
        return False, ""

    snippet = result.get("snippet") or ""
    url = result.get("source_url", "")

    snip_bucket = _snippet_level_bucket(snippet)
    url_bucket = _url_level_bucket(url)

    # Rule 1: snippet clearly indicates a different level (primary signal)
    if snip_bucket and snip_bucket != target_level_bucket:
        reason = (
            f"snippet nearest heading signals {snip_bucket!r} "
            f"(target level is {target_level_bucket!r})"
        )
        return True, reason

    # Rule 2: URL signals wrong level and snippet gives no level signal at all
    if url_bucket and url_bucket != target_level_bucket and not snip_bucket:
        reason = (
            f"url path indicates {url_bucket!r} and snippet has no level signal "
            f"(target level is {target_level_bucket!r})"
        )
        return True, reason

    return False, ""


def map_results_to_course(
    results: list[dict[str, Any]],
    *,
    degree_level: str | None,
    course_name: str | None,
    return_rejects: bool = False,
) -> "dict[str, dict[str, Any]] | tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]":
    """Select the best recovery result per field for the target course.

    Parameters
    ----------
    results:
        Extraction results from extractor.extract_from_url().
    degree_level:
        The target course's degree_level (e.g. "Bachelor's", "Master's").
    course_name:
        The target course's name for subject-area matching.
    return_rejects:
        If True, return a tuple (accepted, rejected) where rejected maps
        field → list of dicts with keys: reason, source_url, value.

    Returns
    -------
    dict mapping field_name → best_result_dict (with mapping_reason added).
    When return_rejects=True, returns (accepted_dict, rejected_dict).
    """
    target_bucket = _normalise_level(degree_level)
    cname = (course_name or "").strip()

    best: dict[str, tuple[int, dict[str, Any]]] = {}
    rejects: dict[str, list[dict[str, Any]]] = {}

    for result in results:
        field = result.get("field")
        if not field:
            continue

        val = result.get("value")
        if val is None:
            log.debug(
                "[RECOVERY:map] field=%r source=%r — value is None, skipping",
                field, result.get("source_url"),
            )
            continue

        disqualified, disq_reason = _is_disqualified(result, target_bucket)
        if disqualified:
            log.info(
                "[RECOVERY:map] field=%r source=%r — REJECTED: %s",
                field, result.get("source_url"), disq_reason,
            )
            if return_rejects:
                rejects.setdefault(field, []).append({
                    "reason": disq_reason,
                    "source_url": result.get("source_url"),
                    "value": str(val)[:200] if val is not None else None,
                })
            continue

        score, reason = _score_result(result, target_bucket, cname)

        log.info(
            "[RECOVERY:map] field=%r source=%r — score=%d reason=%r",
            field, result.get("source_url"), score, reason,
        )

        prev_score, _ = best.get(field, (-999, {}))
        if score > prev_score:
            result_with_reason = dict(result)
            result_with_reason["mapping_reason"] = reason
            result_with_reason["mapping_score"] = score
            best[field] = (score, result_with_reason)

    accepted = {field: item for field, (_, item) in best.items()}
    if return_rejects:
        return accepted, rejects
    return accepted
