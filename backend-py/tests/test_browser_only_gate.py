"""Task #233 — confirmed-browser-only host gate.

On large scrapes of fully Cloudflare/JS-gated universities, every course pays
a guaranteed-to-fail plain-HTTP fetch before the browser fallback rescues it.
``per_course_browser`` keeps a per-run tally of how many times each host was
*rescued* by the browser; once a host crosses the threshold,
``is_confirmed_browser_only`` returns True and ``single_course`` skips the
wasted HTTP attempt for the rest of that run.

The tally lives in a ``ContextVar`` so it is scoped to a single scrape job and
never leaks across runs.  ``reset_browser_only_hosts`` is called at the top of
both ``orchestrator.run_scrape`` and ``repair.run_repair``.
"""
from __future__ import annotations

import contextvars

from app.services.scraper import per_course_browser as pcb


def test_threshold_requires_three_rescues() -> None:
    pcb.reset_browser_only_hosts()
    host = "www.utas.edu.au"
    assert not pcb.is_confirmed_browser_only(host)
    pcb.note_browser_rescue(host)
    pcb.note_browser_rescue(host)
    assert not pcb.is_confirmed_browser_only(host), "2 rescues < threshold 3"
    pcb.note_browser_rescue(host)
    assert pcb.is_confirmed_browser_only(host), "3rd rescue must confirm host"


def test_reset_clears_tally() -> None:
    pcb.reset_browser_only_hosts()
    host = "courses.example.edu"
    for _ in range(3):
        pcb.note_browser_rescue(host)
    assert pcb.is_confirmed_browser_only(host)
    pcb.reset_browser_only_hosts()
    assert not pcb.is_confirmed_browser_only(host), "reset must zero the tally"


def test_hosts_are_isolated() -> None:
    pcb.reset_browser_only_hosts()
    for _ in range(3):
        pcb.note_browser_rescue("a.edu")
    assert pcb.is_confirmed_browser_only("a.edu")
    assert not pcb.is_confirmed_browser_only("b.edu"), "tally must be per-host"


def test_empty_host_never_confirmed() -> None:
    pcb.reset_browser_only_hosts()
    pcb.note_browser_rescue("")
    pcb.note_browser_rescue("")
    pcb.note_browser_rescue("")
    assert not pcb.is_confirmed_browser_only("")


def test_no_run_context_is_safe_noop() -> None:
    """Outside any scrape job (contextvar at its default), the helpers must be
    safe no-ops — never raise, never confirm.  Verified inside a fresh
    ``contextvars.Context`` so a prior test's ``reset`` can't pre-seed it."""
    ctx = contextvars.Context()

    def _inside() -> bool:
        pcb.note_browser_rescue("x.edu")  # must not raise without reset
        return pcb.is_confirmed_browser_only("x.edu")

    assert ctx.run(_inside) is False
