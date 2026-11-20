"""Course-owned Cardiff Met facts and explicitly linked PGCE requirements.

Central prices are eligible only for a verified Cardiff-campus award. Partner
institutions, ambiguous awards and conflicting course prices are not inferred.
"""
import asyncio
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

HOSTS = {"cardiffmet.ac.uk", "www.cardiffmet.ac.uk"}
BASE = "https://www.cardiffmet.ac.uk"
FEES = BASE + "/international-students/fees-and-finance/"
PGCE = BASE + "/courses/postgraduate/pgce-secondary/"
ENGLISH = BASE + "/international-students/english-language-requirements/"
SELT = ENGLISH + "accepted-secure-english-language-tests/"
MONTHS = ("January February March April May June July August September October "
          "November December").split()
IELTS = ("ielts_overall", "ielts_listening", "ielts_reading",
         "ielts_writing", "ielts_speaking")
START_LABELS = ("starting", "start date", "start dates", "intake", "intakes")


def is_course(url):
    u = urlsplit(url or "")
    return u.hostname in HOSTS and bool(re.fullmatch(
        r"/courses/(?:postgraduate|undergraduate|research)/[^/]+/?", u.path))


def _main(html):
    soup = BeautifulSoup(html or "", "html.parser")
    main = soup.find("main")
    if main:
        for node in main.select("nav,header,footer,script,style"):
            node.decompose()
    return main


async def prefetch_sources(fetch):
    """Preload the official fee, programme and linked English profiles."""
    async def read(url):
        try:
            return url, await asyncio.wait_for(fetch(url), timeout=20)
        except Exception:
            return url, ""
    return dict(await asyncio.gather(*(read(url) for url in (FEES, PGCE, ENGLISH, SELT))))


def _write(payload, evidence, field, value, quote, source, method, *, replace=False):
    if not replace and payload.get(field) not in (None, "", 0, []):
        return
    if replace:
        for row in evidence:
            if row.get("field_key") == field:
                row["decision_status"] = "superseded"
    payload[field] = value
    evidence.append({"field_key": field, "value": value, "confidence": 1.0,
                     "method": method, "snippet": quote, "source_url": source})


def _intakes(text):
    return [month for month in MONTHS if re.search(r"\b" + month + r"\b", text)]


def _fee_group(title):
    # Award evidence must come from the current official H1, never a stored
    # category, a related-course card or a guess from subject words.
    if re.search(r"partner|executive|foundation|pgdip|pgcert|mres|mphil", title, re.I):
        return None
    if re.match(r"PGCE\b", title, re.I):
        return "PGCE"
    if re.search(r"\bMBA\b|Master of Business Administration", title, re.I):
        return "MBA"
    if re.search(r"\bPhD\b|Doctorate|Doctor of ", title, re.I):
        return "Postgraduate Doctoral Degree"
    if re.search(r"\bMSc\b|\bMA\b|Master(?:'s)? (?:Degree|of )", title, re.I):
        return "Postgraduate Masters Degree"
    if re.search(r"\b(?:BSc|BA|BEng|LLB|BMus|BDes)\b|Bachelor of ", title, re.I):
        if re.search(r"top.up", title, re.I):
            return "Top-Up Degree"
        return "Undergraduate Degree"
    return None


def _central_fee(html, group):
    main = _main(html)
    if main is None:
        return None
    title = main.find("h1")
    if not title or not re.search(r"fees|finance", title.get_text(), re.I):
        return None
    if not re.search(r"international (?:fee-paying )?students|international tuition",
                     main.get_text(" ", strip=True), re.I):
        return None
    found = set()
    for table in main.find_all("table"):
        text = table.get_text(" ", strip=True).replace("\u200b", "")
        years = re.findall(r"Course Fee\s+(20\d{2})\s*/\s*\d{2,4}", text, re.I)
        if len(set(years)) != 1:
            continue
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"], recursive=False)
            values = [c.get_text(" ", strip=True).replace("\u200b", "").strip()
                      for c in cells]
            if len(values) != 3 or values[0] != group:
                continue
            prices = re.findall(r"£\s*([\d,]+(?:\.\d+)?)", values[2])
            if len(prices) != 1:
                continue
            # Explicit "Course Fee", not a deposit or scholarship. Preserve
            # the published fee year; never relabel 2026/27 as 2027/28.
            term = "Annual" if re.search(r"per year", values[2], re.I) else "Full Course"
            found.add((float(prices[0].replace(",", "")), int(years[0]), term,
                       " | ".join(values) + " | Course Fee " + years[0] + "/" +
                       str(int(years[0]) + 1)[-2:]))
    return next(iter(found)) if len(found) == 1 else None


def _facts(main):
    values = {}
    for item in main.select(".block-12__item"):
        label = item.find(["h3", "h4"])
        description = item.select_one(".block-12__item-description")
        if label and description:
            key = label.get_text(" ", strip=True).lower()
            values.setdefault(key, set()).add(description.get_text(" ", strip=True))
    # Different selectable offerings are not one fact; do not choose the last
    # DOM card or combine start months across unselected variants.
    return {key: next(iter(items)) for key, items in values.items() if len(items) == 1}


def _apply_fee(title, location, payload, evidence, sources, referral=""):
    group = _fee_group(title)
    campus = bool(re.fullmatch(r"(?:Llandaff|Cyncoed)(?:\s+Campus)?", location, re.I))
    if not campus or not group or payload.get("international_fee") not in (None, "", 0):
        return
    fee = _central_fee(sources.get(FEES, ""), group)
    if fee:
        amount, year, term, quote = fee
        for field, value in (("international_fee", amount), ("currency", "GBP"),
                             ("fee_year", year), ("fee_term", term)):
            _write(payload, evidence, field, value, quote + "\n" +
                   title + " — Location: " + location + referral, FEES,
                   "fee.cardiffmet_international_profile", replace=True)


def _apply_linked_selt(main, title, location, url, payload, evidence, sources):
    """Resolve a course-owned IELTS overall against its explicitly linked table."""
    if not re.fullmatch(r"(?:Llandaff|Cyncoed)(?:\s+Campus)?", location, re.I):
        return
    def owns_link(node, target):
        return any(
            a.get("href", "").rstrip("/") in (target.rstrip("/"), urlsplit(target).path.rstrip("/"))
            for a in node.find_all("a", href=True)
        )
    hub = _main(sources.get(ENGLISH, ""))
    tests = _main(sources.get(SELT, ""))
    if not owns_link(main, ENGLISH) or hub is None or tests is None:
        return
    if not owns_link(hub, SELT) or not hub.find("h1") or not tests.find("h1"):
        return
    if not re.search(r"English Language Requirements", hub.h1.get_text(), re.I) or not re.search(
        r"Accepted Secure English Language Tests", tests.h1.get_text(), re.I
    ):
        return
    course_scores = []
    all_scores = set()
    for p in main.find_all("p"):
        text = " ".join(p.get_text(" ", strip=True).split())
        scores = re.findall(r"\bIELTS\s+(?:overall\s+|score\s+(?:of\s+)?)?([0-9](?:\.[05])?)\b", text, re.I)
        all_scores.update(float(score) for score in scores)
        if re.search(r"\bIELTS\b", text, re.I) and re.search(
            r"component|sub.?score|each band|reading|writing|listening|speaking|pre.sessional"
            r"|\b(?:may|normally|typically|recommend\w*|higher|alternative|foundation)\b", text, re.I
        ):
            return
        heading = p.find_previous(["h2", "h3", "h4", "h5"])
        if not heading or not re.search(
            r"International Applicants|English Language Requirements", heading.get_text(), re.I
        ):
            continue
        # Leave course-specific band requirements to their own evidence.
        if re.search(
            r"component|sub.?score|each band|reading|writing|listening|speaking|pre.sessional"
            r"|\b(?:may|normally|typically|recommend|higher|alternative|foundation)\b", text, re.I
        ):
            continue
        if not re.search(
            r"(?:require(?:d)?|need to provide evidence of fluency to at least).{0,45}\bIELTS", text, re.I
        ) or len(set(re.findall(r"\b[0-9](?:\.[05])?\b", text))) != 1:
            continue
        course_scores.extend((float(score), text) for score in scores)
    if len({score for score, _ in course_scores}) != 1:
        return
    overall, course_quote = course_scores[0]
    if overall not in (6.0, 6.5) or all_scores != {overall}:
        return
    matches = []
    for table in tests.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        headers = [c.get_text(" ", strip=True) for c in rows[0].find_all(["th", "td"], recursive=False)]
        columns = [i for i, h in enumerate(headers) if re.fullmatch(
            rf"Courses with {overall:.1f} IELTS Requirements", h, re.I)]
        if len(columns) != 1:
            continue
        for row in rows[1:]:
            cells = row.find_all(["td", "th"], recursive=False)
            if len(cells) != len(headers) or not re.match(
                r"IELTS Academic\b", cells[0].get_text(" ", strip=True), re.I
            ):
                continue
            text = " ".join(cells[columns[0]].get_text(" ", strip=True).split())
            if overall == 6.0 and re.fullmatch(
                r"Overall score:\s*6\.0\s*\(with no component below 5\.5\)", text, re.I
            ):
                matches.append((dict(zip(IELTS, (6.0, 5.5, 5.5, 5.5, 5.5))), text))
            elif overall == 6.5 and re.fullmatch(
                r"6\.5 overall with 6\.5 in both reading and writing, and a minimum of 6\.0 in the remaining components",
                text, re.I
            ):
                matches.append((dict(zip(IELTS, (6.5, 6.0, 6.5, 6.5, 6.0))), text))
    if len(matches) != 1:
        return
    values, table_quote = matches[0]
    if any(payload.get(field) not in (None, "", value) for field, value in values.items()):
        return
    quote = (
        f"Current course: {title}\nCourse source: {url}\nCourse requirement: {course_quote}\n"
        f"Course-linked guidance: {ENGLISH}\nLinked IELTS Academic table, "
        f"Courses with {overall:.1f} IELTS Requirements: {table_quote}"
    )
    for field, value in values.items():
        _write(payload, evidence, field, value, quote, SELT, "english.cardiffmet_linked_selt")


def apply_authority(html, url, payload, evidence, sources=None):
    if not is_course(url):
        return
    main = _main(html)
    if main is None or not main.find("h1"):
        return
    title = main.find("h1").get_text(" ", strip=True)
    sources = sources or {}
    facts = _facts(main)
    for label in START_LABELS:
        text = facts.get(label, "")
        months = _intakes(text)
        if months:
            _write(payload, evidence, "intake_months", months,
                   label + ": " + text, url, "intake.cardiffmet_course_fact",
                   replace=True)
    location = facts.get("location", "")
    # A partner course can have different fees and admission requirements.
    _apply_fee(title, location, payload, evidence, sources)
    _apply_linked_selt(main, title, location, url, payload, evidence, sources)
    # Child PGCE subjects explicitly refer readers to the shared Secondary
    # programme for admission and course information. A global menu does not.
    links = [a for a in main.find_all("a", href=True)
             if a.get("href", "").rstrip("/") in {
                 "/courses/postgraduate/pgce-secondary", PGCE.rstrip("/")}
             and re.search(r"PGCE Secondary course page", a.get_text(" ", strip=True), re.I)]
    if not re.match(r"PGCE Secondary\b", title, re.I) or not links:
        return
    parent = _main(sources.get(PGCE, ""))
    if parent is None or not parent.find("h1") or not re.match(
            r"(?:PGCE Secondary\b|Secondary \(11-18 Age Range\) with QTS - PGCE$)",
            parent.find("h1").get_text(" ", strip=True), re.I):
        return
    if not location:
        _apply_fee(title, _facts(parent).get("location", ""), payload, evidence,
                   sources, "\nProgramme referral: " + PGCE)
    pairs = []
    for p in parent.find_all("p"):
        text = " ".join(p.get_text(" ", strip=True).split())
        match = re.search(r"international applicants.{0,150}IELTS score of "
                          r"(\d(?:\.\d)?) overall with no sub score lower than "
                          r"(\d(?:\.\d)?)", text, re.I)
        if match:
            pairs.append((float(match[1]), float(match[2]), text))
        if re.search(r"programme typically begins.{0,60}\bSeptember\b", text, re.I):
            if not any(_intakes(facts.get(k, "")) for k in START_LABELS):
                _write(payload, evidence, "intake_months", ["September"], text, PGCE,
                       "intake.cardiffmet_linked_pgce", replace=True)
    # An unparsed course-owned IELTS exception must not be replaced by a parent.
    child_text = main.get_text(" ", strip=True)
    if len(pairs) == 1 and not re.search(r"IELTS\s*(?:score|overall|\d)", child_text, re.I):
        overall, band, quote = pairs[0]
        if 0 < band <= overall <= 9:
            for field in IELTS:
                _write(payload, evidence, field, overall if field == IELTS[0] else band,
                       quote + "\nCourse-owned referral: " + links[0].get_text(" ", strip=True),
                       PGCE, "english.cardiffmet_linked_pgce", replace=True)
