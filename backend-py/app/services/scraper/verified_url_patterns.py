"""Propose narrow URL rules from independently verified course pages.

This is a proposal only: full filter simulation, live validation, ownership
fencing and rollback in the repair agent remain mandatory before persistence.
"""
from collections import defaultdict
import posixpath
import re
from urllib.parse import urlsplit


def proposed_allow_patterns(urls: list[str], existing: list[str]) -> list[str] | None:
    groups = defaultdict(set)
    for url in urls:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            continue
        path = parsed.path.rstrip("/")
        parent, leaf = posixpath.split(path)
        # Never generalise a homepage or a top-level subject directory.
        if len([part for part in parent.split("/") if part]) < 2 or not leaf:
            continue
        extension = posixpath.splitext(leaf)[1]
        if extension and extension not in {".aspx", ".html", ".htm", ".php"}:
            continue
        groups[(parsed.netloc.lower(), parent, extension)].add(path)
    additions = []
    for (host, parent, extension), paths in sorted(groups.items()):
        if len(paths) < 2:
            continue
        pattern = (
            rf"^https?://{re.escape(host)}{re.escape(parent)}/"
            rf"[^/?#]+{re.escape(extension)}/?(?:[?#].*)?$"
        )
        if pattern not in existing:
            additions.append(pattern)
    if not additions or len(additions) > 4:
        return None
    return [*existing, *additions]
