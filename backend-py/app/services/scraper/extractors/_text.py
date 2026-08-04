"""Shared text-extraction helpers for the scraper extractors.

Centralised so each extractor can stay short and focused.
"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser


_WS = re.compile(r"\s+")


# Some sites (notably Federation University's Vue-based course pages) ship
# inactive tab / accordion content as JSON-escaped HTML strings inside a
# <script> blob:
#     {"name":"WysiwygBlock","props":{"content":"<h2>...$37,800...</h2>..."}}
# The visible DOM only contains the active tab — clicking "International"
# lazy-injects the other block. Without this preprocessor html_to_text strips
# the <script> entirely and the international tuition / requirements are
# never seen by the regex extractors.
#
# The pattern requires the literal block-component JSON shape so it is a
# no-op on every other university site.  Recognised block names are limited
# to Federation's tab / wysiwyg / accordion components — extend the
# alternation if a new block type appears.
_COMPONENT_CONTENT_RE = re.compile(
    r'"name"\s*:\s*"(?:WysiwygBlock|AccordionBlock|TabBlock|'
    r'FeesAndScholarshipsBlock|EntryRequirementsBlock|CourseDetailsBlock)"'
    r'[^{}]*?"props"\s*:\s*\{[^{}]*?"content"\s*:\s*"((?:[^"\\]|\\.)*)"',
    re.DOTALL,
)


def _extract_hidden_component_html(html: str) -> str:
    """Pull JSON-escaped HTML payloads out of Vue-style component-tree
    <script> blobs and return them as a plain HTML fragment ready to feed
    back through ``_Stripper``.  Returns an empty string when the page has
    no recognised component blocks.
    """
    if not html or '"name"' not in html:
        return ""
    pieces: list[str] = []
    for m in _COMPONENT_CONTENT_RE.finditer(html):
        raw = m.group(1)
        # JSON-decode the escaped string (handles \", \\, \n, \/, \uXXXX).
        try:
            pieces.append(json.loads(f'"{raw}"'))
        except (ValueError, json.JSONDecodeError):
            continue
    return "\n".join(pieces)


class _Stripper(HTMLParser):
    """Minimal HTML→text. Skips <script>, <style>, <noscript>.

    NOTE: <template> is intentionally NOT in SKIP_TAGS.  Alpine.js uses
    ``<template x-if="...">`` to conditionally render real content (IELTS
    requirements, fees, duration) that is structurally identical to the live
    DOM once Alpine evaluates.  Skipping template elements silently drops
    that data.  Plain-text noise from unrendered ``{{ var }}`` expressions
    in client-only Vue/Handlebars templates is minor and does not cause
    false positives in downstream regex extractors.
    """

    SKIP_TAGS = {"script", "style", "noscript"}

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
    """HTML → plain visible text. Robust against malformed markup.

    Also recovers JSON-escaped HTML hidden in component-tree <script> blobs
    (Federation tabs / accordions). Without that recovery the inactive tab
    payload — which on Federation pages is where the international tuition
    fee and several other detail blocks live — would be invisible to all
    downstream extractors because <script> is in ``_Stripper.SKIP_TAGS``.
    """
    if not html:
        return ""
    p = _Stripper()
    try:
        p.feed(html)
    except Exception:
        # HTMLParser can choke on truly broken HTML; fall back to a regex strip.
        visible = re.sub(r"<[^>]+>", " ", html)
    else:
        visible = p.text()

    hidden = _extract_hidden_component_html(html)
    if hidden:
        # Strip the recovered fragment with a fresh parser instance so the
        # original parser's skip-depth state cannot bleed into it.
        p2 = _Stripper()
        try:
            p2.feed(hidden)
            visible = visible + "\n" + p2.text()
        except Exception:
            visible = visible + "\n" + re.sub(r"<[^>]+>", " ", hidden)

    return _WS.sub(" ", visible).strip()


def compact(text: str) -> str:
    return _WS.sub(" ", (text or "")).strip()
