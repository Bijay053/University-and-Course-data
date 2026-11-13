"""Fail-closed evidence checks for central English guidance proposals."""
import re
from app.services.document_course_scope import (
    named_clause, declared_scope, scope_matches, exception_blocks,
)


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
    if proof.get("tableId"):
        from app.services.english_html_tables import profile_for
        return profile_for(candidate, source) is not None
    scope = normalized(proof.get("guidanceQuote"))
    named = any(named_clause(row, c) for c in clauses(proof.get("guidanceQuote")))
    scoped_clauses = clauses(proof.get("guidanceQuote"))
    for change in candidate.get("changes", []):
        quote = normalized(change.get("quote"))
        if quote not in scope:
            return False
        if named and not any(named_clause(row, clause) and quote in clause for clause in scoped_clauses):
            return False
        key, value = change.get("field"), change.get("newValue")
        if key == "ielts_overall":
            from app.services.document_ielts_matching import OVERALL
            pattern = OVERALL
        elif isinstance(key, str) and key.startswith("ielts_"):
            pattern = rf"(?:{re.escape(key[6:])}(?:\s+(?:score|minimum))?|no\s+(?:components?|bands?|skills?)\s+below)\s*:?\s*([0-9](?:\.[05])?)(?!\d|\.\d)"
        else:
            return False
        # Check all named clauses, not just the model's chosen excerpt.
        relevant = scope
        if named:
            relevant += " " + " ".join(c for c in clauses(source.get("_fullText") or source.get("text"))
                                      if named_clause(row, c))
        else:
            # A second level/course score anywhere in central guidance means
            # the blanket default is not sufficient authority.
            relevant += " " + " ".join(
                c for c in clauses(source.get("_fullText") or source.get("text"))
                if not declared_scope(c) or scope_matches(row, c, category(row))
            )
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


def uploaded_course_evidence(row, source):
    """Readable PDFs can establish applicability without requiring a live page.

    Stored course metadata is explicitly labelled; it is not a website quote.
    No institution-specific scores or inferred level facts are supplied.
    """
    if (not source.get("verified") or source.get("sourceType") != "pdf"
            or source.get("unreadablePages")):
        return None
    name = normalized(row.course_name)
    if len(name) < 5:
        return None
    passages = [clause for page in source.get("_pages", [])
                for clause in clauses(page.get("text"))
                if (named_clause(row, clause) or (scope_matches(row, clause, category(row))
                    and declared_scope(clause) != "undergraduate and postgraduate taught"))
                and re.search(r"\bIELTS\b", clause, re.I)]
    if not passages:
        return None
    metadata = (
        f"Existing course record: {name}. Stored qualification level: {getattr(row, 'degree_level', None)}. "
        f"Stored subject: {getattr(row, 'category', None)}; {getattr(row, 'sub_category', None)}."
    )
    return {"verified": True, "url": source.get("url"), "text": metadata + "\n" + "\n".join(passages),
            "provenance": "existing-record-and-document", "officialProvenanceVerified": False,
            "_recordIdentity": metadata, "facts": []}


def applicability(row, candidate, source, course):
    group = category(row)
    result = {"category": group, "applicability": "unverified", "evidence": []}
    def reject(reason, state="unverified", code=None):
        return {**result, "applicability": state, "reason": reason,
                **({"diagnosticCode": code} if code else {})}
    if not source.get("verified") or not course.get("verified"):
        return reject("Readable guidance and sufficient existing-course evidence are required.")
    if source.get("unreadablePages"):
        return reject("Unreadable guidance pages may contain exclusions.")
    proof = candidate.get("applicability") or {}
    if not isinstance(proof, dict):
        return reject("No structured applicability evidence supplied.")
    if proof.get("tableId"):
        from app.services.english_html_tables import checked_profile
        profile = checked_profile(row, candidate, source, course)
        if not profile:
            return reject("The HTML table does not explicitly identify this programme and qualification.",
                          code="html_table_scope_mismatch")
        return {**result, "applicability": "verified",
                "reason": "Explicitly named programme matched to the official IELTS table columns.",
                "evidence": [{"url": source["url"], "quote": profile[k], "page": None}
                             for k in ("scope", "headerQuote", "quote")]
                            + [{"url": course["url"], "quote": proof["courseQuote"]}]}
    scope = normalized(proof.get("guidanceQuote"))
    identity = normalized(proof.get("courseQuote"))
    if not scope:
        return reject("The model supplied no applicability passage.", code="guidance_quote_missing")
    if not quote_page(source, scope, proof.get("page")):
        return reject(
            "The guidance quote was not found on the cited PDF page."
            if source.get("sourceType") == "pdf" else
            "The guidance quote was not found in the official HTML source.",
            code="guidance_page_quote_mismatch" if source.get("sourceType") == "pdf"
                 else "guidance_html_quote_mismatch",
        )
    if not quote_page(course, identity, None):
        return reject("The course identity quote was not found in its source.", code="course_quote_mismatch")
    # Reading a page is not identity proof; require the named course in its quote.
    name = normalized(row.course_name)
    from app.services.document_ielts_matching import identity_matches, corroboration
    if len(name) < 5 or not (name.casefold() in identity.casefold() or identity_matches(name, identity)):
        return reject("The course evidence does not identify this course.")
    confirmed_course = corroboration(row, source, course, scope)
    named = any(named_clause(row, c) for c in clauses(proof.get("guidanceQuote")))
    if exception_blocks(row, scope, group) and not confirmed_course:
        return reject("The applicability passage contains exclusions; course-specific confirmation is required.", "excluded")
    if group in {"health", "postgraduate_research"} and not named:
        return reject("Health and research courses require explicitly named course guidance.", "excluded")
    if not named:
        # Any exception in the document defeats a blanket default, even for
        # subjects absent from our conservative display-category vocabulary.
        if not confirmed_course and any(exception_blocks(row, c, group) for c in clauses(source.get("_fullText") or source.get("text"))):
            return reject("The guidance contains exceptions; explicitly named course guidance is required.", "excluded")
        if any(named_clause(row, c) for c in clauses(source.get("_fullText") or source.get("text"))):
            return reject("This course is mentioned separately in the guidance; check its own requirement.", "conflict")
        level = {"undergraduate": r"undergraduate",
                 "postgraduate_taught": r"(?:postgraduate taught|taught postgraduate)"}.get(group)
        stored_identity = course.get("provenance") == "existing-record-and-document"
        stored_match = stored_identity and scope_matches(row, scope, group)
        live_match = (not stored_identity and level and re.search(level, identity, re.I)
                      and re.search(rf"\b(?:all|every)\s+{level}\s+(?:courses|programmes|programs)\b", scope, re.I))
        if not stored_match and not live_match and not confirmed_course:
            return reject("General guidance does not explicitly cover this course's verified level.")
        if exception_blocks(row, scope, group) and not confirmed_course:
            return reject("Qualified general guidance requires course-specific confirmation.", "excluded")
    if not re.search(r"IELTS|English", scope, re.I):
        return reject("Applicability evidence does not concern English requirements.")
    result["evidence"] = [
        {"url": source.get("url"), "quote": scope, "page": proof.get("page")},
        {"url": course.get("url"), "quote": identity,
         "provenance": course.get("provenance", "official-page")},
    ]
    if confirmed_course:
        result["evidence"].append({"url": course.get("url"), "quote": confirmed_course["quote"],
                                   "provenance": "official-course-english"})
    return {**result, "applicability": "verified", "reason": (
        "Document scope matched to course evidence; staff-uploaded provenance is not independently verified."
        if source.get("provenance") == "staff-uploaded-document"
        else "Explicit course applicability verified."
    )}
