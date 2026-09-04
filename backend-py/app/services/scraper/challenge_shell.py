"""Anti-bot challenge-shell detection — no application dependencies.

A "challenge shell" is a page returned by an anti-bot service (Cloudflare,
Imperva/Incapsula, etc.) instead of the actual course content the scraper
requested.  These pages contain no course data so they must not be counted
as successful browser rescues.

This module is kept intentionally free of application imports so it can be
imported and tested in isolation without triggering the pydantic / gemini
model-compilation chain.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Pattern registry
# ---------------------------------------------------------------------------
# Conservative by design: only vendor-specific markers that *cannot* appear on
# a legitimate university course page are included.  Adding patterns that
# match common English phrases risks false positives and should be avoided.
# Only the first _CHALLENGE_SHELL_SAMPLE bytes are scanned for speed — all
# challenge markers appear in the HTML <head> block.

_CHALLENGE_SHELL_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Cloudflare spinner: <title>Just a moment...</title>
    re.compile(r"<title[^>]*>\s*just a moment", re.IGNORECASE),
    # Cloudflare "Attention Required! | Cloudflare" error page (HTTP 1020)
    re.compile(r"<title[^>]*>attention required", re.IGNORECASE),
    # Cloudflare JS challenge variable — injected only on managed-challenge pages.
    # The leading "window." is required so a bare "__cf_chl_opt" variable name in
    # a comment or identifier does not trigger a false positive.
    re.compile(r"window\.__cf_chl_opt\b"),
    # Cloudflare challenge form (the POST target for the JS/CAPTCHA response)
    re.compile(r'\bid=["\']challenge-form["\']'),
    # Cloudflare Turnstile widget element
    re.compile(r'\bclass=["\'][^"\']*\bcf-turnstile\b'),
    # Imperva / Incapsula bot-protection interstitial
    re.compile(r"\b_Incapsula_Resource\b"),
)

_F5_COOKIE_ASSIGNMENT_PATTERN = re.compile(
    r"\bdocument\.cookie\s*=\s*[\"'][^\"']*\bcookiesession\d+=",
    re.IGNORECASE,
)
_F5_EVAL_PATTERN = re.compile(r"\beval\s*\(", re.IGNORECASE)
_F5_NETWORK_PATTERN = re.compile(
    r"\b(?:XMLHttpRequest|setTimeout)\b",
    re.IGNORECASE,
)
_PACKED_SCRIPT_PATTERN = re.compile(
    r"eval\s*\(\s*function\s*\(\s*p\s*,\s*a\s*,\s*c\s*,\s*k\s*,\s*e\s*,\s*d\s*\)",
    re.IGNORECASE,
)

_CHALLENGE_SHELL_SAMPLE = 4096  # chars; challenge markers are always in <head>


def is_challenge_shell(html: str) -> bool:
    """Return True when *html* is an anti-bot challenge interstitial rather
    than a real course page.

    Checks for Cloudflare "Just a moment…" spinners and managed-challenge
    pages, Cloudflare Turnstile widgets, and Imperva/Incapsula bot-protection
    interstitials.  Only the first :data:`_CHALLENGE_SHELL_SAMPLE` bytes are
    scanned; all markers appear in the ``<head>`` block.

    Conservative by design: only vendor-specific markers that *cannot* appear
    on a legitimate university course page are matched.  A ``False`` return
    does NOT certify the page is a valid course page — it simply means none of
    the known challenge signatures were found.
    """
    if not html:
        return False
    sample = html[:_CHALLENGE_SHELL_SAMPLE]
    if any(pat.search(sample) for pat in _CHALLENGE_SHELL_PATTERNS):
        return True
    if (
        _F5_COOKIE_ASSIGNMENT_PATTERN.search(sample)
        and _F5_EVAL_PATTERN.search(sample)
        and _F5_NETWORK_PATTERN.search(sample)
    ):
        return True
    if _PACKED_SCRIPT_PATTERN.search(sample):
        # Protected domains can return a complete-looking 200 document whose
        # body contains only a Dean-Edwards-packed challenge script. The same
        # packer may legitimately appear on real pages, so require the document
        # to have no visible content after scripts/styles/tags are removed.
        visible = re.sub(
            r"(?is)<(?:script|style)\b[^>]*>.*?</(?:script|style)>",
            " ",
            sample,
        )
        visible = re.sub(r"(?s)<[^>]+>", " ", visible)
        if not visible.strip():
            return True
    return False
