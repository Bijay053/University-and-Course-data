"""Bounded sitemap candidates, never proof of course identity or coverage."""
from xml.etree import ElementTree
from urllib.parse import urlsplit


def _secure_sitemap_location(value: str, source: str) -> str:
    """Use HTTPS for same-host HTTP locations published by an HTTPS sitemap."""
    try:
        target, origin = urlsplit(value), urlsplit(source)
        if (origin.scheme == "https" and target.scheme == "http"
                and target.hostname and origin.hostname
                and target.hostname.lower().removeprefix("www.") ==
                    origin.hostname.lower().removeprefix("www.")
                and not target.username and not target.password
                and target.port is None):
            return target._replace(scheme="https").geturl()
    except ValueError:
        pass
    return value


def inspect_sitemap(url: str, xml: str, *, byte_limit: int, link_limit: int = 5000) -> dict:
    result = {"url": url, "classification": "unconfirmed", "links": [],
              "coverageComplete": False, "candidateOnly": True}
    encoded = xml.encode("utf-8")
    truncated = len(encoded) >= byte_limit - 4
    text = encoded[:byte_limit].decode("utf-8", errors="ignore")
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        return {**result, "reason": "Sitemap entities and document types are prohibited"}
    parser = ElementTree.XMLPullParser(events=("start", "end"))
    stack, links, seen = [], [], set()
    root_kind, capped = None, False
    try:
        # Incremental reads retain only completed, structurally owned records.
        # An incomplete final <loc> is never a candidate.
        for offset in range(0, len(text), 4096):
            parser.feed(text[offset:offset + 4096])
            for event, node in parser.read_events():
                tag = node.tag.rsplit("}", 1)[-1]
                if event == "start":
                    stack.append(tag)
                    if root_kind is None:
                        root_kind = tag
                        if root_kind not in {"urlset", "sitemapindex"}:
                            raise ValueError("Unsupported sitemap root")
                    if len(stack) > 16:
                        raise ValueError("Sitemap nesting limit exceeded")
                else:
                    owner = "url" if root_kind == "urlset" else "sitemap"
                    if tag == "loc" and stack == [root_kind, owner, "loc"] and node.text:
                        published = node.text.strip()
                        value = _secure_sitemap_location(published, url)
                        if value and value not in seen:
                            link = {"url": value}
                            if value != published:
                                link["publishedUrl"] = published
                            links.append(link)
                            seen.add(value)
                    stack.pop()
                    node.clear()
                    if len(links) >= link_limit:
                        capped = True
                        break
            if capped:
                break
        if not truncated and not capped:
            parser.close()  # Reject malformed small documents, not just bad prefixes.
    except (ElementTree.ParseError, ValueError):
        return {**result, "reason": "Sitemap could not be validated"}
    if not links:
        return {**result, "reason": "No complete sitemap locations within the evidence budget"}
    complete = not truncated and not capped
    return {**result, "classification": "sitemap" if root_kind == "urlset" else "sitemap_index",
            "links": links, "coverageComplete": complete,
            "reason": ("Complete bounded sitemap; candidate URLs still require official course checks"
                       if complete else
                       "Bounded sitemap sample; catalogue coverage is incomplete and course pages remain unverified")}
