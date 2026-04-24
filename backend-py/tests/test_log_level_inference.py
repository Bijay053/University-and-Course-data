"""Bug E: scrape_runtime_logs rows must carry a UI-facing ``level``.

Without this, every log line in the live scrape feed rendered in the
same colour and the operator could not pick out errors / warnings /
fallbacks at a glance. ``infer_log_level`` derives the bucket from the
message prefix ([DISCOVER]/[EXTRACT]/[FALLBACK]/[STAGE]/[SAMPLE✓]/...).
Explicit ``level=`` kwargs always win in the orchestrator wrapper —
this file pins the inference rules themselves.
"""
from __future__ import annotations

from app.services.scraper.orchestrator import infer_log_level


def test_default_is_info():
    assert infer_log_level("Worker claimed queued scrape job") == "info"
    assert infer_log_level("") == "info"


def test_discover_and_classify():
    assert infer_log_level("[DISCOVER] crawled sitemap, 42 candidate links") == "discover"
    assert infer_log_level("[CLASSIFY] page is course detail") == "discover"


def test_extract_and_fallback():
    assert infer_log_level("[EXTRACT] fee.extract → AUD 24,000") == "extract"
    assert infer_log_level("[FALLBACK] PDF vision OCR returned 1.2k chars") == "fallback"


def test_sample_success():
    # The check-mark variant is the success signal Node uses too.
    assert infer_log_level("[SAMPLE\u2713] Bachelor of IT looks complete") == "success"
    # Plain [SAMPLE] without the tick is informational, not a success.
    assert infer_log_level("[SAMPLE] inspecting course page") == "info"


def test_stage_outcomes():
    assert infer_log_level("[STAGE] saved Bachelor of IT (id=421)") == "success"
    assert infer_log_level("[STAGE] staged 3 new courses") == "success"
    assert infer_log_level("[STAGE] skipped — duplicate of existing pending row") == "warn"
    assert infer_log_level("[STAGE] dedup blocked by recent rejection") == "warn"
    assert infer_log_level("[STAGE] error: completeness < 0.4") == "error"
    assert infer_log_level("[STAGE] exception while writing evidence") == "error"
    assert infer_log_level("[STAGE] failed to commit") == "error"


def test_bare_stage_is_neutral():
    # An unqualified [STAGE] line falls through the error/warn/success
    # gates and lands on the generic 'stage' bucket.
    assert infer_log_level("[STAGE] beginning persistence pass") == "stage"


def test_error_prefix_wins():
    assert infer_log_level("[ERROR] discovery returned 0 links") == "error"


def test_case_insensitive():
    # Real emit messages tend to keep tags upper-case but be defensive.
    assert infer_log_level("[discover] sitemap parsed") == "discover"
    assert infer_log_level("[Stage] Saved row 99") == "success"
