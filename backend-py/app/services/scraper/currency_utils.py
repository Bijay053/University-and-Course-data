"""Shared currency-inference utilities for the scraper pipeline.

Precedence chain (highest → lowest):
  1. Currency explicitly extracted from fee text (£17,500 → GBP, NZ$29,000 → NZD)
  2. Currency returned by API / PDF extractors
  3. Per-university YAML  extraction.fees.default_currency
  4. TLD-based inference via  currency_detection.tld_currency_map  in defaults.yaml
  5. currency_detection.default_currency in defaults.yaml  (ultimate fallback)

This module handles tiers 4–5 only.  Tiers 1–3 are resolved upstream by the
fee extractor and stored in the extraction result dict under ``fee_currency``.

The TLD map is loaded from ``scraper_config/defaults.yaml`` and cached in memory
after first access.  A Celery / FastAPI worker restart is required for changes
to ``defaults.yaml`` to take effect (acceptable — the map rarely changes).
"""
from __future__ import annotations

import functools
import logging
from pathlib import Path
from urllib.parse import urlparse

import yaml

log = logging.getLogger(__name__)

_DEFAULTS_FILE = (
    Path(__file__).parent.parent.parent.parent / "scraper_config" / "defaults.yaml"
)


@functools.lru_cache(maxsize=1)
def _load_currency_detection() -> dict:
    """Load and cache the ``currency_detection`` block from defaults.yaml."""
    try:
        with _DEFAULTS_FILE.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return data.get("currency_detection") or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("currency_utils: could not load defaults.yaml: %s", exc)
        return {}


def _tld_currency_pairs() -> list[tuple[str, str]]:
    """Return (tld, currency) pairs sorted longest-TLD-first for correct matching.

    Example: ``[(".edu.sg", "SGD"), (".ac.uk", "GBP"), ..., (".uk", "GBP")]``
    so that ``.edu.sg`` is checked before ``.sg`` and ``.ac.uk`` before ``.uk``.
    """
    raw: dict[str, str] = _load_currency_detection().get("tld_currency_map") or {}
    return sorted(raw.items(), key=lambda kv: len(kv[0]), reverse=True)


def infer_currency_from_hostname(hostname: str) -> str | None:
    """Return the ISO 4217 currency code inferred from *hostname*'s TLD.

    Returns ``None`` when no TLD entry matches (caller decides fallback).
    Uses the configurable ``currency_detection.tld_currency_map`` from
    ``scraper_config/defaults.yaml`` — no hardcoded TLD list in this function.

    Examples
    --------
    >>> infer_currency_from_hostname("www.bcu.ac.uk")
    'GBP'
    >>> infer_currency_from_hostname("www.aut.ac.nz")
    'NZD'
    >>> infer_currency_from_hostname("www.uq.edu.au")
    'AUD'
    >>> infer_currency_from_hostname("unknown.example.com")  # no TLD match
    None
    """
    h = hostname.lower()
    for tld, currency in _tld_currency_pairs():
        if h.endswith(tld):
            return currency
    return None


def infer_currency_from_url(url: str) -> str | None:
    """Convenience wrapper: extract hostname from *url* then call
    :func:`infer_currency_from_hostname`.  Returns ``None`` on no match.
    """
    host = (urlparse(url).hostname or "").lower()
    return infer_currency_from_hostname(host)


def default_currency() -> str:
    """Return the ultimate fallback currency from defaults.yaml.

    Reads ``currency_detection.default_currency``; falls back to ``"AUD"``
    only if the config key is missing (so the file can't silently break prod).
    """
    return _load_currency_detection().get("default_currency") or "AUD"


def infer_currency(hostname: str) -> str:
    """Return a currency code for *hostname*, guaranteed non-None.

    Tries TLD inference first; falls back to :func:`default_currency`.
    Suitable for stub-YAML generation where a definitive answer is needed.
    """
    return infer_currency_from_hostname(hostname) or default_currency()
