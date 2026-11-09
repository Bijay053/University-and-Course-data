"""Isolate Acadia's programme content from its shared all-programme widgets."""
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup


def is_programme_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.hostname in {"www2.acadiau.ca", "acadiau.ca", "www.acadiau.ca"}
        and bool(re.fullmatch(
            r"/academics/(?:undergraduate|graduate)/[a-z0-9-]+\.html",
            parsed.path,
        ))
        and parsed.path.rsplit("/", 1)[-1] not in {
            "honours.html", "money-matters.html", "current-students.html",
            "forms.html", "supervisor-information.html",
            "frequently-asked-questions.html",
        }
    )


def scope_programme_html(html: str, url: str) -> str:
    if not is_programme_url(url):
        return html
    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one("main#main")
    heading = soup.find("h1")
    if main is None or heading is None:
        return html
    # This widget contains every nationality's requirements for every subject,
    # plus audience-dependent tuition. It is not evidence for the current route.
    for node in main.select("#requirements-fees, .modal, nav, footer, script, style"):
        node.decompose()
    # Keep the page's own title, not the institution prefix or a guessed award.
    name = re.sub(
        r"^Acadia University\s*[-–—:]\s*", "",
        heading.get_text(" ", strip=True),
    )
    if not name:
        return html
    heading.extract()
    title = soup.new_tag("h1")
    title.string = name
    main.insert(0, title)
    return str(main)
