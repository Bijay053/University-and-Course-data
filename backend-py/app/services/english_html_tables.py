"""Column-bound HTML IELTS evidence, limited to explicitly named programmes."""
import hashlib
import json
import re

from bs4 import BeautifulSoup

SCHEMA = 1
FIELDS = ("ielts_overall", "ielts_listening", "ielts_reading", "ielts_writing", "ielts_speaking")


def text(value):
    return " ".join(str(value or "").split())


def profiles(html):
    soup = BeautifulSoup(html, "html.parser")
    result = []
    for table in soup.find_all("table"):
        # Definition-list accordions commonly carry the test and qualification
        # in their dt. Ignore headings nested in preceding tables' cells.
        container = table.find_parent("dd")
        heading = container.find_previous_sibling("dt") if container else None
        nearest_test = next((h for h in table.find_all_previous(
            ["h1", "h2", "h3", "h4", "h5", "dt"]
        ) if not h.find_parent("table") and re.search(
            r"IELTS|TOEFL|Pearson|PTE|Cambridge|LanguageCert", h.get_text(" ", strip=True), re.I
        )), None)
        if nearest_test and not re.search(r"\bIELTS\b", nearest_test.get_text(" ", strip=True), re.I):
            continue
        heading = heading or nearest_test
        scope = heading.get_text(" ", strip=True) if heading else ""
        if not re.search(r"\bIELTS\b", scope, re.I):
            continue
        level = ("postgraduate_research" if re.search(r"research|doctoral", scope, re.I) else
                 "undergraduate" if re.search(r"undergraduate|bachelor", scope, re.I) else
                 "postgraduate_taught" if re.search(r"postgraduate|master", scope, re.I) else None)
        if not level:
            continue
        preceding = []
        for node in table.previous_siblings:
            if getattr(node, "name", None) and node.name != "table":
                preceding.append(node.get_text(" ", strip=True))
        qualifier = " ".join(reversed(preceding))
        if re.search(r"mandatory|in[- ]sessional|lower IELTS|conditional", qualifier, re.I):
            continue
        rows = table.find_all("tr")
        if not rows:
            continue
        columns = [text(c.get_text(" ", strip=True)).casefold()
                   for c in rows[0].find_all(["th", "td"], recursive=False)]
        required = {"overall", "listening", "reading", "writing", "speaking"}
        if len(columns) != 6 or set(columns[1:]) != required:
            continue
        header_quote = rows[0].get_text(" ", strip=True)
        for row in rows[1:]:
            cells = row.find_all(["th", "td"], recursive=False)
            # Spanned prose such as "at most two bands at 5.5" must never
            # become five independent numeric minima.
            if len(cells) != len(columns) or any(
                str(c.get("colspan", 1)) != "1" or str(c.get("rowspan", 1)) != "1" for c in cells
            ):
                continue
            values = [text(c.get_text(" ", strip=True)) for c in cells]
            if not values[0] or not all(re.fullmatch(r"[0-9](?:\.[05])?", v) for v in values[1:]):
                continue
            scores = {"ielts_" + column: float(value) for column, value in zip(columns[1:], values[1:])}
            if not all(0 < v <= 9 for v in scores.values()) or any(
                scores[k] > scores["ielts_overall"] for k in FIELDS[1:]
            ):
                continue
            profile = {"scope": scope, "level": level, "label": values[0],
                       "headerQuote": header_quote, "quote": row.get_text(" ", strip=True),
                       "scores": scores}
            profile["id"] = hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()
            result.append(profile)
    return list({p["id"]: p for p in result}.values())


def profile_for(candidate, source):
    proof = candidate.get("applicability") or {}
    if not isinstance(proof, dict) or source.get("_englishTablesSchema") != SCHEMA:
        return None
    matches = [p for p in source.get("_englishTables", []) if p["id"] == proof.get("tableId")]
    if len(matches) != 1 or not source.get("verified") or source.get("sourceType") != "html":
        return None
    profile = matches[0]
    body = text(source.get("_fullText") or source.get("text"))
    if any(text(profile[k]) not in body for k in ("scope", "headerQuote", "quote")):
        return None
    if text(proof.get("guidanceQuote")) != text(profile["quote"]) or proof.get("page") is not None:
        return None
    changes = candidate.get("changes", [])
    if len(changes) != 5 or {c.get("field") for c in changes} != set(FIELDS):
        return None
    for change in changes:
        value = change.get("newValue")
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or value != profile["scores"].get(change.get("field"))
                or text(change.get("quote")) != text(profile["quote"]) or change.get("page") is not None):
            return None
    return profile


def named_match(row, profile, course):
    """No school/subject inference from a title; require an exact named cohort."""
    from app.services.document_ielts_matching import identity_matches
    from app.services.document_course_scope import stored_level
    if not course.get("verified") or course.get("provenance") == "existing-record-and-document":
        return False
    title = text(course.get("courseTitle"))
    body = text(course.get("_fullText") or course.get("text"))
    if not title or title not in body or not identity_matches(row.course_name, title):
        return False
    level = stored_level(row)
    if (re.fullmatch(r"doctorate|doctoral|ph\.?d", text(getattr(row, "degree_level", None)), re.I)
            and re.search(r"\bPh\.?D\b", title, re.I)):
        level = "postgraduate_research"
    if profile["level"] != level:
        return False
    label = profile["label"]
    # A PhD programme explicitly named for its school is not a blanket
    # default for unrelated research degrees within that school.
    if level == "postgraduate_research":
        label = re.sub(r"^School\s+of\s+", "", label, flags=re.I)
    if identity_matches(row.course_name, label) and identity_matches(title, label):
        return True
    # A table may use an official programme acronym shown in the course's H1.
    # The model cannot introduce aliases or infer school membership.
    code = re.search(r"(?:^|[-:])\s*([A-Z][A-Z0-9]{2,11})\s*$", label)
    return bool(code and re.search(r"\(" + re.escape(code[1]) + r"\)", title, re.I))


def checked_profile(row, candidate, source, course):
    profile = profile_for(candidate, source)
    proof = candidate.get("applicability") or {}
    body = text(course.get("_fullText") or course.get("text"))
    if (not profile or not named_match(row, profile, course)
            or text(proof.get("courseQuote")) != text(course.get("courseTitle"))
            or text(proof.get("courseQuote")) not in body):
        return None
    for fact in course.get("facts", []):
        if fact.get("field") not in FIELDS:
            continue
        try:
            values = fact.get("normalized") or fact.get("value")
            values = values if isinstance(values, dict) else {fact["field"]: values}
            for key, value in values.items():
                if key in FIELDS and float(value) != profile["scores"][key]:
                    return None
        except (TypeError, ValueError):
            return None
    return profile


def candidate(row, source, course):
    if source.get("_englishTablesSchema") != SCHEMA:
        return None
    choices = [p for p in source.get("_englishTables", []) if named_match(row, p, course)]
    if len(choices) != 1:
        return None
    profile = choices[0]
    result = {"id": row.id, "matched": True,
              "applicability": {"tableId": profile["id"], "guidanceQuote": profile["quote"],
                               "courseQuote": course["courseTitle"], "page": None},
              "changes": [{"field": key, "newValue": profile["scores"][key],
                           "quote": profile["quote"], "page": None} for key in FIELDS]}
    return result if checked_profile(row, result, source, course) else None
