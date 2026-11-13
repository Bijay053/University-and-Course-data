"""Column-bound IELTS evidence with independently verified programme/cohort scope."""
import hashlib
import json
import re

from bs4 import BeautifulSoup

SCHEMA = 2
FIELDS = ("ielts_overall", "ielts_listening", "ielts_reading", "ielts_writing", "ielts_speaking")


def text(value):
    return " ".join(str(value or "").split())


def programme_name(value):
    """Intake annotations are not awards or different programme identities."""
    return re.sub(
        r"\s*\((?:January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+20\d{2}\)\s*$", "", text(value), flags=re.I,
    )


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
            if any(str(c.get("rowspan", 1)) != "1" for c in cells):
                continue
            if any(not re.fullmatch("[1-4]", str(c.get("colspan", 1))) for c in cells):
                continue
            values = []
            for cell in cells:
                span = str(cell.get("colspan", 1))
                if not re.fullmatch("[1-4]", span):
                    break
                values.extend([text(cell.get_text(" ", strip=True))] * int(span))
            if len(values) != len(columns) or not values[0]:
                continue
            numeric = all(re.fullmatch(r"[0-9](?:\.[05])?", v) for v in values[1:])
            # Numeric spans are ambiguous. Recognised conditional prose is
            # preserved as a constraint, never converted to four band minima.
            if numeric and any(str(c.get("colspan", 1)) != "1" for c in cells):
                continue
            constraint = None
            if numeric:
                scores = {"ielts_" + column: float(value) for column, value in zip(columns[1:], values[1:])}
            else:
                by_column = dict(zip(columns[1:], values[1:]))
                prose = {v for k, v in by_column.items() if k != "overall" and v not in {"", "-", "–"}}
                if len(prose) != 1 or not re.fullmatch(r"[0-9](?:\.[05])?", by_column["overall"]):
                    continue
                rule = next(iter(prose))
                match = re.fullmatch(
                    r"(two|2|one|1) bands? at ([0-9](?:\.[05])?)"
                    r"(?: as a minimum[.]?| and) [Oo]ther bands at ([0-9](?:\.[05])?) and above[.]?",
                    rule, re.I,
                )
                if not match or not 0 < float(match[2]) < float(match[3]) <= float(by_column["overall"]) <= 9:
                    continue
                scores = {"ielts_overall": float(by_column["overall"])}
                constraint = {"kind": "conditional_band_floor", "minimum": float(match[2]),
                              "threshold": float(match[3]), "maximumBelowThreshold": 2 if match[1].lower() in {"two", "2"} else 1,
                              "text": rule}
            if not all(0 < v <= 9 for v in scores.values()) or any(
                scores[k] > scores["ielts_overall"] for k in scores if k != "ielts_overall"
            ):
                continue
            profile = {"scope": scope, "level": level, "label": values[0],
                       "headerQuote": header_quote, "quote": row.get_text(" ", strip=True),
                       "scores": scores}
            if constraint:
                profile["bandConstraint"] = constraint
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
    if len(changes) != len(profile["scores"]) or {c.get("field") for c in changes} != set(profile["scores"]):
        return None
    for change in changes:
        value = change.get("newValue")
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or value != profile["scores"].get(change.get("field"))
                or text(change.get("quote")) != text(profile["quote"]) or change.get("page") is not None):
            return None
    return profile


def named_match(row, profile, course, siblings=()):
    """No school/subject inference from a title; require an exact named cohort."""
    from app.services.document_ielts_matching import identity_matches
    from app.services.document_course_scope import stored_level
    if not course.get("verified") or course.get("provenance") == "existing-record-and-document":
        return False
    title = text(course.get("courseTitle"))
    body = text(course.get("_fullText") or course.get("text"))
    if not title or title not in body or not identity_matches(programme_name(row.course_name), programme_name(title)):
        return False
    level = stored_level(row)
    if (re.fullmatch(r"doctorate|doctoral|ph\.?d", text(getattr(row, "degree_level", None)), re.I)
            and re.search(r"\bPh\.?D\b", title, re.I)):
        level = "postgraduate_research"
    confirmed_levels = {e["level"] for e in course.get("cohortEvidence", []) if e.get("level")}
    if len(confirmed_levels) > 1:
        return False
    if len(confirmed_levels) == 1:
        level = next(iter(confirmed_levels))
    if profile["level"] != level:
        return False
    label = profile["label"]
    # A PhD programme explicitly named for its school is not a blanket
    # default for unrelated research degrees within that school.
    if level == "postgraduate_research":
        label = re.sub(r"^School\s+of\s+", "", label, flags=re.I)
    if identity_matches(row.course_name, label) and identity_matches(title, label):
        return True
    # Explicit programme lists can establish scope without inventing school
    # membership (e.g. "Psychology all programmes", not "biomedical" substring).
    if ":" in label:
        terms = re.split(r",|\band\b", label.split(":", 1)[1], flags=re.I)
        for term in terms:
            term = re.sub(r"\ball\b|\bprogrammes?\b|\bprograms?\b", "", term, flags=re.I)
            words = re.findall(r"\w+", term.casefold())
            title_words = " ".join(re.findall(r"\w+", title.casefold()))
            if words and re.search(r"(?<!\w)" + re.escape(" ".join(words)) + r"(?!\w)", title_words):
                return True
    from app.services.english_course_cohorts import key
    cohort = [e for e in course.get("cohortEvidence", []) if key(e.get("label")) == key(label)]
    if cohort:
        # A general cohort must not swallow a narrower, unresolved exception.
        narrower = [p for p in siblings if p is not profile and p["level"] == level
                    and key(p["label"]).startswith(key(label)) and key(p["label"]) != key(label)
                    and (p["scores"] != profile["scores"] or p.get("bandConstraint") != profile.get("bandConstraint"))]
        if not narrower:
            return True
    # A table may use an official programme acronym shown in the course's H1.
    # The model cannot introduce aliases or infer school membership.
    code = re.search(r"(?:^|[-:])\s*([A-Z][A-Z0-9]{2,11})\s*$", label)
    return bool(code and re.search(r"\(" + re.escape(code[1]) + r"\)", title, re.I))


def checked_profile(row, candidate, source, course):
    profile = profile_for(candidate, source)
    proof = candidate.get("applicability") or {}
    body = text(course.get("_fullText") or course.get("text"))
    if (not profile or not named_match(row, profile, course, source.get("_englishTables", []))
            or text(proof.get("courseQuote")) != text(course.get("courseTitle"))
            or text(proof.get("courseQuote")) not in body):
        return None
    if final_year_restricted(row, profile, source, course):
        return None
    for fact in course.get("facts", []):
        if fact.get("field") not in FIELDS:
            continue
        try:
            values = fact.get("normalized") or fact.get("value")
            values = values if isinstance(values, dict) else {fact["field"]: values}
            for key, value in values.items():
                if key in profile["scores"] and float(value) != profile["scores"][key]:
                    return None
        except (TypeError, ValueError):
            return None
    return profile


def final_year_restricted(row, profile, source, course):
    body = text(source.get("_fullText") or source.get("text"))
    return bool(
        profile["level"] == "undergraduate"
        and re.search(r"top[- ]up|final[- ]year", row.course_name + " " + course.get("courseTitle", ""), re.I)
        and re.search(r"applicable to students entering Years? 1 and 2", body, re.I)
        and re.search(r"Final year.{0,100}(?:vary|depend)", body, re.I)
    )


def candidate(row, source, course):
    if source.get("_englishTablesSchema") != SCHEMA:
        return None
    choices = [p for p in source.get("_englishTables", [])
               if named_match(row, p, course, source.get("_englishTables", []))
               and not final_year_restricted(row, p, source, course)]
    if not choices:
        return None
    if len(choices) > 1:
        if any(p["scores"] != choices[0]["scores"] or
               p.get("bandConstraint") != choices[0].get("bandConstraint") for p in choices[1:]):
            return None
        choices.sort(key=lambda p: len(p["label"]), reverse=True)
    profile = choices[0]
    result = {"id": row.id, "matched": True,
              "applicability": {"tableId": profile["id"], "guidanceQuote": profile["quote"],
                               "courseQuote": course["courseTitle"], "page": None},
              "changes": [{"field": key, "newValue": profile["scores"][key],
                           "quote": profile["quote"], "page": None} for key in profile["scores"]]}
    return result if checked_profile(row, result, source, course) else None
