"""Deterministic, audience-owned international tuition schedules.

Keep scoped table semantics out of the generic flattened-text fee parser.
Unknown awards/variants fail closed rather than borrowing a similar price.
"""
import re
from urllib.parse import urljoin, urlparse


def explicit_international_fee_links(html, course_url):
    """Bounded same-origin explicit tuition links; no nav/ancillary guesses."""
    from bs4 import BeautifulSoup
    origin = urlparse(course_url)
    soup = BeautifulSoup(html or "", "html.parser")
    links = []
    for anchor in soup.find_all("a", href=True):
        label = anchor.get_text(" ", strip=True).lower()
        if not re.search(r"\binternational\b.*\b(?:tuition|fees)\b", label):
            continue
        target = urlparse(urljoin(course_url, anchor["href"]))
        if (
            target.scheme not in {"http", "https"}
            or target.netloc.lower() != origin.netloc.lower()
            or target.username or target.password
            or re.search(r"scholarship|deposit|accommodation|living.cost", target.path + " " + label)
        ):
            continue
        clean = target._replace(fragment="").geturl()
        if clean not in links:
            links.append(clean)
    return links[:4]


def leeds_trinity_link_only_fees(html, course_url):
    """Domestic values and the explicit empty international panel, if present."""
    if urlparse(course_url).hostname not in {"leedstrinity.ac.uk", "www.leedstrinity.ac.uk"}:
        return None
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "html.parser")
    domestic = []
    linked = False
    for box in soup.select(".lt-repo-course-fees__fees-box"):
        heading = box.find(["h2", "h3", "h4"])
        label = heading.get_text(" ", strip=True).lower() if heading else ""
        text = box.get_text(" ", strip=True)
        amounts = [float(a.replace(",", "")) for a in re.findall(r"£\s*(\d[\d,]*)", text)]
        if label == "uk home fees":
            domestic.extend(amounts)
        elif label == "international fees" and not amounts:
            linked = bool(explicit_international_fee_links(str(box), course_url))
    return domestic if linked else None


def leeds_trinity_study_year(html):
    """Only the selected study-year control, not arbitrary years in prose."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "html.parser")
    control = soup.select_one(".lt-section-course-details__year-cta")
    text = control.get_text(" ", strip=True) if control else ""
    return int(text) if re.fullmatch(r"20\d{2}", text) else None


def parse_leeds_trinity_schedule(soup, source_url):
    """Return None for other sources, [] for an unrecognised official layout."""
    parsed = urlparse(source_url)
    if parsed.hostname not in {"leedstrinity.ac.uk", "www.leedstrinity.ac.uk"}:
        return None
    if parsed.path.rstrip("/") != "/international/international-fees-and-funding/tuition-fees":
        return None
    records = []
    years = set()
    for table in soup.find_all("table"):
        heading = table.find_previous("h2")
        title = heading.get_text(" ", strip=True) if heading else ""
        # Archived accordion tables must never override the current schedule.
        year = re.fullmatch(r"Tuition fees for the (20\d{2})[-–](?:20)?\d{2} academic year", title)
        if not year:
            continue
        years.add(int(year.group(1)))
        level_heading = table.find_previous("h3")
        level = level_heading.get_text(" ", strip=True).lower() if level_heading else ""
        if level not in {"undergraduate", "postgraduate"}:
            continue
        for row in table.find_all("tr")[1:]:
            cells = row.find_all(["td", "th"], recursive=False)
            if len(cells) != 2:
                continue
            name, fee = (c.get_text(" ", strip=True).replace("\u200b", "") for c in cells)
            amount = re.search(r"£\s*(\d[\d,]*)", fee)
            if not amount or re.search(r"placement year|discount|deposit", name, re.I):
                continue
            records.append({
                "program_pattern": name,
                "international_fee": float(amount.group(1).replace(",", "")),
                "currency": "GBP",
                "fee_year": int(year.group(1)),
                "per": "year" if level == "undergraduate" or re.search(r"per year", name + " " + fee, re.I) else None,
                "degree_level": level,
                "source_url": source_url,
                "audience": "international",
                "schedule_semantics": "leeds-trinity-current-v1",
                "snippet": f"{title} | {level} | {name} | {fee}",
            })
    # A heading that merely looks current is insufficient when several years
    # compete. Do not let source order choose a stale schedule.
    if len(years) != 1:
        return []
    seen = {}
    for record in records:
        key = (record["degree_level"], record["program_pattern"])
        value = (record["international_fee"], record["per"])
        if key in seen and seen[key] != value:
            return []
        seen[key] = value
    return records


def _title(value):
    value = value.lower().replace("\u200b", "").replace("&", " and ")
    value = re.sub(r"\b(?:ba|bsc|ma|msc|llm|joint|hons)\b", " ", value)
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def match_scoped_schedule(course_name, records, degree_level=None, course_url=None):
    """Exact subject/variant join, or an explicitly inclusive published UG rule."""
    scoped = [r for r in records if r.get("schedule_semantics") == "leeds-trinity-current-v1"]
    if not scoped:
        return None
    query = _title(course_name)
    level = (degree_level or "").lower()
    path = urlparse(course_url or "").path
    if "/undergraduate/" in path or re.search(r"\b(?:ba|bsc|bachelor)\b", course_name, re.I):
        level = "undergraduate"
    elif "/postgraduate/" in path or re.search(r"\b(?:ma|msc|mba|llm|master|pgce|mbr)\b", course_name, re.I):
        level = "postgraduate"
    # Published award-wide PGCE row includes subject specialisms. MBA's
    # expansion is an award synonym, not permission to match placements.
    if re.match(r"^pgce(?:\s|$)", query):
        query = "pgce"
    elif query == "mba business administration":
        query = "mba"
    for record in scoped:
        query_award = re.search(r"\b(ba|bsc|ma|msc|mba|llm|pgce|mbr)\b", course_name, re.I)
        row_award = re.search(r"\b(ba|bsc|ma|msc|mba|llm|pgce|mbr)\b", record["program_pattern"], re.I)
        if query_award and row_award and query_award[1].lower() != row_award[1].lower():
            continue
        if record["degree_level"] == level and _title(record["program_pattern"]) == query:
            return record, "exact"
    # Current schedule explicitly covers all UG (including foundation), not
    # the historical "three-year degree" rule. Never use it for nursing.
    if level == "undergraduate" and not re.search(r"\bnursing\b", query):
        for record in scoped:
            if record["program_pattern"].lower() == "undergraduate courses (excluding nursing)":
                return record, "exact"
    return None, "none"