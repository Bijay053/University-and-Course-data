"""Exact-source, award-first partition of ULaw Legal Technology fees."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from sqlalchemy import select

from app.services.scraper.extractors.ulaw_fees import _cohort_dates, parse_course_fees, validated_fee_variants

URL = "https://www.law.ac.uk/study/postgraduate/law/pg-dip-and-pg-cert-legal-technology/"
QUALIFICATION_SCOPE = "ulaw_qualification_scope"
AWARDS = {
    "PG Dip": ("PG Dip Legal Technology", "Graduate Diploma", {"London": 13150, "Outside of London": 12200}),
    "PG Cert": ("PG Cert Legal Technology", "Graduate Certificate", {"London": 6600, "Outside of London": 6150}),
}
REVIEW_REASON = "Official Legal Technology qualification and applicable cohort evidence could not be verified; refresh before approval."
CAMPUSES = {"Bristol", "London Moorgate"}
COHORT_LOCATIONS = {"PG Dip": {"Bristol", "London Moorgate"}, "PG Cert": {"London Moorgate"}}
COHORT_STARTS = {
    "PG Dip": {"October 2026", "February 2027"},
    "PG Cert": {"October 2026"},
}


def _proven_intake_months(proof, award):
    """Read only dated, award-owned entries in the verified tuition cohort."""
    entries = proof.get("start_dates")
    if not isinstance(entries, list) or not entries:
        return None
    starts = set()
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        try:
            label = entry["intake"]
            date = datetime.strptime(label, "%B %Y").date()
            start = datetime.strptime(proof["cohort_start"], "%Y-%m-%d").date()
            end = datetime.strptime(proof["cohort_end"], "%Y-%m-%d").date()
        except (KeyError, TypeError, ValueError):
            return None
        if date.strftime("%B %Y") != label or not (
            start <= date <= end
        ):
            return None
        starts.add(label)
    if proof.get("fee_year") == 2026 and starts != COHORT_STARTS[award]:
        return None
    return [datetime.strptime(label, "%B %Y").strftime("%B")
            for label in sorted(starts, key=lambda label: datetime.strptime(label, "%B %Y"))]


def _award_start_proofs(soup, url, authority):
    """Only award-labelled starts inside the course's start-date accordion count."""
    accordion = soup.find(id="accordion-bcz")
    bounds = {_cohort_dates(o["snippet"]) for o in authority["selected"]}
    if not accordion or len(bounds) != 1 or None in bounds:
        return None
    start, end = next(iter(bounds))
    proofs = {}
    for award, (name, _, _) in AWARDS.items():
        entries = []
        for item in accordion.select(".accordion__item"):
            heading = item.select_one(".accordion__header h4")
            body = item.select_one(".accordion__body")
            if not heading or not body:
                return None
            try:
                intake = datetime.strptime(heading.get_text(" ", strip=True), "%B %Y").date()
            except ValueError:
                return None
            if not (start <= intake <= end):
                continue
            for li in body.select("ul > li"):
                strong = li.find("strong", recursive=False)
                if not strong or not re.fullmatch(
                    rf"{re.escape(name)}\s+\(Postgraduate {'Diploma' if award == 'PG Dip' else 'Certificate'}\)",
                    strong.get_text(" ", strip=True),
                ):
                    continue
                for line in li.select("ul > li"):
                    text = line.get_text(" ", strip=True)
                    match = re.fullmatch(r"(Full-time|Part-time): (.+)", text)
                    if not match:
                        return None
                    places = [part.strip() for part in match[2].split(" and ")]
                    if not places or any(place not in CAMPUSES for place in places):
                        return None
                    entries.append({"intake": heading.get_text(" ", strip=True),
                                    "study_load": match[1], "locations": places,
                                    "snippet": f"{strong.get_text(' ', strip=True)} | {text}"})
        locations = sorted({place for entry in entries for place in entry["locations"]})
        if not locations:
            return None
        proofs[award] = {
            "method": "location.ulaw_course_authority", "source_url": url,
            "course_name": name, "study_variant": award,
            "fee_year": authority["fee_year"], "fee_term": authority["fee_term"],
            "cohort_start": start.isoformat(), "cohort_end": end.isoformat(),
            "locations": locations, "start_dates": entries,
            "snippet": " | ".join(e["snippet"] for e in entries),
        }
    return proofs


def validate_qualification_scope(sc):
    """Check internal consistency only; promotion ALSO requires live verification."""
    from app.services.scraper.campus_fee_split import SCOPE, _matches, _locations

    metadata = sc.extraction_method or {}
    if not isinstance(metadata, dict):
        return False
    qualification = metadata.get(QUALIFICATION_SCOPE)
    award = qualification.get("award") if isinstance(qualification, dict) else None
    if (award not in AWARDS or (sc.course_website or "").rstrip("/") != URL.rstrip("/")
            or qualification.get("source_url") != URL
            or type(qualification.get("split_from_id")) is not int
            or sc.degree_level != AWARDS[award][1]):
        return False
    proof = metadata.get("campus_authority") or {}
    authority = validated_fee_variants(sc)
    if (not authority or not authority["selected"] or not isinstance(proof, dict)
            or not isinstance(proof.get("locations"), list)):
        return False
    selected = authority["selected"]
    source = qualification.get("verified_source")
    if source is None:
        # Legacy 2026 scopes retain only their original pinned contract; this
        # compatibility path can never authorize a different cohort.
        captured = ((qualification.get("pre_split") or {}).get("extraction_method") or {}).get("fee_variants")
        if (authority["fee_year"] != 2026 or not _joint_cohort(captured)
                or captured["fee_year"] != 2026
                or set(proof.get("locations", [])) != COHORT_LOCATIONS[award]):
            return False
    else:
        if not isinstance(source, dict):
            return False
        captured = source.get("authority")
        source_proofs = source.get("proofs")
        if (not _joint_cohort(captured) or not isinstance(source_proofs, dict)
                or source_proofs.get(award) != proof
                or source.get("source_url") != URL
                or not re.fullmatch(r"[0-9a-f]{64}", str(source.get("response_sha256", "")))
                or source.get("contract_sha256") != _contract_hash(captured, source_proofs)):
            return False
        try:
            captured_at = datetime.fromisoformat(source["captured_at"])
            if captured_at.tzinfo is None or captured_at > datetime.now(timezone.utc):
                return False
        except (KeyError, TypeError, ValueError):
            return False
    expected = [o for o in captured["selected"] if o["study_variant"] == award]
    bounds = _cohort_dates(expected[0]["snippet"])
    from app.services.scraper.extractors import ulaw_fees
    if ulaw_fees.date.today() > bounds[1]:
        return False
    if any(o not in expected for o in selected):
        return False
    if (proof.get("method") != "location.ulaw_course_authority"
            or proof.get("source_url") != URL or proof.get("study_variant") != award
            or proof.get("course_name") != AWARDS[award][0]
            or proof.get("fee_year") != captured["fee_year"] or proof.get("fee_term") != "Full Course"
            or proof.get("cohort_start") != bounds[0].isoformat() or proof.get("cohort_end") != bounds[1].isoformat()
            or authority["fee_year"] != captured["fee_year"] or authority["fee_term"] != "Full Course"
            or any(o.get("study_variant") != award
                   for o in selected)):
        return False
    entries = proof.get("start_dates")
    months = _proven_intake_months(proof, award)
    if (months is None or type(sc.intake_months) is not list
            or sc.intake_months != months or not proof.get("snippet")):
        return False
    proven = set()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("study_load") not in {"Full-time", "Part-time"}:
            return False
        try:
            intake = datetime.strptime(entry["intake"], "%B %Y").date()
        except (KeyError, TypeError, ValueError):
            return False
        if not (bounds[0] <= intake <= bounds[1]):
            return False
        places = entry.get("locations")
        if (not isinstance(places, list) or not places or any(p not in CAMPUSES for p in places)
                or not isinstance(entry.get("snippet"), str)
                or entry.get("snippet") !=
                f"{AWARDS[award][0]} (Postgraduate {'Diploma' if award == 'PG Dip' else 'Certificate'}) | "
                f"{entry['study_load']}: {' and '.join(places)}"):
            return False
        proven.update(places)
    if proven != set(proof["locations"]) or proof["snippet"] != " | ".join(e["snippet"] for e in entries):
        return False
    scope = metadata.get(SCOPE)
    locations = set(_locations(sc.course_location))
    if scope:
        if (scope.get("original_name") != AWARDS[award][0]
                or set(_locations(scope.get("original_locations"))) != proven
                or locations != set(scope.get("locations", [])) or not locations <= proven
                or sc.course_name != f"{AWARDS[award][0]} — {sc.course_location}"
                or len(selected) != 1
                or any(not _matches(o["campus"], location) for o in selected for location in locations)
                or any(o["amount"] != sc.international_fee for o in selected)):
            return False
    elif (sc.course_name != AWARDS[award][0] or locations != proven
          or sc.international_fee is not None
          or selected != expected):
        return False
    return True


def _signature(options):
    try:
        return sorted((o["study_variant"], o["campus"], o["amount"], o["currency"],
                       o["year"], o["period"]) for o in options)
    except (KeyError, TypeError, ValueError):
        return []


EXPECTED = sorted((award, campus, amount, "GBP", 2026, "Full Course")
                  for award, (_, _, prices) in AWARDS.items() for campus, amount in prices.items())


def _contract_hash(authority, proofs):
    return sha256(json.dumps({"authority": authority, "proofs": proofs},
                             sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _joint_cohort(authority):
    """One complete award-by-region matrix with a single explicit tuition window."""
    if not isinstance(authority, dict):
        return False
    selected = authority.get("selected")
    if not isinstance(selected, list) or len(selected) != 4:
        return False
    try:
        bounds = {_cohort_dates(o["snippet"]) for o in selected}
        year = authority["fee_year"]
        if (len(bounds) != 1 or None in bounds or type(year) is not int
                or authority["fee_term"] != "Full Course"
                or authority["currency"] != "GBP"
                or {(o["study_variant"], o["campus"]) for o in selected}
                != {(a, c) for a in AWARDS for c in ("London", "Outside of London")}):
            return False
        start, end = next(iter(bounds))
        if start.year != year or end.year != year + 1 or not start < end:
            return False
        if any(o["source_url"] != URL or o["year"] != year or o["period"] != "Full Course"
               or o["currency"] != "GBP" or not 0 < o["amount"] < 100000
               or o not in authority["options"] for o in selected):
            return False
        if any(len({o["amount"] for o in selected if o["campus"] == c}) != 2
               for c in ("London", "Outside of London")):
            return False
        return year != 2026 or _signature(selected) == EXPECTED
    except (KeyError, TypeError, ValueError):
        return False


def _verified_page(html, url):
    if url.rstrip("/") != URL.rstrip("/"):
        return None
    soup = BeautifulSoup(html, "html.parser")
    canonical = soup.find("link", rel="canonical")
    if canonical and urljoin(url, canonical.get("href", "")).rstrip("/") != URL.rstrip("/"):
        return None
    heading = soup.find("h1")
    award_heading = soup.find(["h3", "h4"], string=lambda text: bool(text and
                                  " ".join(text.split()).casefold() == "pg dip and pg cert"))
    intro = soup.find(id="course-details-header-intro")
    if (not heading or heading.get_text(" ", strip=True) != "Legal Technology"
            or not award_heading or not intro
            or "Postgraduate Diploma and Postgraduate Certificate in Legal Technology"
            not in intro.get_text(" ", strip=True)):
        return None
    fresh = parse_course_fees(html, url)
    if not _joint_cohort(fresh):
        return None
    proofs = _award_start_proofs(soup, url, fresh)
    if not proofs or any((fresh["fee_year"] == 2026 and set(proofs[a]["locations"]) != COHORT_LOCATIONS[a])
                         or _proven_intake_months(proofs[a], a) is None for a in AWARDS):
        return None
    locations = soup.select(".key-facts .key-facts__locations a[href]")
    if {(a.get_text(" ", strip=True), urljoin(url, a["href"]).rstrip("/"))
        for a in locations if "/locations/" in urljoin(url, a["href"])} != {
            ("Bristol", "https://www.law.ac.uk/locations/bristol"),
            ("London Moorgate", "https://www.law.ac.uk/locations/london/moorgate"),
            ("Online", "https://www.law.ac.uk/locations/online"),
        }:
        return None
    return fresh, proofs


async def verify_qualification_source(sc):
    """Re-establish external authority before any pending award is promoted.

    Persisted hashes are audit checksums, not signatures. Neither they nor a
    mutually consistent pair of editable proof/fee records authenticates a fee.
    Re-fetch the exact official route, reparse it, and compare the selected
    joint cohort and award-owned intakes without trusting stored response hashes.
    """
    if not validate_qualification_scope(sc):
        return False
    import httpx

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            async with client.stream("GET", URL) as response:
                response.raise_for_status()
                if str(response.url).rstrip("/") != URL.rstrip("/"):
                    return False
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 2_000_000:
                        return False
                    chunks.append(chunk)
        verified = _verified_page(b"".join(chunks).decode("utf-8"), URL)
    except (httpx.HTTPError, UnicodeError):
        return False
    if not verified:
        return False
    authority, proofs = verified
    metadata = sc.extraction_method
    qualification = metadata[QUALIFICATION_SCOPE]
    source = qualification.get("verified_source")
    captured = (source["authority"] if source else
                qualification["pre_split"]["extraction_method"]["fee_variants"])
    # Compare source-owned structured evidence, not the whole HTTP response:
    # analytics/nonces can change between two otherwise identical live pages.
    if (authority["selected"] != captured["selected"]
            or metadata["campus_authority"] != proofs[qualification["award"]]):
        return False
    return source is None or source["proofs"] == proofs


def is_qualification_candidate(row):
    if (getattr(row, "course_website", None) or "").rstrip("/") != URL.rstrip("/"):
        return False
    if (row.extraction_method or {}).get(QUALIFICATION_SCOPE):
        return False
    authority = (row.extraction_method or {}).get("fee_variants") or {}
    return {o.get("study_variant") for o in authority.get("selected", []) if isinstance(o, dict)} == set(AWARDS)


async def split_pending_qualifications(db, row, *, actor="scraper"):
    """Caller owns row lock/transaction. Return unchanged for unrelated awards."""
    from app.models import ScrapedCourse, ScrapedFieldEvidence, FieldConflict

    unchanged = {"id": row.id, "status": "unchanged", "courseIds": [row.id]}
    if not is_qualification_candidate(row):
        return unchanged
    def reject():
        return {"id": row.id, "status": "needs_review", "courseIds": [row.id], "reason": REVIEW_REASON}
    if row.status not in {"pending", "review_ready"} or row.course_name not in {
        "Legal Technology", "PG Dip and PG Cert Legal Technology",
    }:
        return reject()
    stored = validated_fee_variants(row)
    if not _joint_cohort(stored):
        return reject()
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            async with client.stream("GET", row.course_website) as response:
                response.raise_for_status()
                if str(response.url).rstrip("/") != URL.rstrip("/"):
                    return reject()
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 2_000_000:
                        return reject()
                    chunks.append(chunk)
        verified = _verified_page(b"".join(chunks).decode("utf-8", errors="replace"), row.course_website)
    except (httpx.HTTPError, UnicodeError):
        return reject()
    if not verified or _signature(verified[0]["selected"]) != _signature(stored["selected"]):
        return reject()
    fresh, proofs = verified
    if {_cohort_dates(o["snippet"]) for o in fresh["selected"]} != {
        _cohort_dates(o["snippet"]) for o in stored["selected"]
    }:
        return reject()
    verified_source = {
        "source_url": URL, "captured_at": datetime.now(timezone.utc).isoformat(),
        "response_sha256": sha256(b"".join(chunks)).hexdigest(),
        "authority": deepcopy(fresh), "proofs": deepcopy(proofs),
        "contract_sha256": _contract_hash(fresh, proofs),
    }
    evidence = (await db.execute(select(ScrapedFieldEvidence).where(
        ScrapedFieldEvidence.scraped_course_id == row.id))).scalars().all()
    conflicts = (await db.execute(select(FieldConflict).where(
        FieldConflict.scraped_course_id == row.id))).scalars().all()
    values = {c.name: deepcopy(getattr(row, c.name)) for c in ScrapedCourse.__table__.columns
              if c.name not in {"id", "created_at", "canonical_course_url"}}
    snapshot = {key: deepcopy(values.get(key)) for key in (
        "course_name", "degree_level", "course_location", "international_fee",
        "currency", "fee_year", "fee_term", "fee_scope_key", "extraction_method",
    )}
    when = datetime.now(timezone.utc).isoformat()
    children = [row]
    for award in ("PG Dip", "PG Cert"):
        if award == "PG Cert":
            child = ScrapedCourse(**deepcopy(values))
            db.add(child)
            children.append(child)
        else:
            child = row
        name, level, _ = AWARDS[award]
        subset = [o for o in fresh["selected"] if o["study_variant"] == award]
        authority = {**deepcopy(fresh), "selected": deepcopy(subset),
                     "status": "range", "international_fee": None}
        key = sha256(f"ulaw-legal-technology|{award}".encode()).hexdigest()[:20]
        child.course_name = name
        child.degree_level = level
        child.course_location = ", ".join(proofs[award]["locations"])
        child.intake_months = _proven_intake_months(proofs[award], award)
        child.international_fee = None
        child.currency = "GBP"
        child.fee_year = fresh["fee_year"]
        child.fee_term = "Full Course"
        child.fee_scope_key = key
        child.extraction_method = {
            **deepcopy(values["extraction_method"]), "fee_variants": authority,
            "campus_authority": deepcopy(proofs[award]),
            QUALIFICATION_SCOPE: {
                "award": award, "source_url": URL, "split_from_id": row.id,
                "split_actor": actor, "split_at": when, "pre_split": snapshot,
                "verified_source": deepcopy(verified_source),
            },
        }
        child.extraction_method.pop("fee_selection", None)
        child.course_id = None
        child.status = "pending"
        child.reviewed_at = None
        child.pub_decision = None
        child.pub_decision_reason = None
        child.pub_score = None
        child.pub_score_breakdown = None
    await db.flush()
    child = children[1]
    copied = {}
    for item in evidence:
        data = {c.name: deepcopy(getattr(item, c.name)) for c in ScrapedFieldEvidence.__table__.columns
                if c.name not in {"id", "created_at", "scraped_course_id"}}
        ev = ScrapedFieldEvidence(scraped_course_id=child.id, **data)
        db.add(ev)
        copied[item.id] = ev
    await db.flush()
    for conflict in conflicts:
        data = {c.name: deepcopy(getattr(conflict, c.name)) for c in FieldConflict.__table__.columns
                if c.name not in {"id", "created_at", "scraped_course_id", "course_id", "evidence_a_id", "evidence_b_id"}}
        db.add(FieldConflict(
            scraped_course_id=child.id, **data,
            evidence_a_id=getattr(copied.get(conflict.evidence_a_id), "id", None),
            evidence_b_id=getattr(copied.get(conflict.evidence_b_id), "id", None),
        ))
    return {"id": row.id, "status": "split", "courseIds": [c.id for c in children]}