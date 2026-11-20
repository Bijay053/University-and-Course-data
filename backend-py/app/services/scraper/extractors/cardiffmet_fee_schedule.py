"""Select explicitly scoped published annual schedules, never Home fee tables."""
import asyncio
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

BASE = "https://www.cardiffmet.ac.uk"
INDEX = BASE + "/support/fees-and-finance/tuition-fees/"
YEAR_PATH = re.compile(r"/support/fees-and-finance/tuition-fees/academic-year-(20\d{2})(\d{2})/")


def main(html):
    root = BeautifulSoup(html or "", "html.parser").find("main")
    if root:
        for node in root.select("nav,header,footer,aside,script,style"):
            node.decompose()
    return root


def schedule_year(url):
    parsed = urlsplit(url)
    match = YEAR_PATH.fullmatch(parsed.path)
    if (parsed.scheme != "https" or parsed.hostname != "www.cardiffmet.ac.uk"
            or parsed.query or parsed.fragment or not match
            or int(match[2]) != (int(match[1]) + 1) % 100):
        return None
    return int(match[1])


async def prefetch(fetch, index_html):
    root = main(index_html)
    if root is None or not root.find("h1") or not re.fullmatch(
        r"Tuition Fees", root.h1.get_text(" ", strip=True), re.I
    ):
        return {}
    urls = {urljoin(BASE, a["href"]) for a in root.select("a[href]")}
    urls = sorted((u for u in urls if schedule_year(u)), key=schedule_year, reverse=True)[:2]

    async def read(url):
        try:
            return url, await asyncio.wait_for(fetch(url), timeout=20)
        except Exception:
            return url, ""

    return dict(await asyncio.gather(*(read(url) for url in urls)))


def entry_year(course_main):
    selectors = course_main.select("select.years-select-course")
    if not selectors:
        return None
    years = set()
    for selector in selectors:
        options = selector.select("option[selected]") or selector.select("option")
        if len(options) != 1 or not re.fullmatch(r"20\d{2}", options[0].get("value", "")):
            return None
        years.add(int(options[0]["value"]))
    # The responsive desktop/mobile controls must agree.
    return next(iter(years)) if len(years) == 1 else None


def select_fee(sources, group, course_main, baseline):
    """Require current option, full-time course, international section and basis."""
    if not baseline:
        return None
    duration = []
    for item in course_main.select(".block-12__item"):
        heading = item.find(["h3", "h4"])
        value = item.select_one(".block-12__item-description")
        if heading and value and heading.get_text(" ", strip=True).lower() == "duration":
            duration.append(value.get_text(" ", strip=True))
    if len(set(duration)) != 1 or not re.search(r"\bfull[ -]?time\b", duration[0], re.I):
        return None
    year = entry_year(course_main)
    if year is None:
        return None
    matches = set()
    for url, html in sources.items():
        if schedule_year(url) != year:
            continue
        root = main(html)
        if root is None or not root.find("h1"):
            continue
        if not re.fullmatch(rf"Course Fees\s*[-–—]\s*Academic Year {year}/{str(year + 1)[-2:]}",
                            root.h1.get_text(" ", strip=True), re.I):
            continue
        for table in root.find_all("table"):
            audience = table.find_previous("h2")
            period = table.find_previous(["h2", "h3"])
            if not audience or not re.fullmatch(
                r"Full[ -]Time Programmes\s*[-–—]\s*International Students\s*\(including EU\)",
                audience.get_text(" ", strip=True), re.I,
            ):
                continue
            if not period or not re.fullmatch(
                rf"Academic Year {year}/{year + 1}", period.get_text(" ", strip=True), re.I
            ):
                continue
            rows = table.find_all("tr")
            headers = [c.get_text(" ", strip=True) for c in rows[0].find_all(["th", "td"])] if rows else []
            if headers != ["Programme", "Fee (£)"]:
                continue
            for row in rows[1:]:
                cells = [c.get_text(" ", strip=True).replace("\u200b", "")
                         for c in row.find_all(["td", "th"], recursive=False)]
                if len(cells) != 2:
                    continue
                label = cells[0]
                matched = (
                    group in {"Undergraduate Degree", "Top-Up Degree"}
                    and re.fullmatch(rf"Undergraduate\s*[-–—]\s*{year} Intake", label, re.I)
                    or group in {"Postgraduate Masters Degree", "MRes"}
                    and re.fullmatch(r"Postgraduate Masters \(including MRes\s*&\s*DMan\)", label, re.I)
                    or group in {"MBA", "PGCE"} and label == group
                    or group == "Postgraduate Doctoral Degree"
                    and re.fullmatch(rf"MPhil/PhD/DMan Research\*?\s*[-–—]\s*{year} Intake", label, re.I)
                )
                price = re.fullmatch(r"£?\s*([0-9][0-9,]*(?:\.[0-9]{2})?)", cells[1])
                if matched and price:
                    amount = float(price[1].replace(",", ""))
                    if amount > 0:
                        # Basis is corroborated by the university's international
                        # Course Fee table, not inferred from an academic-year heading.
                        quote = (f"{audience.get_text(' ', strip=True)} | Academic Year {year}/{year + 1}"
                                 f" | {label} | Fee (£): {cells[1]} | Basis guidance: {baseline[3]}")
                        matches.add((amount, year, baseline[2], quote, url))
    return next(iter(matches)) if len(matches) == 1 else None
