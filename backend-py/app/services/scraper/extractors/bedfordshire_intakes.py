"""Course-owned admissions JSON used to populate Bedfordshire's dropdowns."""
import json
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from .base import ExtractionResult

METHOD = "intake:bedfordshire_admission_data"
MONTHS = "January February March April May June July August September October November December".split()


def extract_admission_intakes(html, url, *, location=None, _location_only=False):
    """Return (handled, results); ambiguous data must not fall into page-wide regex."""
    parsed = urlsplit(url)
    if parsed.hostname not in {"beds.ac.uk", "www.beds.ac.uk"} or not parsed.path.startswith("/courses/"):
        return False, []
    soup = BeautifulSoup(html, "html.parser")
    assignments = []
    for script in soup.find_all("script"):
        text = script.string or script.get_text()
        for match in re.finditer(r"\b(?:const|let|var)\s+courseDataList\s*=\s*", text):
            assignments.append(text[match.end():].lstrip())
    if not assignments:
        return soup.find("select", id="intakeMonth") is not None, []
    if len(assignments) != 1 or parsed.query:
        return True, []
    try:
        records, end = json.JSONDecoder().raw_decode(assignments[0])
    except (ValueError, TypeError):
        return True, []
    quote = assignments[0][:end]
    heading = soup.find("h1")
    if not heading or not isinstance(records, list) or len(records) > 100 or len(quote) > 32000:
        return True, []
    title = heading.get_text(" ", strip=True)
    # A placement course can have its own full-time admissions. A Foundation
    # Year is a different dropdown option and must match the actual title.
    if re.search(r"part.time|distance|online", title, re.I):
        return True, []
    foundation_year = bool(re.search(r"foundation\s+year", title, re.I))
    selected = []
    seen = set()
    for record in records:
        if not isinstance(record, dict):
            return True, []
        modes = record.get("studyModes") or []
        if not isinstance(modes, list) or not modes:
            return True, []
        if any(not isinstance(mode, dict) for mode in modes):
            return True, []
        values = [str(mode.get("value", "")).casefold() for mode in modes]
        expected_mode = "foundationyear|full-time" if foundation_year else "full-time"
        if any(value != expected_mode for value in values):
            continue
        key = record.get("key")
        if not isinstance(key, str) or not key or key in seen:
            return True, []
        seen.add(key)
        panels = [p for p in soup.find_all(attrs={"data-course-admission": key})
                  if p.get("data-study-type") == "International"]
        application = any(
            node.name == "a" and node.get("data-id") == "International"
            and urlsplit(node.get("href", "")).hostname == "evision.beds.ac.uk"
            and not any(p.name in {"nav", "aside", "footer"} for p in node.parents)
            for node in soup.find_all(attrs={"data-course-admission": key})
        )
        panel_text = " ".join(p.get_text(" ", strip=True) for p in panels)
        if (not panel_text and not application) or re.search(
                r"not\s+(?:(?:currently|presently)\s+)?(?:available|open|eligible)|cannot\s+apply|home\s+students\s+only",
                panel_text, re.I):
            continue
        dates = record.get("intakeMonths")
        if not isinstance(dates, list) or not dates:
            return True, []
        months = []
        for date in dates:
            if not isinstance(date, dict) or date.get("value") not in MONTHS:
                return True, []
            if date.get("displayText") != date["value"]:
                return True, []
            months.append(date["value"])
        location_data = record.get("location") or {}
        if not isinstance(location_data, dict) or not isinstance(location_data.get("value", ""), str):
            return True, []
        selected.append((location_data.get("value", ""), set(months)))
    if not selected:
        return True, []
    # Existing location is only an option selector, not verified location
    # evidence. Match it to a published label; never silently choose the first.
    selection = None
    if location:
        fold = lambda value: " ".join(re.findall(
            r"\w+", re.sub(r"\bcampus\b", "", value, flags=re.I).casefold()))
        labels = {label for label, _ in selected if fold(label) == fold(location)}
        if len(labels) == 1:
            selection = labels.pop()
            selected = [(label, dates) for label, dates in selected if label == selection]
    locations = {location for location, _ in selected}
    if len(locations) > 1 and not _location_only:
        by_location = {
            location: frozenset().union(*(months for loc, months in selected if loc == location))
            for location in locations
        }
        if len(set(by_location.values())) != 1:
            return True, []
    months = [month for month in MONTHS if any(month in dates for _, dates in selected)]
    return True, [ExtractionResult(
        field_key="intake_months", value=months, normalized={"intake_months": months},
        confidence=0.98, snippet=quote, method=METHOD,
        extras={"selected_location": selection, "applicable_locations": sorted(locations)},
    )]


def extract_admission_location(html, url):
    """Require one course-owned international campus, never a filter default.

    Reuse the existing award, study-option and international-application checks.
    Distinct applicable campuses remain ambiguous rather than assigning the
    first dropdown option to a row whose existing location is a placeholder.
    """
    # A campus fact does not require different delivery options to have the
    # same intake dates. Keep that restriction for the intake extractor.
    handled, results = extract_admission_intakes(html, url, _location_only=True)
    if not handled or len(results) != 1:
        return handled, []
    from .location import _normalise
    labels = results[0].extras.get("applicable_locations") or []
    cleaned = {}
    for label in labels:
        # This published option explicitly names its physical campus. It does
        # not authorize changing the course's separate delivery-mode field.
        blended = re.fullmatch(r"Blended\s+Learning\s*\(([^()]+\bcampus)\)", label, re.I)
        value = _normalise(blended[1] if blended else label)
        if not value:
            return True, []
        cleaned.setdefault(" ".join(value.casefold().split()), value)
    if len(cleaned) != 1:
        return True, []
    value = next(iter(cleaned.values()))
    return True, [ExtractionResult(
        field_key="course_location", value=value,
        normalized={"course_location": value}, confidence=0.98,
        snippet=results[0].snippet, method="location:bedfordshire_admission_data",
    )]
