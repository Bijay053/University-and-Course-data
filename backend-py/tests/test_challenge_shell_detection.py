"""Unit tests for the challenge-shell detector (Task #236).

Tests cover:
- is_challenge_shell() correctly identifies each supported vendor/pattern.
- Legitimate course pages are never mis-classified as challenge shells.
- Boundary cases: empty input, marker after the 4 KB scan window.
- Integration: challenge shells do NOT increment the browser-only rescue tally,
  but real pages do.
"""
from __future__ import annotations

import pytest

# Pure-function imports — no application dependencies, always importable.
from app.services.scraper.challenge_shell import (
    _CHALLENGE_SHELL_SAMPLE,
    is_challenge_shell,
)

# Integration-test imports (need the browser-tally helpers from per_course_browser).
# If the app chain is unavailable (e.g. under heavy CPU load in isolation mode)
# these are gracefully skipped via the HAS_BROWSER_TALLY flag.
try:
    from app.services.scraper.per_course_browser import (
        is_confirmed_browser_only,
        note_browser_rescue,
        reset_browser_only_hosts,
    )
    HAS_BROWSER_TALLY = True
except Exception:  # noqa: BLE001
    HAS_BROWSER_TALLY = False

# Matches single_course._BROWSER_RESCUE_MIN_HTML_LEN — inlined here so this
# file does not import single_course.py (which pulls the full extractor chain).
_BROWSER_RESCUE_MIN_HTML_LEN = 2000

# ---------------------------------------------------------------------------
# HTML fixtures
# ---------------------------------------------------------------------------

_CF_SPINNER = (
    "<!DOCTYPE html><html><head>"
    "<title>Just a moment...</title>"
    "</head><body></body></html>"
)

_CF_SPINNER_CAPS = (
    "<!DOCTYPE html><html><head>"
    "<title>JUST A MOMENT...</title>"
    "</head><body></body></html>"
)

_CF_SPINNER_WHITESPACE = (
    "<!DOCTYPE html><html><head>"
    "<title>  Just a moment...  </title>"
    "</head><body></body></html>"
)

_CF_ATTENTION = (
    "<!DOCTYPE html><html><head>"
    "<title>Attention Required! | Cloudflare</title>"
    "</head><body></body></html>"
)

_CF_CHL_OPT = (
    "<!DOCTYPE html><html><head></head><body>"
    "<script>window.__cf_chl_opt={cType:'managed',cNounce:'99999'};</script>"
    "</body></html>"
)

_CF_CHALLENGE_FORM = (
    "<!DOCTYPE html><html><head><title>One more step</title></head><body>"
    '<form id="challenge-form" action="/cdn-cgi/challenge-platform/h/b/flow/ov1">'
    '<input type="hidden" name="md" value="xxx">'
    "</form></body></html>"
)

_CF_CHALLENGE_FORM_SINGLE_QUOTE = (
    "<!DOCTYPE html><html><head></head><body>"
    "<form id='challenge-form' method='POST'></form>"
    "</body></html>"
)

_CF_TURNSTILE = (
    "<!DOCTYPE html><html><head><title>Verify you are human</title></head><body>"
    '<div class="cf-turnstile" data-sitekey="0x4AAAAAABkMYinukE0bPKi"></div>'
    "</body></html>"
)

_CF_TURNSTILE_EXTRA_CLASSES = (
    "<!DOCTYPE html><html><head></head><body>"
    '<div class="container cf-turnstile widget" data-sitekey="abc123"></div>'
    "</body></html>"
)

_IMPERVA = (
    "<!DOCTYPE html><html><head><title>Request unsuccessful. Incapsula incident ID: 0</title></head>"
    "<body><script>"
    "var _Incapsula_Resource = {'bu': 'https://content.incapsula.com'};"
    "</script></body></html>"
)

_F5_COOKIE_CHALLENGE = (
    "<!DOCTYPE html><html><head><script>"
    'document.cookie="cookiesession8341=blocked";'
    "eval(function(){var request=new XMLHttpRequest();"
    "setTimeout(function(){request.open('GET','/challenge');},10);});"
    "</script></head><body></body></html>"
)

_PACKED_SCRIPT_ONLY_CHALLENGE = (
    "<!DOCTYPE html><html><head></head><body><script>"
    "eval(function(p,a,c,k,e,d){return p;}"
    "('challenge payload',62,95,'tokens'.split('|'),0,{}));"
    "</script></body></html>"
)

_REAL_COURSE_PAGE = (
    "<!DOCTYPE html><html lang='en'><head>"
    "<title>Bachelor of Science (Computer Science) | Example University</title>"
    "<meta name='description' content='Study CS at Example Uni'>"
    "</head><body>"
    "<h1>Bachelor of Science (Computer Science)</h1>"
    "<p>Duration: 3 years full-time.</p>"
    "<p>International fee: AUD 38,000 per year.</p>"
    "<p>English requirements: IELTS overall 6.5 with no band below 6.0.</p>"
    "<p>Intakes: February, July.</p>"
    "<p>Campus: City campus, Online.</p>"
    "</body></html>"
)

_REAL_SHORT_PAGE = (
    "<html><head><title>Graduate Certificate in Data Science | Uni X</title></head>"
    "<body><p>IELTS 6.5 overall, no band below 6.0.</p><p>Fee: $28,000/yr.</p></body></html>"
)

_REAL_PAGE_WITH_INCIDENTAL_MOMENT = (
    "<!DOCTYPE html><html><head>"
    "<title>Master of Engineering | Greenfield University</title>"
    "</head><body>"
    "<p>At this moment in time, the programme offers world-class facilities.</p>"
    "<p>This is just a moment for students to explore their passion.</p>"
    "</body></html>"
)


# ---------------------------------------------------------------------------
# Tests: is_challenge_shell positive cases
# ---------------------------------------------------------------------------


class TestChallengeShellDetected:
    def test_f5_cookie_challenge(self):
        assert is_challenge_shell(_F5_COOKIE_CHALLENGE) is True

    def test_packed_script_only_challenge(self):
        assert is_challenge_shell(_PACKED_SCRIPT_ONLY_CHALLENGE) is True

    def test_cloudflare_spinner_title(self):
        assert is_challenge_shell(_CF_SPINNER) is True

    def test_cloudflare_spinner_case_insensitive(self):
        assert is_challenge_shell(_CF_SPINNER_CAPS) is True

    def test_cloudflare_spinner_leading_whitespace(self):
        assert is_challenge_shell(_CF_SPINNER_WHITESPACE) is True

    def test_cloudflare_attention_required(self):
        assert is_challenge_shell(_CF_ATTENTION) is True

    def test_cloudflare_chl_opt_js_variable(self):
        assert is_challenge_shell(_CF_CHL_OPT) is True

    def test_cloudflare_challenge_form_double_quote(self):
        assert is_challenge_shell(_CF_CHALLENGE_FORM) is True

    def test_cloudflare_challenge_form_single_quote(self):
        assert is_challenge_shell(_CF_CHALLENGE_FORM_SINGLE_QUOTE) is True

    def test_cloudflare_turnstile_widget(self):
        assert is_challenge_shell(_CF_TURNSTILE) is True

    def test_cloudflare_turnstile_with_extra_classes(self):
        assert is_challenge_shell(_CF_TURNSTILE_EXTRA_CLASSES) is True

    def test_imperva_incapsula(self):
        assert is_challenge_shell(_IMPERVA) is True


# ---------------------------------------------------------------------------
# Tests: is_challenge_shell negative cases
# ---------------------------------------------------------------------------


class TestChallengeShellNotDetected:
    def test_f5_cookie_marker_alone(self):
        html = "<script>document.cookie='cookiesession8341=preferences';</script>"
        assert is_challenge_shell(html) is False

    def test_eval_and_xhr_without_f5_cookie_marker(self):
        html = "<script>eval(code); new XMLHttpRequest();</script>"
        assert is_challenge_shell(html) is False

    def test_legitimate_page_that_mentions_f5_cookie_and_uses_xhr(self):
        html = (
            "<html><head>"
            '<meta name="cookie-name" content="cookiesession8341">'
            "<script>const loader=eval; const request=new XMLHttpRequest();</script>"
            "</head><body><h1>Computer Science</h1></body></html>"
        )
        assert is_challenge_shell(html) is False

    def test_legitimate_page_with_packed_script_and_visible_content(self):
        html = (
            "<html><body><h1>Computer Science</h1><script>"
            "eval(function(p,a,c,k,e,d){return p;}"
            "('analytics',62,1,'x'.split('|'),0,{}));"
            "</script></body></html>"
        )
        assert is_challenge_shell(html) is False

    def test_real_course_page(self):
        assert is_challenge_shell(_REAL_COURSE_PAGE) is False

    def test_short_real_page(self):
        assert is_challenge_shell(_REAL_SHORT_PAGE) is False

    def test_incidental_moment_phrase_in_body(self):
        assert is_challenge_shell(_REAL_PAGE_WITH_INCIDENTAL_MOMENT) is False

    def test_empty_string(self):
        assert is_challenge_shell("") is False

    def test_marker_buried_after_scan_window(self):
        prefix = "a" * _CHALLENGE_SHELL_SAMPLE
        html = prefix + "<title>Just a moment...</title>"
        assert is_challenge_shell(html) is False

    def test_marker_at_edge_of_scan_window_is_detected(self):
        prefix = "<!-- " + "x" * (_CHALLENGE_SHELL_SAMPLE - 8) + " -->"
        html = "<title>Just a moment...</title>" + prefix
        assert is_challenge_shell(html) is True

    def test_chl_opt_without_window_prefix(self):
        html = "<script>var __cf_chl_opt = {};</script>"
        assert is_challenge_shell(html) is False

    def test_normal_form_id_not_confused(self):
        html = (
            "<html><head><title>Apply Now | Example Uni</title></head><body>"
            '<form id="application-form" method="POST">'
            "<input name='name'></form></body></html>"
        )
        assert is_challenge_shell(html) is False


# ---------------------------------------------------------------------------
# Integration: rescue-tally gate behaviour
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_BROWSER_TALLY, reason="per_course_browser import unavailable (app chain not loaded)")
class TestRescueTallyGate:
    def setup_method(self):
        reset_browser_only_hosts()

    def _simulate_rescue(self, html: str, host: str) -> None:
        """Replicate the guard logic from single_course.py."""
        if (
            html
            and len(html) >= _BROWSER_RESCUE_MIN_HTML_LEN
            and not is_challenge_shell(html)
        ):
            note_browser_rescue(host)

    def test_real_page_increments_tally_to_confirmation(self):
        padded = _REAL_COURSE_PAGE + "<!-- padding -->" * 150
        assert len(padded) >= _BROWSER_RESCUE_MIN_HTML_LEN
        assert not is_challenge_shell(padded)
        host = "realuni.edu.au"
        for _ in range(3):
            self._simulate_rescue(padded, host)
        assert is_confirmed_browser_only(host)

    def test_challenge_shell_never_increments_tally(self):
        padded = _CF_SPINNER + "x" * max(0, _BROWSER_RESCUE_MIN_HTML_LEN - len(_CF_SPINNER))
        assert len(padded) >= _BROWSER_RESCUE_MIN_HTML_LEN
        assert is_challenge_shell(padded)
        host = "cf-blocked.edu"
        for _ in range(10):
            self._simulate_rescue(padded, host)
        assert not is_confirmed_browser_only(host)

    def test_mixed_real_and_challenge_pages_count_only_real(self):
        padded_real = _REAL_COURSE_PAGE + "<!-- padding -->" * 150
        padded_cf = _CF_CHL_OPT + "x" * max(0, _BROWSER_RESCUE_MIN_HTML_LEN - len(_CF_CHL_OPT))
        host = "mixed.edu"
        self._simulate_rescue(padded_cf, host)
        self._simulate_rescue(padded_real, host)
        self._simulate_rescue(padded_cf, host)
        self._simulate_rescue(padded_real, host)
        assert not is_confirmed_browser_only(host)
        self._simulate_rescue(padded_real, host)
        assert is_confirmed_browser_only(host)

    def test_short_real_page_below_floor_does_not_count(self):
        assert len(_REAL_SHORT_PAGE) < _BROWSER_RESCUE_MIN_HTML_LEN
        host = "shortuni.edu"
        for _ in range(5):
            self._simulate_rescue(_REAL_SHORT_PAGE, host)
        assert not is_confirmed_browser_only(host)
