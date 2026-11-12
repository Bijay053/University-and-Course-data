"""Bounded catalogue-source recognition and evidence-derived allow proposals."""
import re
from collections import defaultdict
from urllib.parse import urlsplit


def is_catalogue_source(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return bool(
            parsed.scheme == "https" and not parsed.query
            and not parsed.username and not parsed.password
            and re.search(
                r"/(?:courses?-atoz|(?:a-z|az)-of-courses(?:\.aspx)?|"
                r"course-finder|find-a-course|courses?|programmes?|programs?|degrees?|"
                r"courses/(?:undergraduate|postgraduate)-degrees)"
                r"(?:/(?:ug|pg|undergraduate|postgraduate))?/?$",
                parsed.path, re.I,
            )
        )
    except ValueError:
        return False


def catalogue_allow_proposal(urls: list[str], discovery: dict) -> dict:
    """Propose narrow missing URL families, never waive other URL gates.

    These are candidates only: independent live course checks and fresh replay
    still have to accept the entire proposed configuration before persistence.
    """
    from app.services.scraper.ai_repair_live import passes

    allows = discovery.get("allow_url_patterns") or []
    if not allows or sum(passes(url, discovery) for url in urls) >= 2:
        return {}
    groups = defaultdict(set)
    for url in urls:
        if not passes(url, {**discovery, "allow_url_patterns": []}):
            continue
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.query:
            continue
        path = parsed.path.rstrip("/")
        if not re.search(r"/(?:courses?|programmes?|programs?)/.+", path, re.I):
            continue
        parent = path.rsplit("/", 1)[0] + "/"
        groups[(parsed.netloc, parent)].add(path)
    patterns = []
    for (host, parent), paths in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0])):
        if len(paths) >= 2:
            patterns.append(r"^https://" + re.escape(host + parent) + r"[^/?#]+/?$")
        if len(patterns) == 3:
            break
    proposed = list(dict.fromkeys([*allows, *patterns]))
    patch = {"allow_url_patterns": proposed}
    return patch if patterns and sum(passes(url, {**discovery, **patch}) for url in urls) >= 2 else {}
