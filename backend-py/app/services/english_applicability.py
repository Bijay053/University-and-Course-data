"""Fail-closed evidence checks for central English guidance proposals."""
import re


def normalized(text):
    return " ".join(str(text or "").split())


def clauses(text):
    # Do not split decimal scores. Separate prose sentences and table rows.
    return [normalized(part) for part in re.split(r"\n+|(?<=[.;])\s+(?=[A-Z])", str(text or "")) if normalized(part)]


def score_binding(row, candidate, source):
    """Scope and a score must share a clause; mixed-score prose is not authority."""
    proof = candidate.get("applicability")
    if not isinstance(proof, dict):
        return False
    scope = normalized(proof.get("guidanceQuote"))
    name = normalized(row.course_name).casefold()
    named = name in scope.casefold()
    scoped_clauses = clauses(proof.get("guidanceQuote"))
    for change in candidate.get("changes", []):
        quote = normalized(change.get("quote"))
        if quote not in scope:
            return False
        if named and not any(name in clause.casefold() and quote in clause for clause in scoped_clauses):
            return False
        key, value = change.get("field"), change.get("newValue")
        if key == "ielts_overall":
            pattern = r"(?:overall(?:\s+score)?|IELTS(?:\s+Academic)?(?:\s+overall(?:\s+score)?)?)\s*:?\s*([0-9](?:\.[05])?)(?!\d|\.\d)"
        elif isinstance(key, str) and key.startswith("ielts_"):
            pattern = rf"(?:{re.escape(key[6:])}|no\s+(?:component|band)\s+below|minimum(?:\s+of)?)\s*:?\s*([0-9](?:\.[05])?)(?!\d|\.\d)"
        else:
            return False
        # Check all named clauses, not just the model's chosen excerpt.
        relevant = scope
        if named:
            relevant += " " + " ".join(c for c in clauses(source.get("_fullText") or source.get("text"))
                                      if name in c.casefold())
        else:
            # A second level/course score anywhere in central guidance means
            # the blanket default is not sufficient authority.
            relevant += " " + normalized(source.get("_fullText") or source.get("text"))
        values = {float(v) for v in re.findall(pattern, relevant, re.I)}
        if values != {value}:
            return False
    return bool(candidate.get("changes"))


def category(row):
    text = f"{row.course_name} {row.degree_level}"
    if re.search(r"\b(ph\.?d|doctor|doctorate|mres|mphil|research)\b", text, re.I):
        return "postgraduate_research"
    if re.search(r"health|nurs|midwif|physiotherap|radiograph|occupational therap|medicine|dentist|paramedic|pharmac|clinical|podiatr|optometr|dietet|medical", text, re.I):
        return "health"
    if re.search(r"undergraduate|bachelor", text, re.I):
        return "undergraduate"
    if re.search(r"postgraduate|master|graduate diploma|graduate certificate", text, re.I):
        return "postgraduate_taught"
    return "unknown"


def quote_page(source, quote, page):
    """Never accept a model's page number without locating the quote there."""
    quote = normalized(quote)
    if len(quote) < 15:
        return False
    if source.get("sourceType") == "pdf":
        return type(page) is int and page > 0 and any(
            p.get("page") == page and quote in normalized(p.get("text"))
            for p in source.get("_pages", [])
        )
    return page is None and quote in normalized(source.get("_fullText") or source.get("text"))


def applicability(row, candidate, source, course):
    group = category(row)
    result = {"category": group, "applicability": "unverified", "evidence": []}
    def reject(reason, state="unverified"):
        return {**result, "applicability": state, "reason": reason}
    if not source.get("verified") or not course.get("verified"):
        return reject("Official guidance and course page must both be accessible.")
    if source.get("provenance") == "staff-uploaded-document":
        return reject("Uploaded guidance is not independently authenticated as official.")
    if source.get("unreadablePages"):
        return reject("Unreadable guidance pages may contain exclusions.")
    proof = candidate.get("applicability") or {}
    if not isinstance(proof, dict):
        return reject("No structured applicability evidence supplied.")
    scope = normalized(proof.get("guidanceQuote"))
    identity = normalized(proof.get("courseQuote"))
    if not quote_page(source, scope, proof.get("page")) or not quote_page(course, identity, None):
        return reject("Applicability quotes or guidance page could not be verified.")
    # Reading a page is not identity proof; require the named course in its quote.
    name = normalized(row.course_name)
    if len(name) < 5 or name.casefold() not in identity.casefold():
        return reject("The course evidence does not identify this course.")
    named = name.casefold() in scope.casefold()
    if re.search(r"except|excluding|not apply|not applicable", scope, re.I):
        return reject("The applicability passage contains exclusions; course-specific confirmation is required.", "excluded")
    if group in {"health", "postgraduate_research"} and not named:
        return reject("Health and research courses require explicitly named course guidance.", "excluded")
    if not named:
        full_text = normalized(source.get("_fullText") or source.get("text"))
        # Any exception in the document defeats a blanket default, even for
        # subjects absent from our conservative display-category vocabulary.
        if re.search(r"except|excluding|not apply|not applicable|higher|vary|different|specific", full_text, re.I):
            return reject("The guidance contains exceptions; explicitly named course guidance is required.", "excluded")
        if name.casefold() in full_text.casefold():
            return reject("This course is mentioned separately in the guidance; check its own requirement.", "conflict")
        level = {"undergraduate": r"undergraduate",
                 "postgraduate_taught": r"(?:postgraduate taught|taught postgraduate)"}.get(group)
        if not level or not re.search(level, identity, re.I) or not re.search(
            rf"\b(?:all|every)\s+{level}\s+(?:courses|programmes|programs)\b", scope, re.I
        ):
            return reject("General guidance does not explicitly cover this course's verified level.")
        if re.search(r"except|excluding|higher|vary|different|specific|not apply", scope, re.I):
            return reject("Qualified general guidance requires course-specific confirmation.", "excluded")
    if not re.search(r"IELTS|English", scope, re.I):
        return reject("Applicability evidence does not concern English requirements.")
    result["evidence"] = [
        {"url": source.get("url"), "quote": scope, "page": proof.get("page")},
        {"url": course.get("url"), "quote": identity},
    ]
    return {**result, "applicability": "verified", "reason": "Explicit course applicability verified."}
