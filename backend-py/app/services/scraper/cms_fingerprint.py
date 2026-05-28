"""Detect the CMS / front-end family of a university course page.

Why this exists
---------------
We have ~43 hand-tuned per-university YAML configs and need to scale to
1500+ universities without writing a YAML by hand for each one. The
single best signal for "which existing YAML is most likely to work on
this new uni?" is the CMS / framework powering its course pages —
Federation's NextJS pages look NOTHING like UNSW's Drupal pages, but
they look A LOT like every other NextJS-driven uni in the catalogue.

This module takes a single HTML body (typically the listing or
homepage) and returns one or more family tags ranked by confidence.
Heuristics only — never makes a network call, never raises, never
mutates state. Safe to run on any HTML payload.

Family tags
-----------
- ``nextjs``         — Vercel/NextJS (`__NEXT_DATA__`, `_next/static`)
- ``wordpress``      — WP (`wp-content`, `wp-json`, `wpemoji`)
- ``drupal``         — Drupal 7-10 (`Drupal.settings`, `drupal-settings-json`)
- ``wagtail``        — Wagtail/Django (`<meta name="generator" content="Wagtail">`)
- ``sitecore``       — Sitecore (`scwebeditinput`, `sc_site=`)
- ``terminalfour``   — TERMINALFOUR (`terminalfour`, `T4 CMS`)
- ``adobe_aem``      — Adobe Experience Manager (`/etc.clientlibs/`, `cq-`)
- ``squarespace``    — Squarespace (`Static.SQUARESPACE_CONTEXT`)
- ``wix``            — Wix (`wix-bolt`, `static.parastorage.com`)
- ``react_spa``      — generic React SPA (`react-dom`, `id="root"`, no NextJS)
- ``angular_spa``    — Angular (`ng-version`, `_nghost-`)
- ``generic``        — falls back when nothing matches

The function is cheap (substring scans, no parsing) so it's safe to run
inside the cascade loop on every candidate page.
"""
from __future__ import annotations

import re
from typing import Iterable

# Each tuple is (family_tag, list-of-marker-substrings, confidence_weight).
# Markers are case-sensitive substring checks against the raw HTML.
# Higher-weight markers reflect uniqueness — `__NEXT_DATA__` is essentially
# diagnostic of NextJS, while `react-dom` shows up in many flavours of SPA.
_FAMILY_MARKERS: list[tuple[str, list[str], int]] = [
    ("nextjs",        ["__NEXT_DATA__", "/_next/static/"],                                   10),
    ("wordpress",     ["/wp-content/", "/wp-json/", "wpemoji", "wp-includes/"],               10),
    ("drupal",        ["drupal-settings-json", "Drupal.settings", "/sites/default/files/"],   10),
    ("wagtail",       ['name="generator" content="Wagtail"', "/static/wagtail"],              10),
    ("sitecore",      ["scWebEditInput", "sc_site=", "/sitecore/shell/"],                     10),
    ("terminalfour",  ["terminalfour", "T4 CMS", "T4Skins"],                                  10),
    ("adobe_aem",     ["/etc.clientlibs/", 'data-sly-', "cq-Editable"],                       10),
    ("squarespace",   ["Static.SQUARESPACE_CONTEXT", "static.squarespace.com"],               10),
    ("wix",           ["wix-bolt", "static.parastorage.com", "wix-warmup-data"],              10),
    ("angular_spa",   ["ng-version=", "_nghost-", "_ngcontent-"],                              8),
    # React SPA: only attribute *after* explicit NextJS check fails — the
    # cascade loop below subtracts NextJS hits before reporting react_spa.
    ("react_spa",     ["react-dom", 'id="root"', "data-reactroot"],                             6),
]

# Lowercase TLD/region tag — used as a secondary ranking signal (e.g.
# Australian unis prefer Australian-template YAMLs because of locale-
# specific intake names like "Autumn Session" → March).
_REGION_TLD_TO_REGION = {
    "edu.au": "au",
    "ac.nz":  "nz",
    "ac.uk":  "uk",
    "edu":    "us",
    "edu.sg": "sg",
    "ac.in":  "in",
    "edu.my": "my",
    "ac.za":  "za",
    "edu.ph": "ph",
}


def fingerprint(html: str) -> dict[str, int]:
    """Return ``{family_tag: hit_score}`` for every family with at least one hit.

    The score is the **sum of the confidence weights of the markers that
    matched**, so a page with two NextJS markers scores 20 while a page
    with one weak React marker scores 6. Empty result means the page
    looks like generic static HTML.

    Never raises — safe on empty / binary / malformed input.
    """
    if not html:
        return {}
    out: dict[str, int] = {}
    for family, markers, weight in _FAMILY_MARKERS:
        score = 0
        for m in markers:
            if m in html:
                score += weight
        if score:
            out[family] = score

    # Disambiguate generic React from NextJS: NextJS pages always include
    # react-dom too, but we don't want to double-count React when the
    # primary signal is NextJS. Demote react_spa when nextjs already won.
    if "nextjs" in out and "react_spa" in out:
        del out["react_spa"]

    return out


def primary_family(html: str) -> str:
    """Return the highest-scoring family, or ``"generic"`` when no marker hit.

    Stable tie-break: when two families tie, the one declared earlier in
    :data:`_FAMILY_MARKERS` wins (NextJS > WordPress > Drupal > ...).
    """
    scores = fingerprint(html)
    if not scores:
        return "generic"
    # Stable tie-break by insertion order in _FAMILY_MARKERS:
    order = {fam: i for i, (fam, _, _) in enumerate(_FAMILY_MARKERS)}
    return max(scores.items(), key=lambda kv: (kv[1], -order.get(kv[0], 999)))[0]


def region_for_hostname(hostname: str) -> str:
    """Return a coarse region tag from a hostname, or ``"global"``.

    Used by the cascade ranker to prefer same-region candidates (an
    Australian uni's YAML has AU-specific intake/season mappings that
    are useless on a UK uni and vice-versa).
    """
    if not hostname:
        return "global"
    h = hostname.lower().lstrip(".")
    for tld, region in _REGION_TLD_TO_REGION.items():
        if h.endswith("." + tld) or h == tld:
            return region
    # Single-label TLDs (.edu, .ac.uk handled above already).
    if h.endswith(".edu"):
        return "us"
    return "global"


def url_shape_signature(url: str) -> dict[str, object]:
    """Return a small bag of URL-shape features the cascade ranker scores on.

    Two unis with the same listing-URL shape are MORE likely to share a
    YAML than two unis with very different shapes. We capture a small
    set of shape features (path depth, presence of ``/courses/`` /
    ``/programs/`` / ``/study/``, query keys) so the ranker can compare
    by Jaccard / equality without doing expensive URL clustering.
    """
    if not url:
        return {"depth": 0, "segments": frozenset(), "query_keys": frozenset()}
    try:
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(url)
    except (ValueError, AttributeError):
        return {"depth": 0, "segments": frozenset(), "query_keys": frozenset()}
    path = (parsed.path or "").strip("/")
    segs = [s for s in path.split("/") if s]
    # Keep only stable structural segments — drop anything that looks
    # like a slug (>20 chars / contains digits / contains a dash).
    structural = frozenset(
        s.lower() for s in segs
        if len(s) <= 20 and "-" not in s and not re.search(r"\d", s)
    )
    query_keys = frozenset((parse_qs(parsed.query) or {}).keys())
    return {
        "depth": len(segs),
        "segments": structural,
        "query_keys": query_keys,
    }


def shape_similarity(a: dict, b: dict) -> float:
    """Symmetric 0..1 similarity between two :func:`url_shape_signature` dicts.

    Uses Jaccard for the segment + query-key sets and a depth-difference
    penalty (capped). Conservative — returns 0.0 on bad input rather
    than raising.
    """
    if not a or not b:
        return 0.0
    a_segs = a.get("segments") or frozenset()
    b_segs = b.get("segments") or frozenset()
    if not a_segs and not b_segs:
        seg_jacc = 1.0
    else:
        union = a_segs | b_segs
        seg_jacc = len(a_segs & b_segs) / len(union) if union else 1.0
    a_qk = a.get("query_keys") or frozenset()
    b_qk = b.get("query_keys") or frozenset()
    if not a_qk and not b_qk:
        qk_jacc = 1.0
    else:
        union = a_qk | b_qk
        qk_jacc = len(a_qk & b_qk) / len(union) if union else 1.0
    a_depth = int(a.get("depth", 0) or 0)
    b_depth = int(b.get("depth", 0) or 0)
    depth_pen = min(abs(a_depth - b_depth), 5) / 5.0  # 0 (same) .. 1 (5+ apart)
    # Weighted average — segments are the strongest signal.
    return max(0.0, 0.6 * seg_jacc + 0.25 * qk_jacc + 0.15 * (1.0 - depth_pen))


def families_overlap(a: Iterable[str], b: Iterable[str]) -> int:
    """Count of CMS family tags shared between two iterables (0 = nothing in common)."""
    return len(set(a) & set(b))
