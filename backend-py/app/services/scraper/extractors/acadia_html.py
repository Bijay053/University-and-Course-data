"""Isolate Acadia's programme content from its shared all-programme widgets."""
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup


CATALOGUE_URL = "https://www2.acadiau.ca/academics.html"
_OFFSITE_AWARDS = {
    "https://acadiadiv.ca/bth/": "Bachelor of Theology",
    "https://acadiadiv.ca/mdiv/": "Master of Divinity",
    "https://acadiadiv.ca/dmin/": "Doctor of Ministry",
    "https://med.acadiau.ca/home.html": "Master of Education",
    "https://www.nsphdeducation.ca/": "PhD in Education",
}


def linked_award_cards(html, page_url):
    """An offsite destination is a candidate only while Acadia lists its card."""
    if page_url != CATALOGUE_URL:
        return {}
    soup = BeautifulSoup(html, "html.parser")
    return {a["href"]: _OFFSITE_AWARDS[a["href"]] for a in soup.select(
        "main #program_sort .profile_content a[href], main #program_sort_grad .profile_content a[href]"
    ) if a["href"] in _OFFSITE_AWARDS and a.get_text(strip=True)}


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


def guarded_widget_tuition(html, url, rule):
    """Only the validated tuition rule may read the otherwise excluded widget."""
    from app.services.scraper.repair_fee_authority import FEE_EVIDENCE_GUARD, missing_fee_authority
    if (not is_programme_url(url) or not isinstance(rule, dict)
            or rule.get("evidence_guard") != FEE_EVIDENCE_GUARD):
        return None
    from app.services.scraper.ai_extractor_run import apply_extraction_rules
    raw, method = apply_extraction_rules(html, {"international_fee": rule})["international_fee"]
    if raw is None:
        return None
    proof = missing_fee_authority(html, raw)
    if proof["supported"]:
        return float(proof["amount"]), method, proof["excerpt"]
    return None
