"""ACAP's indicated-campus restriction is not a course-wide exclusion."""
import re
from urllib.parse import urlsplit
from bs4 import BeautifulSoup


def eligibility_html(html: str, url: str | None) -> str:
    if (urlsplit(url or "").hostname or "").lower() not in {"acap.edu.au", "www.acap.edu.au"}:
        return html
    if "indicated campuses" not in html.lower():
        return html
    soup = BeautifulSoup(html, "html.parser")
    root = soup.find("main") or soup
    for text in root.find_all(string=lambda value: value and "indicated campuses" in value.lower()):
        note = text.parent
        box = note.parent
        row = box.parent if box else None
        if not row or not row.find("strong", string=re.compile(r"^\s*Available at\s*$", re.I)):
            continue
        campuses = box.find("span")
        if not campuses:
            continue
        # A real, unmarked campus must remain. Online alone never qualifies.
        parts = re.split(r"[,;/]|\band\b", campuses.get_text(" ", strip=True))
        if any(re.fullmatch(r"\s*(?:Melbourne|Sydney|Adelaide|Brisbane|Perth)\s*", value, re.I)
               for value in parts):
            html = html.replace(str(note), "", 1)
    return html


def apply_campus_authority(html, url, payload, evidence):
    """Use this course's labelled availability, not other navigation campuses."""
    if (urlsplit(url or "").hostname or "").lower() not in {"acap.edu.au", "www.acap.edu.au"}:
        return
    soup = BeautifulSoup(html, "html.parser")
    root = soup.find("main")
    if root is None:
        return
    for label in root.find_all("strong", string=re.compile(r"^\s*Available at\s*$", re.I)):
        row = label.parent.parent
        box = row.find("span")
        if box is None:
            continue
        names = re.split(r"[,;/]|\band\b", box.get_text(" ", strip=True))
        restriction = "indicated campuses" in row.get_text(" ", strip=True).lower()
        cities = []
        unrestricted_online = False
        for name in names:
            if restriction and "^" in name:
                continue
            name = name.strip()
            if re.fullmatch(r"Melbourne|Sydney|Adelaide|Brisbane|Perth", name, re.I):
                cities.append(name)
            if name.lower() == "online":
                unrestricted_online = True
        if not cities:
            continue
        values = {"course_location": ", ".join(dict.fromkeys(cities))}
        if restriction and not unrestricted_online:
            values["study_mode"] = "On Campus"
        for field, value in values.items():
            payload[field] = value
            payload.setdefault("extraction_method", {})[field] = "acap.international_campus"
            evidence.append({"field_key": field, "value": value, "confidence": 1.0,
                             "method": "acap.international_campus", "source_url": url,
                             "snippet": row.get_text(" ", strip=True)[:1200]})
        return
