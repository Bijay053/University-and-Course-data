"""Complete, bounded traversal of explicitly selected official subject directories.

Completeness here means the configured directory tree, never an assertion that
the university has no other courses. No source-card facts become course fields.
"""
import asyncio
import re
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

AGENT = "StudyInfoCentre-SourceVerifier/1.0"


def robots_policy(text: str, agent: str = AGENT):
    """Merge repeated applicable groups; use longest REP match, Allow on ties."""
    groups = []
    agents, rules, delay, directives = [], [], 0.0, False
    def flush():
        if agents:
            groups.append((list(agents), list(rules), delay))
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if directives:
                flush()
                agents, rules, delay, directives = [], [], 0.0, False
            agents.append(value.lower())
        elif key in {"allow", "disallow"} and agents:
            directives = True
            if value:
                rules.append((key == "allow", value))
        elif key == "crawl-delay" and agents:
            directives = True
            try:
                delay = max(delay, float(value))
            except ValueError:
                pass
    flush()
    matches = [(max((len(a) for a in names if a != "*" and a in agent.lower()), default=0),
                names, rules, delay) for names, rules, delay in groups]
    specificity = max((item[0] for item in matches), default=0)
    chosen = [item for item in matches if item[0] == specificity
              and (specificity or "*" in item[1])]
    def normalise(value):
        value = quote(value, safe="/%:@!$&'()*+,;=-._~?")
        def decode(match):
            char = chr(int(match[1], 16))
            return char if char.isascii() and (char.isalnum() or char in "-._~") else match[0].upper()
        return re.sub(r"%([0-9a-fA-F]{2})", decode, value)
    compiled = []
    for _, _, rules, _ in chosen:
        for allow, pattern in rules:
            ending = pattern.endswith("$")
            expression = re.escape(normalise(pattern[:-1] if ending else pattern)).replace(r"\*", ".*")
            compiled.append((len(normalise(pattern).replace("*", "").rstrip("$").encode("utf-8")), allow,
                             re.compile("^" + expression + ("$" if ending else ""))))
    def permits(url):
        parsed = urlsplit(url)
        path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
        if parsed.query:
            path += "?" + parsed.query
        matched = [(weight, allow) for weight, allow, rule in compiled if rule.search(normalise(path))]
        return max(matched, default=(0, True))[1]
    return permits, max((item[3] for item in chosen), default=0.0)


def directory_roots(seeds):
    """This opt-in is limited to unqueried subject-directory entry points."""
    if not seeds or len(seeds) > 8:
        return []
    if not all(isinstance(url, str) and urlsplit(url).scheme == "https"
               and not urlsplit(url).query and
               re.search(r"/(?:subjects?|subject-areas|study-areas)/?$",
                         urlsplit(url).path, re.I) for url in seeds):
        return []
    return list(dict.fromkeys(url.split("#", 1)[0] for url in seeds))


@dataclass
class DirectoryCatalogue:
    roots: list[str]
    candidates: list[dict] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)
    remaining: list[str] = field(default_factory=list)
    complete: bool = False
    scope: str = "configured_subject_directories"
    international_sample_hints: dict[str, bool] = field(default_factory=dict)


class DirectoryCatalogueIncomplete(RuntimeError):
    def __init__(self, report):
        self.report = report
        super().__init__(
            f"Official directory scan incomplete: {len(report.pages)} pages read, "
            f"{len(report.remaining)} not checked, {len(report.failures)} failures. "
            "Partial candidates were not accepted as a complete discovery."
        )


async def collect_directory_catalogue(
    roots, seed, config, *, max_pages=80, max_candidates=200,
    max_seconds=180, fetch=None, emit=None,
):
    """Only walk published descendants; no query URLs, tracking requests or guesses."""
    from app.services.scraper.ai_repair_live import (
        _fetch_official, _course_links_in_region, official_url,
    )
    fetch = fetch or _fetch_official
    roots = directory_roots(roots)
    extra_hosts = getattr(config.discovery, "allowed_extra_hostnames", ())
    if not roots or not all(official_url(url, seed, extra_hosts) for url in roots):
        raise ValueError("Official unqueried subject-directory roots are required.")
    max_pages = max(0, min(int(max_pages), 300))
    max_candidates = max(0, min(int(max_candidates), 5000))
    deadline = time.monotonic() + max(0, min(float(max_seconds), 600))
    report = DirectoryCatalogue(roots=roots)
    queue, seen, courses, policies = list(roots), set(), {}, {}
    requests = 0
    async def read(url):
        nonlocal requests
        remaining = deadline - time.monotonic()
        if remaining <= 0 or requests >= max_pages:
            raise TimeoutError("Directory page/time budget exhausted")
        requests += 1
        return await asyncio.wait_for(fetch(url, config, min(20, remaining)),
                                      timeout=min(20, remaining))
    def in_scope(url):
        p = urlsplit(url)
        ambiguous_path = any(
            unquote(segment) in {".", ".."}
            or any(char in unquote(segment) for char in "/\\?#")
            for segment in p.path.split("/")
        )
        return (not p.query and not ambiguous_path and official_url(url, seed, extra_hosts) and any(
            p.path.rstrip("/") == urlsplit(root).path.rstrip("/")
            or p.path.startswith(urlsplit(root).path.rstrip("/") + "/")
            for root in roots))
    while queue:
        url = queue.pop(0)
        if url in seen:
            continue
        if not in_scope(url):
            report.failures.append({"url": url, "reason": "Outside declared directory scope"})
            continue
        if requests >= max_pages or time.monotonic() >= deadline:
            report.remaining = [url, *queue]
            break
        seen.add(url)
        p = urlsplit(url)
        origin = urlunsplit((p.scheme, p.netloc, "", "", ""))
        kind = ""
        try:
            if origin not in policies:
                body, kind, reason = await read(origin + "/robots.txt")
                if kind == "not_published":
                    policies[origin] = (lambda _: True, 0.0)
                elif kind or not body or "<html" in body.lower():
                    raise ValueError("robots.txt could not be verified: " + (reason or kind))
                else:
                    policies[origin] = robots_policy(body)
            permits, delay = policies[origin]
            if not permits(url):
                raise ValueError("robots.txt disallows this directory; no bypass attempted")
            if delay:
                if delay > deadline - time.monotonic():
                    raise TimeoutError("Crawl delay exceeds remaining directory budget")
                await asyncio.sleep(delay)
            html, kind, reason = await read(url)
            if kind or not html:
                raise ValueError(reason or kind or "Empty official directory")
            soup = BeautifulSoup(html, "html.parser")
            main = soup.select_one("main, [role=main]")
            # Some official subject templates start at H2 rather than H1.
            # This identifies a listing only; its cards never own course facts.
            heading = soup.find(["h1", "h2"])
            if main is None or heading is None or heading.find_parent(["nav", "footer"]):
                raise ValueError("Official directory has no identifiable main heading")
            region = main.select_one("#fb-search-results") or main
            if main.select_one(".pagination a[href], a[rel=next], button.load-more"):
                raise ValueError("Directory is paginated; unverified pages were not skipped")
            for anchor in main.select("a[href]"):
                target = urljoin(url, anchor["href"])
                path_only = target.split("?", 1)[0].split("#", 1)[0]
                if in_scope(path_only) and any(
                    key.lower() in {"page", "pagenum", "page_number", "start_rank",
                                    "offset", "cursor", "start"}
                    for key in parse_qs(urlsplit(target).query)
                ):
                    raise ValueError("Directory is paginated; unverified pages were not skipped")
            page_courses = _course_links_in_region(region, url, extra_hosts)
            for course in page_courses:
                courses.setdefault(course["url"].rstrip("/"), course)
            # Card audience labels only prioritise which detail pages to
            # verify. They do not establish eligibility or populate any field.
            for card in region.select(".search-result, .course-card, [data-course-card]"):
                card_heading = card.select_one("h2, h3, h4")
                if card_heading is None:
                    continue
                international = bool(re.search(
                    r"\bsuitable\s+for\s*:?\s*(?:UK\s*(?:and|,|&)\s*)?international\b",
                    card.get_text(" ", strip=True), re.I,
                ))
                for item in _course_links_in_region(card_heading, url, extra_hosts):
                    key = item["url"].rstrip("/")
                    report.international_sample_hints[key] = (
                        report.international_sample_hints.get(key, False) or international
                    )
            children = []
            for anchor in main.select("a[href]"):
                target = urljoin(url, anchor["href"]).split("#", 1)[0]
                if in_scope(target) and target.rstrip("/") != url.rstrip("/"):
                    children.append(target)
                    if target not in seen and target not in queue:
                        queue.append(target)
            if not page_courses and not children:
                raise ValueError("No course or child-directory links established on this page")
            report.pages.append(url)
            if emit:
                await emit("status", f"[DIRECTORY] {len(report.pages)} pages read; "
                           f"{len(queue)} queued; {len(courses)} course candidates",
                           phase="discover", kind="directory_progress")
        except Exception as exc:
            report.failures.append({"url": url, "reason": str(exc)})
            # No repeated requests against a failed policy endpoint or a host
            # which has just challenged access. A new attempt needs changed
            # access conditions, not another transport or user agent.
            if origin not in policies or kind == "challenge":
                report.remaining = list(queue)
                break
    report.candidates = list(courses.values())[:max_candidates]
    if len(courses) > max_candidates:
        report.failures.append({"url": seed, "reason": "Candidate cap reached; discovery is incomplete"})
    if not courses and not report.failures and not report.remaining:
        report.failures.append({"url": seed, "reason": "No course candidates established"})
    report.complete = not report.failures and not report.remaining and bool(report.candidates)
    return report
