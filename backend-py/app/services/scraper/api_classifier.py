"""Phase 4B — Autonomous API Discovery: API type classifier.

Classifies XhrCapture objects into one of the supported API types by examining
the endpoint URL, response-body shape, and request headers.

Supported types
---------------
* ``algolia``       — Algolia search-as-a-service (*.algolia.net, hits[] + nbHits)
* ``elasticsearch`` — Elastic/OpenSearch (/_search, hits.hits[])
* ``solr``          — Apache Solr (/select?wt=json, response.docs[])
* ``searchstax``    — SearchStax-hosted Solr (*.searchstax.com)
* ``graphql``       — GraphQL endpoint (/graphql, body has data.{})
* ``rest_json``     — Generic REST JSON (top-level array or object with list)

The result of ``classify_captures()`` is stored on ``SiteProfile.xhr_api`` and
then fed to ``api_schema_analyzer.analyze_schema()`` to produce a field mapping.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# ── URL pattern matchers ──────────────────────────────────────────────────────
_SEARCHSTAX_URL_RE = re.compile(r"searchstax\.com", re.I)
_ALGOLIA_URL_RE = re.compile(
    r"algolia(?:net)?\.(?:com|net)|algolia\.io|\.algolia\.com", re.I
)
_ELASTIC_URL_RE = re.compile(
    r"elastic(?:search)?\.(?:co|io|com)|:9200\b|/_search\b", re.I
)
_SOLR_URL_RE = re.compile(r"/solr/|/select\?|[?&]wt=json", re.I)
_GRAPHQL_URL_RE = re.compile(r"/graphql\b|/api/graphql\b|/query\b", re.I)

# ── Consent / tracking / analytics domain blocklist ───────────────────────────
# APIs from these domains are never course APIs — skip them before classification
# so they cannot score above threshold even if they return a valid JSON shape.
#
# Patterns matched against the full URL (hostname + path):
#   usercentrics  — cookie consent management (api.usercentrics.eu/settings/...)
#   cookiebot     — cookie consent (consent.cookiebot.com/...)
#   onetrust      — consent platform (cdn.cookielaw.org, geolocation.onetrust.com)
#   cookielaw     — OneTrust CDN
#   analytics     — GA / Plausible / Matomo analytics endpoints
#   tracking / tracker — any tracking pixel / session recording API
#   gtm           — Google Tag Manager
#   segment.io    — Segment analytics
#   hotjar        — session recording
#   sentry        — error reporting
#   datadog       — APM
_CONSENT_TRACKING_BLOCKLIST_RE = re.compile(
    r"usercentrics\.eu|usercentrics\.com"
    r"|cookiebot\.com|cookielaw\.org|onetrust\.com"
    r"|google-analytics\.com|googletagmanager\.com|gtm\.js"
    r"|segment\.io|segment\.com/v1"
    r"|hotjar\.com|hj\.hotjar"
    r"|sentry\.io|browser\.sentry-cdn\.com"
    r"|datadoghq\.com|dd-rum"
    r"|[/?&]tracking\b|[/?&]tracker\b"
    r"|/analytics/|/collect\b|/beacon\b"
    r"|clarity\.ms|mxpnl\.com|mixpanel\.com",
    re.I,
)

# ── Course-URL presence heuristic ─────────────────────────────────────────────
# Boost rest_json candidates whose response body contains URL values that look
# like individual course/programme pages.  Downgrades configuration / consent /
# settings blobs that happen to be valid JSON arrays-of-objects.
_COURSE_URL_RE = re.compile(
    r"/(?:course|programme|program|study|degree|undergraduate|postgraduate"
    r"|masters?|bachelor|diploma|certificate|qualification)[s/\-]",
    re.I,
)


@dataclass
class ClassifiedAPI:
    """A classified XHR capture with confidence score."""

    api_type: str
    endpoint_url: str
    confidence: float           # 0.0–1.0
    sample_response: Any        # parsed JSON (may be large — only kept in memory)
    auth_hint: str = ""         # truncated auth header value for the caller
    results_path: str = ""      # JSON dot-path to the results array
    extra: dict[str, Any] = field(default_factory=dict)


# ── Per-type scorers ─────────────────────────────────────────────────────────

def _score_searchstax(url: str, body: Any) -> float:
    score = 0.0
    if _SEARCHSTAX_URL_RE.search(url):
        score += 0.80          # URL is definitive
    if isinstance(body, dict) and "response" in body:
        docs = (body.get("response") or {}).get("docs")
        if isinstance(docs, list):
            score += 0.15
        if "responseHeader" in body:
            score += 0.05
    return min(score, 1.0)


def _score_algolia(url: str, body: Any) -> float:
    score = 0.0
    if _ALGOLIA_URL_RE.search(url):
        score += 0.60
    if isinstance(body, dict):
        if isinstance(body.get("hits"), list):
            score += 0.20
        # nbHits + nbPages are Algolia-specific; each adds independently
        if "nbHits" in body:
            score += 0.20
        if "nbPages" in body:
            score += 0.10
        if "hitsPerPage" in body or "processingTimeMS" in body:
            score += 0.05
    return min(score, 1.0)


def _score_elasticsearch(url: str, body: Any) -> float:
    score = 0.0
    if _ELASTIC_URL_RE.search(url):
        score += 0.60
    if isinstance(body, dict):
        hits_obj = body.get("hits") or {}
        if isinstance(hits_obj, dict) and isinstance(hits_obj.get("hits"), list):
            # hits.hits nested shape is the primary ES fingerprint
            score += 0.35
        if "_shards" in body:
            # _shards adds independently — it co-occurs with hits in real responses
            score += 0.15
    return min(score, 1.0)


def _score_solr(url: str, body: Any) -> float:
    score = 0.0
    if _SOLR_URL_RE.search(url):
        score += 0.50
    if isinstance(body, dict) and "response" in body:
        docs = (body.get("response") or {}).get("docs")
        if isinstance(docs, list):
            score += 0.35
        if "responseHeader" in body:
            score += 0.15
    return min(score, 1.0)


def _score_graphql(url: str, body: Any) -> float:
    score = 0.0
    if _GRAPHQL_URL_RE.search(url):
        score += 0.70
    if isinstance(body, dict):
        # {"data": {...}} is a canonical GraphQL response shape
        if "data" in body and isinstance(body.get("data"), dict):
            score += 0.50
        # errors key also independently present in GraphQL responses
        if "errors" in body:
            score += 0.10
    return min(score, 1.0)


def _score_rest_json(url: str, body: Any) -> float:
    """Fallback: any JSON endpoint returning a list or object-with-array.

    A bonus is applied when the response body contains URL strings that look
    like individual course/programme pages — this prioritises real course APIs
    over configuration / consent / settings blobs that happen to be valid JSON.
    """
    base = 0.0
    if isinstance(body, list) and body and isinstance(body[0], dict):
        base = 0.55
    elif isinstance(body, dict):
        for v in body.values():
            if isinstance(v, list) and len(v) >= 2 and isinstance(v[0], dict):
                base = 0.50
                break
    if base == 0.0:
        return 0.0
    # Check first 10 items for URL-like string values matching course paths.
    sample_items: list[dict] = []
    if isinstance(body, list):
        sample_items = [x for x in body[:10] if isinstance(x, dict)]
    elif isinstance(body, dict):
        for v in body.values():
            if isinstance(v, list):
                sample_items = [x for x in v[:10] if isinstance(x, dict)]
                break
    course_url_hits = 0
    for item in sample_items:
        for val in item.values():
            if isinstance(val, str) and _COURSE_URL_RE.search(val):
                course_url_hits += 1
                break
    if course_url_hits >= 3:
        base = min(base + 0.20, 1.0)   # clear course URLs in response → boost
    elif course_url_hits == 0 and sample_items:
        base = max(base - 0.15, 0.0)   # no course URLs at all → slight penalty
    return base


# Evaluated in order; first type that beats the previous score wins.
# searchstax must come before solr (it is a Solr superset).
_CLASSIFIERS: list[tuple[str, Any]] = [
    ("searchstax",    _score_searchstax),
    ("algolia",       _score_algolia),
    ("elasticsearch", _score_elasticsearch),
    ("solr",          _score_solr),
    ("graphql",       _score_graphql),
    ("rest_json",     _score_rest_json),
]

_MIN_CONFIDENCE: float = 0.45


# ── Results-path resolver ─────────────────────────────────────────────────────

def _results_path_for(api_type: str, body: Any) -> str:
    """Return the JSON dot-path pointing to the results array."""
    if api_type == "algolia":
        return "hits"
    if api_type == "elasticsearch":
        return "hits.hits"
    if api_type in ("solr", "searchstax"):
        return "response.docs"
    if api_type == "graphql":
        if isinstance(body, dict):
            data = body.get("data") or {}
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, list):
                        return f"data.{k}"
        return "data"
    # rest_json: top-level array or object with first list
    if isinstance(body, list):
        return ""
    if isinstance(body, dict):
        for k, v in body.items():
            if isinstance(v, list) and len(v) >= 1 and isinstance(v[0], dict):
                return k
    return ""


# ── Public API ────────────────────────────────────────────────────────────────

def classify_capture(capture: "XhrCapture") -> ClassifiedAPI | None:  # type: ignore[name-defined]
    """Classify a single XHR capture. Returns None if confidence < threshold.

    Import of XhrCapture is deferred to avoid circular imports (the caller
    imports this module after importing xhr_interceptor).
    """
    from .xhr_interceptor import XhrCapture

    if not isinstance(capture, XhrCapture) or not capture.sample_body:
        return None

    # ── Consent / tracking / analytics blocklist ──────────────────────────────
    # These are never course APIs.  Reject immediately so they cannot score
    # above threshold even if they return a structurally valid JSON array.
    # Examples blocked: usercentrics.eu/settings/…, cookiebot.com/consent/…,
    # google-analytics.com/collect, gtm.js
    if _CONSENT_TRACKING_BLOCKLIST_RE.search(capture.url):
        log.info(
            "[API_CLASSIFIER] BLOCKED — consent/tracking/analytics URL skipped: %s",
            capture.url[:120],
        )
        return None

    best_type = ""
    best_score = 0.0
    for api_type, scorer in _CLASSIFIERS:
        score = scorer(capture.url, capture.sample_body)
        if score > best_score:
            best_score = score
            best_type = api_type

    if best_score < _MIN_CONFIDENCE or not best_type:
        log.debug(
            "[API_CLASSIFIER] %s — no confident match (best=%.2f)",
            capture.url[:80],
            best_score,
        )
        return None

    results_path = _results_path_for(best_type, capture.sample_body)
    log.info(
        "[API_CLASSIFIER] %s → type=%r confidence=%.2f results_path=%r",
        capture.url[:80],
        best_type,
        best_score,
        results_path,
    )
    return ClassifiedAPI(
        api_type=best_type,
        endpoint_url=capture.url,
        confidence=best_score,
        sample_response=capture.sample_body,
        results_path=results_path,
    )


def classify_captures(
    captures: list["XhrCapture"],  # type: ignore[name-defined]
) -> ClassifiedAPI | None:
    """Classify a list of captures and return the highest-confidence result.

    Parameters
    ----------
    captures:
        Output of ``xhr_interceptor.capture_xhr_signals()``.

    Returns
    -------
    ClassifiedAPI | None
        Best match, or None if no capture reaches the confidence threshold.
    """
    best: ClassifiedAPI | None = None
    for cap in captures:
        result = classify_capture(cap)
        if result is not None and (best is None or result.confidence > best.confidence):
            best = result
    if best:
        log.info(
            "[API_CLASSIFIER] best overall: type=%r endpoint=%s conf=%.2f",
            best.api_type,
            best.endpoint_url[:80],
            best.confidence,
        )
    return best
