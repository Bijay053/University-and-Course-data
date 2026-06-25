"""Per-run skip-gate counters (Task #235).

Pure stdlib module — no app dependencies — so it can be imported and tested in
isolation under any CPU load (same isolation pattern as challenge_shell.py).

Five latency-gate skip paths are counted per scrape run:

  gemini_timeout       — Gemini SDK call exceeded the per-call timeout; the
                         circuit breaker was notified and future calls may short-
                         circuit.  Counts API-latency events, not budget skips.
  gemini_circuit_open  — Gemini skipped because the circuit breaker is already
                         tripped (too many quota/timeout errors in the window).
                         Counts courses that paid zero Gemini latency this run.
  vision_early_exit    — Vision OCR pass skipped entirely; all five English
                         overall slots (IELTS/PTE/TOEFL/CAE/Duolingo) were
                         already filled AND no tier-0 image was anchored in the
                         English-requirements DOM section.  The OCR pass would
                         have been a guaranteed no-op.
  browser_http_skipped — Plain HTTP fetch skipped for a host confirmed to be
                         browser-only (≥3 genuine browser rescues in this run).
                         The wasted HTTP round-trip is omitted for the rest of
                         the run; each such skip saves ~1–3 s of fetch latency.
  challenge_shell      — The browser returned substantive HTML that the content-
                         based detector (challenge_shell.py) identified as a
                         Cloudflare/Imperva anti-bot interstitial.  The browser
                         rescue tally was NOT incremented (Task #236 gate).

Usage pattern (mirrors per_course_browser browser-only tally and scrape_do counters):
  1. orchestrator.run_scrape()   → reset_skip_counters()   (run start)
  2. gate fire-sites             → note_skip(<key>)         (per skip event)
  3. orchestrator completion     → get_skip_counts()        (read + persist)
"""
from __future__ import annotations

from contextvars import ContextVar

_COUNTER_KEYS: tuple[str, ...] = (
    "gemini_timeout",
    "gemini_circuit_open",
    "vision_early_exit",
    "browser_http_skipped",
    "challenge_shell",
)

_skip_counts: ContextVar[dict[str, int] | None] = ContextVar(
    "gate_skip_counts", default=None
)


def reset_skip_counters() -> None:
    """Initialise a fresh per-run counter dict.  Call once at run start.

    Mirrors ``per_course_browser.reset_browser_only_hosts()`` — both are
    called together at the start of ``orchestrator.run_scrape()`` so a prior
    run on the same Celery worker cannot carry stale counts into the next run.
    """
    _skip_counts.set({k: 0 for k in _COUNTER_KEYS})


def note_skip(key: str) -> None:
    """Increment the counter for *key*.

    Silently no-ops when the counters have not been initialised (e.g. in unit
    tests that call extractors directly without going through run_scrape()).
    Unknown keys are also silently ignored so new gate sites can be added
    without breaking existing runs that haven't called reset_skip_counters()
    with the new key yet.
    """
    d = _skip_counts.get(None)
    if d is None:
        return
    if key in d:
        d[key] += 1


def get_skip_counts() -> dict[str, int]:
    """Return a snapshot copy of all counters.

    Returns an empty dict when the counters have not been initialised.
    Only non-zero counters are typically of interest; callers may filter with
    ``{k: v for k, v in get_skip_counts().items() if v}``.
    """
    d = _skip_counts.get(None)
    if d is None:
        return {}
    return dict(d)
