"""Complete a course title only from its own bounded hero's explicit award."""
import re
from urllib.parse import urlsplit
from bs4 import BeautifulSoup

_ATOM = re.compile(
    r"(?:BSc|BEng|BAcc|BA|BBA|BEd|BCom|BTech|MSc|MEng|MA|MBA|MRes|MEd|MPhil|"
    r"MCom|LLB|LLM|PhD|DPhil|PGCert|PGDip|Professional\s+Doctorate)(?:\s*\(Hons\))?", re.I,
)


def complete_owned_course_title(html: str, url: str, name: str | None) -> str | None:
    from app.services.scraper.guards import is_blocked_page, is_generic_course_category_name, _name_has_degree_qualifier
    if not name or _name_has_degree_qualifier(name) or is_generic_course_category_name(name):
        return None
    if is_blocked_page(url, name)[0] or not re.search(
        r"/(?:courses?|programmes?|programs?)/[^/]+", urlsplit(url).path, re.I
    ):
        return None
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    normal = lambda text: re.sub(r"\s+", " ", text).strip().casefold()
    if not h1 or normal(h1.get_text(" ", strip=True)) != normal(name):
        return None
    owner = h1.parent
    for _ in range(4):
        if (owner is None or owner.name in {"html", "body", "nav", "aside", "footer"}
                or re.search(r"related|recommend|course.card", " ".join(owner.get("class", [])), re.I)):
            break
        if len(owner.get_text(" ", strip=True)) > 900 or len(owner.select("h1")) != 1:
            break
        awards = set()
        for node in owner.find_all(["p", "span", "div", "strong", "small", "h2", "h3"]):
            text = node.get_text(" ", strip=True)
            if not _ATOM.fullmatch(text):
                continue
            if any(parent.name in {"a", "nav", "aside", "footer"}
                   or re.search(r"related|recommend|course.card", " ".join(parent.get("class", [])), re.I)
                   for parent in (node, *node.parents) if parent is not owner):
                continue
            awards.add(text)
        if len(awards) == 1:
            return f"{name.strip()} {awards.pop()}"
        if len(awards) > 1:
            return None
        owner = owner.parent
    return None
