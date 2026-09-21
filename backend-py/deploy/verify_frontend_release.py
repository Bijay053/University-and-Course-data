#!/usr/bin/env python3
"""Verify that a built or publicly served frontend is the target release."""

from __future__ import annotations

import argparse
from html.parser import HTMLParser
from pathlib import Path
import re
import urllib.parse
import urllib.request


HASHED_JAVASCRIPT = re.compile(r"(?:^|/)assets/[^/?#]+-[A-Za-z0-9_-]{8,}\.js$")


class _ScriptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sources: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.lower() != "script":
            return
        source = dict(attrs).get("src")
        if source:
            self.sources.append(source)


def script_sources(html: str) -> list[str]:
    parser = _ScriptParser()
    parser.feed(html)
    return parser.sources


def hashed_script_sources(html: str) -> list[str]:
    return [
        source
        for source in script_sources(html)
        if HASHED_JAVASCRIPT.search(urllib.parse.urlsplit(source).path)
    ]


def verify_local_build(dist: Path, marker: bytes) -> list[str]:
    index = dist / "index.html"
    assert index.is_file(), f"Frontend build did not create {index}"
    sources = hashed_script_sources(index.read_text(encoding="utf-8"))
    assert sources, "Built frontend HTML references no hashed JavaScript asset"
    marker_found = False
    for source in sources:
        relative = urllib.parse.urlsplit(source).path.lstrip("/")
        asset = dist / relative
        assert asset.is_file(), f"Built frontend asset is missing: {relative}"
        marker_found = marker_found or marker in asset.read_bytes()
    assert marker_found, "Target release marker is absent from built frontend assets"
    return sources


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        assert response.status == 200, f"Unexpected HTTP {response.status} for {url}"
        return response.read()


def verify_public_build(
    dist: Path, public_url: str, marker: bytes
) -> list[str]:
    expected = verify_local_build(dist, marker)
    separator = "&" if "?" in public_url else "?"
    html = _fetch(f"{public_url}{separator}release={marker.decode().rsplit(':', 1)[-1]}")
    public_sources = hashed_script_sources(html.decode("utf-8"))
    assert public_sources == expected, (
        "Public HTML does not reference the newly built hashed frontend assets: "
        f"expected {expected!r}, got {public_sources!r}"
    )
    assert any(
        marker in _fetch(urllib.parse.urljoin(public_url, source))
        for source in public_sources
    ), "Target release marker is absent from publicly served frontend assets"
    return public_sources


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--public-url")
    args = parser.parse_args()
    marker = f"UNIVERSITY_PORTAL_RELEASE:{args.target}".encode()
    sources = (
        verify_public_build(args.dist, args.public_url, marker)
        if args.public_url
        else verify_local_build(args.dist, marker)
    )
    print("FRONTEND_RELEASE_OK", *sources)


if __name__ == "__main__":
    main()