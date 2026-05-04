"""Shared text-extraction helpers for the scraper extractors.

Centralised so each extractor can stay short and focused.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser


_WS = re.compile(r"\s+")


class _Stripper(HTMLParser):
    """Minimal HTML→text. Skips <script>, <style>, <noscript>."""

    SKIP_TAGS = {"script", "style", "noscript", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._buf: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        elif tag in {
            "br", "p", "li", "tr", "div",
            "h1", "h2", "h3", "h4", "h5", "h6",
            # Definition-list terms and values: ensure "Duration" and the
            # following value cell ("Minimum 1 Semester") are separated by a
            # newline so the sentence splitter treats them as distinct units.
            "dt", "dd",
            # Table header / data cells (th/td already split via tr, but an
            # explicit newline at the cell level is safer for nested tables).
            "th", "td",
        }:
            self._buf.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in {"p", "li", "tr", "div", "h1", "h2", "h3", "h4", "h5", "h6",
                     "dt", "dd", "th", "td"}:
            self._buf.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._buf.append(data)

    def text(self) -> str:
        return "".join(self._buf)


def html_to_text(html: str) -> str:
    """HTML → plain visible text. Robust against malformed markup."""
    if not html:
        return ""
    p = _Stripper()
    try:
        p.feed(html)
    except Exception:
        # HTMLParser can choke on truly broken HTML; fall back to a regex strip.
        return _WS.sub(" ", re.sub(r"<[^>]+>", " ", html)).strip()
    return _WS.sub(" ", p.text()).strip()


def compact(text: str) -> str:
    return _WS.sub(" ", (text or "")).strip()
