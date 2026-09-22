"""Evidence-backed status for academic and IELTS component requirements.

The status is deliberately separate from extracted values.  In particular, a
degree classification is useful admission evidence, but it is not a numeric
academic score and must never be converted to one.
"""
from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
from urllib.parse import urlparse


IELTS_COMPONENT_FIELDS = (
    "ielts_listening",
    "ielts_speaking",
    "ielts_writing",
    "ielts_reading",
)

_QUALIFICATION = re.compile(
    r"\b(?:bachelor(?:'s)?|master(?:'s)?|honours?|undergraduate|postgraduate)"
    r"(?:\s+(?:honours?|university|college))*\s+(?:degree|qualification|diploma|certificate)\b",
    re.I,
)
_CLASSIFICATION = re.compile(
    r"\b(?:2\s*[:.]\s*[12]|first[-\s]class|upper[-\s]second|lower[-\s]second|"
    r"second[-\s]class|credit(?:\s+average)?|distinction|merit)\b",
    re.I,
)
_ENTRY_CUE = re.compile(
    r"\b(?:entry|admission|applican(?:t|ts)|must\s+(?:have|hold)|"
    r"required?|minimum|normally\s+(?:have|hold)|equivalent)\b",
    re.I,
)
def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _valid_url(value: Any) -> str | None:
    raw = _text(value)
    try:
        parsed = urlparse(raw)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return raw


def _site_key(url: str) -> str:
    host = (urlparse(url).hostname or "").casefold().strip(".")
    labels = host.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in {
        "ac.uk", "co.uk", "com.au", "edu.au", "ac.nz", "co.nz",
    }:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _is_ai_method(method: str) -> bool:
    return bool(
        any(
            marker in method
            for marker in ("openai", "gemini", "llm", "anthropic", "claude")
        )
        or re.search(r"(?:^|[_.:-])ai(?:$|[_.:-])", method)
    )


def _evidence_for(
    evidence: Iterable[dict[str, Any]] | None,
    field: str,
    *,
    fallback_url: str | None,
    deterministic_only: bool = False,
    require_non_ai_method: bool = False,
) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for item in evidence or ():
        if not isinstance(item, dict) or item.get("field_key") != field:
            continue
        if item.get("decision_status") == "rejected":
            continue
        if str(item.get("method") or "").startswith("approved_row:"):
            continue
        method = str(item.get("method") or "").casefold()
        if require_non_ai_method and (
            not method
            or _is_ai_method(method)
        ):
            continue
        if deterministic_only:
            if not method or _is_ai_method(method):
                continue
            if not any(
                marker in method
                for marker in (
                    "regex", "heading", "table", "pdf", "dom", "css", "xpath",
                    "json", "structured", "central", "searchstax", "selector",
                    "static", "label", "entry_panel",
                )
            ):
                continue
        source_url = _valid_url(item.get("source_url") or fallback_url)
        snippet = _text(item.get("snippet") or item.get("value"))
        official_base = _valid_url(fallback_url)
        if (
            deterministic_only
            and source_url
            and official_base
            and _site_key(source_url) != _site_key(official_base)
        ):
            continue
        if source_url and snippet:
            found.append((source_url, snippet[:2000]))
    return found


def _score_token(value: Any) -> str | None:
    try:
        score = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None
    if not score.is_finite():
        return None
    return format(score.normalize(), "f")


def _score_pattern(value: Any) -> str | None:
    token = _score_token(value)
    if token is None:
        return None
    whole, dot, fraction = token.partition(".")
    if not dot or not fraction or set(fraction) == {"0"}:
        return rf"{re.escape(whole)}(?:\.0+)?"
    return rf"{re.escape(whole)}\.{re.escape(fraction)}0*"


def _component_evidence_matches(field: str, value: Any, snippet: str) -> bool:
    """Bind a component value to its named skill or an explicit all-band floor."""
    score = _score_pattern(value)
    if score is None:
        return False
    component = re.escape(field.removeprefix("ielts_"))
    text = _text(snippet)

    # Named skill: "listening: 6", "6.0 in listening", etc. Only permit
    # requirement words between the name and score; arbitrary prose windows can
    # accidentally cross-bind an overall value to a lower component value.
    named = (
        rf"\b{component}\b\s*(?:(?:minimum|at\s+least)\s+)?"
        rf"(?:(?:band|score)\s*)?(?:(?:of|is|must\s+be)\s*)?[:=-]?\s*"
        rf"\b{score}\b"
        rf"|\b{score}\b\s*(?:in|for)\s+(?:the\s+)?{component}\b"
    )
    if re.search(named, text, re.I):
        return True

    # A compact list may publish one value for specifically named skills:
    # "Listening, reading, writing and speaking: 6.0".
    grouped = re.search(
        rf"(?P<skills>\b(?:listening|reading|writing|speaking)\b"
        rf"(?:\s*(?:,|and|&)\s*\b(?:listening|reading|writing|speaking)\b)+)"
        rf"\s*(?:(?:minimum|at\s+least)\s+)?"
        rf"(?:(?:band|score)\s*)?[:=-]?\s*\b{score}\b",
        text,
        re.I,
    )
    if grouped and re.search(rf"\b{component}\b", grouped.group("skills"), re.I):
        return True

    # Uniform floor: "minimum 5.5 in each component", "each band 6", or
    # "no band less than 5.5". The score must be in the same bounded phrase.
    all_band = (
        rf"(?:minimum(?:\s+of)?|at\s+least)\s+{score}\b[^.;:]{{0,24}}"
        rf"\b(?:in\s+)?(?:each|every|all(?:\s+four)?)\s+(?:individual\s+)?"
        rf"(?:IELTS\s+)?(?:component|band|skill|section)s?\b"
        rf"|\b(?:each|every|all(?:\s+four)?)\s+(?:individual\s+)?"
        rf"(?:IELTS\s+)?(?:component|band|skill|section)s?\b"
        rf"[^.;:]{{0,24}}\b{score}\b"
        rf"|\bno\s+(?:individual\s+)?(?:component|band|skill|section)\s+"
        rf"(?:below\s+{score}\b|(?:less|lower)\s+than\s+{score}\b)"
    )
    return bool(re.search(all_band, text, re.I))


def is_bounded_qualification_requirement(text: Any) -> bool:
    """Accept only concise, admission-scoped, genuine qualification language."""
    normalized = _text(text)
    if not normalized or len(normalized) > 2000:
        return False
    return bool(
        _QUALIFICATION.search(normalized)
        and _ENTRY_CUE.search(normalized)
        and (
            _CLASSIFICATION.search(normalized)
            or re.search(r"\b(?:degree|qualification)\s+or\s+equivalent\b", normalized, re.I)
        )
    )


def _qualification_evidence_matches(value_text: str, evidence_text: str) -> bool:
    value_class = _CLASSIFICATION.search(value_text)
    evidence_class = _CLASSIFICATION.search(evidence_text)
    if value_class or evidence_class:
        if not (value_class and evidence_class):
            return False
        compact = lambda value: re.sub(r"[\s-]+", "", value.casefold())
        return compact(value_class.group(0)) == compact(evidence_class.group(0))
    # The only accepted classification-free form is "degree/qualification or
    # equivalent"; require that exact semantic phrase on both sides.
    equivalent = re.compile(r"\b(?:degree|qualification)\s+or\s+equivalent\b", re.I)
    return bool(equivalent.search(value_text) and equivalent.search(evidence_text))


def _academic_fingerprint(
    row: Any,
    *,
    source_url: str | None,
    requirement_text: str | None,
) -> str:
    values = {
        "academic_score": getattr(row, "academic_score", None),
        "score_type": _text(getattr(row, "score_type", None)) or None,
        "academic_level": _text(getattr(row, "academic_level", None)) or None,
        "other_requirement": _text(getattr(row, "other_requirement", None)) or None,
        "source_url": source_url,
        "requirement_text": requirement_text,
    }
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _english_fingerprint(row: Any, *, source_url: str | None) -> str:
    values = {
        "ielts_overall": getattr(row, "ielts_overall", None),
        **{field: getattr(row, field, None) for field in IELTS_COMPONENT_FIELDS},
        "source_url": source_url,
    }
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_requirement_status(
    row: Any,
    *,
    evidence: Iterable[dict[str, Any]] | None = None,
    source_url: str | None = None,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build status, preserving only still-valid explicit persisted proof."""
    previous = previous if isinstance(previous, dict) else {}
    previous_academic = previous.get("academic") if isinstance(previous.get("academic"), dict) else {}
    previous_english = (
        previous.get("englishComponents")
        if isinstance(previous.get("englishComponents"), dict)
        else {}
    )

    score = getattr(row, "academic_score", None)
    try:
        has_numeric = score is not None and float(score) > 0
    except (TypeError, ValueError):
        has_numeric = False

    requirement_text = _text(getattr(row, "other_requirement", None))
    academic: dict[str, Any]
    if has_numeric:
        academic = {"state": "numeric"}
    else:
        proof: tuple[str, str] | None = None
        if is_bounded_qualification_requirement(requirement_text):
            for proof_url, snippet in _evidence_for(
                evidence,
                "other_requirement",
                fallback_url=source_url,
                deterministic_only=True,
            ):
                # The cited evidence itself must contain the bounded requirement;
                # arbitrary prose plus an unrelated URL is not proof.
                if (
                    is_bounded_qualification_requirement(snippet)
                    and _qualification_evidence_matches(requirement_text, snippet)
                ):
                    proof = (proof_url, requirement_text[:2000])
                    break

        if proof:
            proof_url, proof_text = proof
            academic = {
                "state": "qualification_based",
                "sourceUrl": proof_url,
                "requirementText": proof_text,
            }
            academic["_fingerprint"] = _academic_fingerprint(
                row, source_url=proof_url, requirement_text=proof_text
            )
        elif previous_academic.get("state") == "qualification_based":
            proof_url = _valid_url(previous_academic.get("sourceUrl"))
            proof_text = _text(previous_academic.get("requirementText"))
            expected = _academic_fingerprint(
                row, source_url=proof_url, requirement_text=proof_text
            )
            if (
                proof_url
                and is_bounded_qualification_requirement(proof_text)
                and previous_academic.get("_fingerprint") == expected
            ):
                academic = dict(previous_academic)
            else:
                academic = {"state": "unverified" if requirement_text else "missing"}
        else:
            academic = {"state": "unverified" if requirement_text else "missing"}

    missing_fields = [
        field
        for field in IELTS_COMPONENT_FIELDS
        if not isinstance(getattr(row, field, None), (int, float))
        or getattr(row, field, None) <= 0
    ]
    overall = getattr(row, "ielts_overall", None)
    english: dict[str, Any]
    if overall is not None and missing_fields:
        english = {"state": "missing", "missingFields": missing_fields}
    elif overall is None:
        # Absence never proves that IELTS components are not required. There is
        # currently no narrow producer for explicit not_required proof, so even
        # a hand-crafted/hash-shaped persisted claim is unsupported.
        english = {"state": "unknown"}
    else:
        component_proofs: list[tuple[str, str]] = []
        for field in IELTS_COMPONENT_FIELDS:
            field_proofs = [
                item
                for item in _evidence_for(
                    evidence,
                    field,
                    fallback_url=source_url,
                    require_non_ai_method=True,
                )
                if _component_evidence_matches(field, getattr(row, field, None), item[1])
            ]
            if not field_proofs:
                # Some extractors emit one IELTS evidence row for a published
                # "each component" floor.  Accept it only when that exact cue and
                # the component value are both in the citation.
                field_proofs = [
                    item
                    for item in _evidence_for(
                        evidence,
                        "ielts_overall",
                        fallback_url=source_url,
                        require_non_ai_method=True,
                    )
                    if _component_evidence_matches(
                        field, getattr(row, field, None), item[1]
                    )
                ]
            if field_proofs:
                component_proofs.append(field_proofs[0])

        if len(component_proofs) == len(IELTS_COMPONENT_FIELDS):
            proof_url = component_proofs[0][0]
            english = {"state": "verified", "sourceUrl": proof_url}
            english["_fingerprint"] = _english_fingerprint(row, source_url=proof_url)
        elif previous_english.get("state") == "verified":
            proof_url = _valid_url(previous_english.get("sourceUrl"))
            expected = _english_fingerprint(row, source_url=proof_url)
            if proof_url and previous_english.get("_fingerprint") == expected:
                english = dict(previous_english)
            else:
                english = {"state": "unknown"}
        else:
            english = {"state": "unknown"}

    return {"academic": academic, "englishComponents": english}


def effective_requirement_status(row: Any) -> dict[str, Any]:
    """Resolve a stored status against current values, invalidating stale proof."""
    return build_requirement_status(
        row,
        previous=getattr(row, "requirement_status", None),
    )


def public_requirement_status(row: Any) -> dict[str, Any]:
    """Return the additive API contract without internal proof fingerprints."""
    status = effective_requirement_status(row)
    return {
        "academic": {
            key: value
            for key, value in status["academic"].items()
            if key in {"state", "sourceUrl", "requirementText"}
        },
        "englishComponents": {
            key: value
            for key, value in status["englishComponents"].items()
            if key in {"state", "missingFields", "sourceUrl"}
        },
    }