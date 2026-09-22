#!/usr/bin/env python3
"""Verify that a built or publicly served frontend is the target release."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.parse


HASHED_JAVASCRIPT = re.compile(r"(?:^|/)assets/[^/?#]+-[A-Za-z0-9_-]{8,}\.js$")
HASHED_STYLESHEET = re.compile(r"(?:^|/)assets/[^/?#]+-[A-Za-z0-9_-]{8,}\.css$")
# Production's public edge can remain unavailable for roughly two minutes
# after a service restart even though local health and revision checks pass.
# Keep the gate bounded while allowing a full four-minute recovery window.
PUBLIC_FETCH_ATTEMPTS = 48
PUBLIC_FETCH_RETRY_SECONDS = 5


@dataclass(frozen=True)
class FetchResponse:
    body: bytes
    headers: dict[str, str]


class _AssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.script_sources: list[str] = []
        self.stylesheet_sources: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        if tag.lower() == "script":
            source = attributes.get("src")
            if source:
                self.script_sources.append(source)
        elif (
            tag.lower() == "link"
            and (attributes.get("rel") or "").lower() == "stylesheet"
        ):
            source = attributes.get("href")
            if source:
                self.stylesheet_sources.append(source)


def script_sources(html: str) -> list[str]:
    parser = _AssetParser()
    parser.feed(html)
    return parser.script_sources


def hashed_script_sources(html: str) -> list[str]:
    return [
        source
        for source in script_sources(html)
        if HASHED_JAVASCRIPT.search(urllib.parse.urlsplit(source).path)
    ]


def hashed_asset_sources(html: str) -> list[str]:
    parser = _AssetParser()
    parser.feed(html)
    return [
        source
        for source in parser.script_sources
        if HASHED_JAVASCRIPT.search(urllib.parse.urlsplit(source).path)
    ] + [
        source
        for source in parser.stylesheet_sources
        if HASHED_STYLESHEET.search(urllib.parse.urlsplit(source).path)
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


def _parse_final_headers(raw: bytes) -> dict[str, str]:
    blocks = re.split(rb"\r?\n\r?\n", raw.strip())
    final = next(
        (block for block in reversed(blocks) if block.startswith(b"HTTP/")),
        None,
    )
    assert final is not None, "Public response contained no HTTP headers"
    headers: dict[str, str] = {}
    for line in final.splitlines()[1:]:
        if b":" not in line:
            continue
        name, value = line.split(b":", 1)
        normalized_name = name.decode("latin-1").strip().lower()
        normalized_value = value.decode("latin-1").strip()
        headers[normalized_name] = ", ".join(
            filter(None, (headers.get(normalized_name), normalized_value))
        )
    return headers


def _fetch_response(url: str) -> FetchResponse:
    # The public edge rejects Python urllib's client fingerprint with HTTP 403
    # even when its headers match a browser. curl reaches the exact same
    # cache-busted URL reliably from the release host.
    with tempfile.NamedTemporaryFile() as header_file:
        command = [
            "curl",
            "--fail",
            "--silent",
            "--show-error",
            "--location",
            "--max-time",
            "30",
            "--noproxy",
            "*",
            "--dump-header",
            header_file.name,
            "--header",
            "Cache-Control: no-cache",
            "--header",
            "Pragma: no-cache",
            "--user-agent",
            "Mozilla/5.0 UniversityPortalReleaseVerifier/1.0",
            url,
        ]
        for attempt in range(PUBLIC_FETCH_ATTEMPTS):
            try:
                result = subprocess.run(
                    command,
                    check=True,
                    capture_output=True,
                )
                header_file.seek(0)
                return FetchResponse(
                    body=result.stdout,
                    headers=_parse_final_headers(header_file.read()),
                )
            except subprocess.CalledProcessError:
                if attempt == PUBLIC_FETCH_ATTEMPTS - 1:
                    raise
                # The public edge can reject requests throughout the service
                # restart window, then serve the exact same URL moments later.
                time.sleep(PUBLIC_FETCH_RETRY_SECONDS)
    raise AssertionError("unreachable")


def _fetch(url: str) -> bytes:
    return _fetch_response(url).body


def _cache_directives(response: FetchResponse) -> set[str]:
    return {
        directive.strip().lower()
        for directive in response.headers.get("cache-control", "").split(",")
        if directive.strip()
    }


def _verify_html_cache_policy(response: FetchResponse, label: str) -> None:
    directives = _cache_directives(response)
    assert {"no-store", "no-cache"} <= directives, (
        f"{label} must return Cache-Control with no-store and no-cache; "
        f"got {response.headers.get('cache-control')!r}"
    )


def _verify_asset_cache_policy(response: FetchResponse, source: str) -> None:
    directives = _cache_directives(response)
    max_ages = [
        int(directive.split("=", 1)[1])
        for directive in directives
        if directive.startswith("max-age=")
        and directive.split("=", 1)[1].isdigit()
    ]
    assert "immutable" in directives and max(max_ages, default=0) >= 31_536_000, (
        f"Hashed asset {source!r} must return long-lived immutable Cache-Control; "
        f"got {response.headers.get('cache-control')!r}"
    )


def verify_public_build(
    dist: Path, public_url: str, marker: bytes
) -> list[str]:
    expected = verify_local_build(dist, marker)
    separator = "&" if "?" in public_url else "?"
    release = marker.decode().rsplit(":", 1)[-1]
    root_response = _fetch_response(f"{public_url}{separator}release={release}")
    _verify_html_cache_policy(root_response, "Public frontend HTML")
    html = root_response.body.decode("utf-8")
    public_sources = hashed_script_sources(html)
    assert public_sources == expected, (
        "Public HTML does not reference the newly built hashed frontend assets: "
        f"expected {expected!r}, got {public_sources!r}"
    )
    fallback_url = urllib.parse.urljoin(
        public_url, f"__frontend_release_verify__/{release}?release={release}"
    )
    fallback_response = _fetch_response(fallback_url)
    _verify_html_cache_policy(fallback_response, "SPA fallback HTML")
    assert hashed_script_sources(fallback_response.body.decode("utf-8")) == expected, (
        "SPA fallback does not serve the newly built frontend HTML"
    )
    marker_found = False
    for source in hashed_asset_sources(html):
        asset_response = _fetch_response(urllib.parse.urljoin(public_url, source))
        _verify_asset_cache_policy(asset_response, source)
        marker_found = marker_found or marker in asset_response.body
    assert marker_found, "Target release marker is absent from publicly served frontend assets"
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