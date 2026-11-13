"""Evidence-bound standard IELTS clauses; no provider-specific score defaults."""
import hashlib
import json
import re

from app.services.document_course_scope import title_key, scope_matches

BANDS = ("ielts_overall", "ielts_listening", "ielts_reading", "ielts_writing", "ielts_speaking")
OVERALL = r"(?:overall(?:\s+score)?|IELTS(?:\s+Academic)?(?:\s*\([^)]{1,90}\))?(?:\s+overall(?:\s+score)?)?)\s*:?\s*([0-9](?:\.[05])?)(?!\d|\.\d)"
FLOOR = r"(?:no\s+(?:components?|bands?|skills?)\s+below)\s*:?\s*([0-9](?:\.[05])?)(?!\d|\.\d)"


def normal(value):
    return " ".join(str(value or "").split())


def identity_matches(name, quote):
    """Ignore formatting/award omission, not subject changes or different awards."""
    # Only a heading-sized quote can use semantic title equivalence.
    if len(normal(quote)) > 200:
        return bool(re.search(r"(?<!\w)"+re.escape(normal(name))+r"(?!\w)", normal(quote), re.I))
    left, right = title_key(name), title_key(quote)
    awards = {"bachelor", "master", "arts", "science", "honours"}
    if left == right:
        return True
    la, ra = set(left) & awards, set(right) & awards
    return bool((not la or not ra) and tuple(x for x in left if x not in awards)
                == tuple(x for x in right if x not in awards) and len(set(left)-awards) >= 2)


def scores(quote):
    """Require one explicit overall and one explicit common component floor."""
    overall = {float(v) for v in re.findall(OVERALL, quote, re.I)}
    floor = {float(v) for v in re.findall(FLOOR, quote, re.I)}
    if len(overall) != 1 or len(floor) != 1:
        return None
    values = [next(iter(overall)), next(iter(floor))]
    if not all(0 < v <= 9 for v in values) or values[1] > values[0]:
        return None
    return dict(zip(BANDS, [values[0], *([values[1]] * 4)]))


def corroboration(row, source, course, scope):
    """A qualified standard needs the actual course's explicit English scores."""
    from app.services.english_applicability import category
    if category(row) in {"health", "postgraduate_research", "unknown"}:
        return None
    if (course.get("provenance") == "existing-record-and-document"
            or not scope_matches(row, scope, category(row))):
        return None
    title = course.get("courseTitle", "")
    body = normal(course.get("_fullText") or course.get("text"))
    if not title or not identity_matches(row.course_name, title) or normal(title) not in body:
        return None
    expected = scores(scope)
    if not expected:
        return None
    # Keep all mentioned course-page IELTS clauses: a second conflicting score
    # anywhere in course-owned main content defeats the generic default.
    passages = [m.group() for m in re.finditer(
        r"\bIELTS\b.{0,300}", body, re.I)]
    found = [scores(p) for p in passages]
    if not found or any(v is None or v != expected for v in found):
        return None
    for fact in course.get("facts", []):
        if fact.get("field") in expected:
            try:
                value = fact.get("normalized") or fact.get("value")
                if isinstance(value, dict):
                    value = value.get(fact["field"])
                if float(value) != expected[fact["field"]]:
                    return None
            except (ValueError, TypeError):
                return None
    return {"title": normal(title), "quote": passages[0], "scores": expected}


def standard_candidate(row, source, course):
    """Build a proposal only from exact PDF text plus independent course proof."""
    from app.services.english_applicability import category
    if not source.get("verified") or source.get("unreadablePages") or not course.get("verified"):
        return None
    choices = []
    for page in source.get("_pages", []):
        for paragraph in re.split(r"\n\s*\n", page.get("text", "")):
            scope = normal(paragraph)
            if not scope_matches(row, scope, category(row)):
                continue
            proof = corroboration(row, source, course, scope)
            if proof:
                choices.append((scope, page["page"], proof))
    if len(choices) != 1:
        return None
    scope, page, proof = choices[0]
    return {"id": row.id, "matched": True,
            "applicability": {"guidanceQuote": scope, "page": page,
                             "courseQuote": proof["title"], "courseEnglishQuote": proof["quote"]},
            "changes": [{"field": field, "newValue": value, "quote": scope, "page": page}
                        for field, value in proof["scores"].items()]}


def response_rows(raw, expected_ids, *, source_type="pdf"):
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    parsed = json.loads(raw)
    rows = parsed.get("rows") if isinstance(parsed, dict) else None
    if not isinstance(rows, list):
        raise ValueError("rows_array_missing")
    valid, errors = {}, []
    for candidate in rows:
        if not isinstance(candidate, dict):
            errors.append("row_not_object")
            continue
        key = candidate.get("id")
        if type(key) is not int or key not in expected_ids:
            errors.append("unexpected_record_id")
            continue
        if key in valid:
            valid[key] = None
            errors.append("duplicate_record_id")
        elif type(candidate.get("matched")) is not bool:
            errors.append("matched_boolean_missing")
        elif not isinstance(candidate.get("changes"), list):
            errors.append("changes_array_missing")
        elif any(not isinstance(c, dict) for c in candidate["changes"]):
            errors.append("change_not_object")
        elif candidate["matched"] and not (
            isinstance(candidate.get("applicability"), dict)
            and all(isinstance(candidate["applicability"].get(k), str)
                    and candidate["applicability"][k].strip() for k in ("guidanceQuote", "courseQuote"))
            and (
                type(candidate["applicability"].get("page")) is int
                and candidate["applicability"]["page"] > 0
                if source_type == "pdf"
                else candidate["applicability"].get("page") is None
            )
        ):
            errors.append("applicability_structure_invalid")
        elif not candidate["matched"] and candidate["changes"]:
            errors.append("declined_match_has_changes")
        else:
            valid[key] = candidate
    return {k: v for k, v in valid.items() if v is not None}, errors


def diagnostic(raw, code, ids, attempt):
    """No raw model output, user prompt, credentials or reasoning is retained."""
    return {"code": code, "recordIds": list(ids), "attempt": attempt,
            "responseChars": len(raw), "responseSha256": hashlib.sha256(raw.encode()).hexdigest()}


async def match_batch(rows, invoke, diagnostics, *, source_type="pdf"):
    """Two attempts; preserve valid rows and retry only unresolved record IDs."""
    pending = {r.id: r for r in rows}
    retained = {}
    for attempt in (1, 2):
        if not pending:
            break
        ids = list(pending)
        raw = ""
        try:
            response = await invoke(list(pending.values()))
            raw = response["message"]["content"]
        except ValueError as exc:
            # Budget/accounting and provider exceptions must propagate.
            if str(exc) != "Model response did not complete; no partial tool calls will run":
                raise
            diagnostics.append(diagnostic(raw, "model_incomplete", ids, attempt))
            continue
        except (KeyError, TypeError, AttributeError):
            diagnostics.append(diagnostic("", "invalid_response_envelope", ids, attempt))
            continue
        try:
            valid, errors = response_rows(raw, set(ids), source_type=source_type)
        except (ValueError, TypeError):
            valid, errors = {}, ["invalid_json_or_rows"]
        retained.update(valid)
        pending = {key: row for key, row in pending.items() if key not in valid}
        for code in errors:
            diagnostics.append(diagnostic(raw, code, ids, attempt))
        if pending:
            diagnostics.append(diagnostic(raw, "unresolved_records", list(pending), attempt))
    return retained, set(pending)


def evidence_diagnostic(candidate, source):
    proof = candidate.get("applicability") or {}
    if not isinstance(proof, dict):
        proof = {}
    return {
        "requestedPage": proof.get("page") if type(proof.get("page")) is int else None,
        "availablePages": [p.get("page") for p in source.get("_pages", [])],
        **{key: {"chars": len(normal(proof.get(key))),
                 "sha256": hashlib.sha256(normal(proof.get(key)).encode()).hexdigest()}
           for key in ("guidanceQuote", "courseQuote")},
    }
