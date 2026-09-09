"""Parse JSON responses rendered through a browser, without app dependencies."""
from __future__ import annotations

import html
import json
import re
from typing import Any

from app.services.scraper.challenge_shell import is_challenge_shell


_CHROMIUM_JSON_DOCUMENT_RE = re.compile(
    r"""
    \A\s*
    (?:<!doctype\s+html\s*>\s*)?
    <html\b[^>]*>\s*
      (?:
        <head\b[^>]*>\s*
          (?:<meta\b[^>]*>\s*)*
        </head>\s*
      )?
      <body\b[^>]*>\s*
        <pre\b[^>]*>(?P<payload>.*?)</pre>\s*
        (?:
          <div\b
            (?=[^>]*\bclass\s*=\s*["'][^"']*\bjson-formatter-container\b[^"']*["'])
            [^>]*>\s*</div>\s*
        )?
      </body>\s*
    </html>\s*\Z
    """,
    re.IGNORECASE | re.DOTALL | re.VERBOSE,
)


class RenderedJsonError(ValueError):
    """Raised when a rendered response is explicitly not usable JSON."""


def parse_rendered_json(raw: str) -> Any:
    """Parse plain JSON or Chromium's HTML-escaped ``<pre>`` JSON viewer.

    Known anti-bot challenge pages are rejected before wrapper extraction.
    Incomplete wrappers, arbitrary HTML, and non-JSON text are left to
    ``json.loads`` to fail rather than being guessed or repaired.
    """
    if not isinstance(raw, str):
        raise TypeError("JSON response must be text")

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    if is_challenge_shell(raw):
        raise RenderedJsonError(
            "rendered JSON response was an anti-bot challenge page"
        )

    match = _CHROMIUM_JSON_DOCUMENT_RE.fullmatch(raw)
    if not match:
        return json.loads(raw)

    return json.loads(html.unescape(match.group("payload")).strip())