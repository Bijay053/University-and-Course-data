"""Validation, atomic persistence and lossless reads for intake-linked tuition."""
from copy import deepcopy
import math
import re
from urllib.parse import urlsplit

from sqlalchemy import select, text
from app.models import Course, Fee, FeeIntakeApplicability, Intake
from app.services.assistant_intake_fees import KEY, HOLD, intact, projection, publication_hold

MONTHS = "January February March April May June July August September October November December".split()
EDIT_HOLD = ("This course has intake-linked tuition schedules. Use a reviewed, schedule-aware "
             "pending-course approval to change its fees or intakes; a legacy edit would lose applicability.")


async def storage_ready(db):
    # Per-request caching, not process caching: a subsequent request must see a
    # separately provisioned migration. Missing storage never breaks old fees.
    cache = db.info
    if "fee_intake_storage_ready" not in cache:
        schema = (db.get_bind().get_execution_options().get("schema_translate_map") or {}).get(None)
        if schema:
            qualified = db.get_bind().dialect.identifier_preparer.quote(schema) + ".fee_intake_applicability"
            ready = await db.scalar(text("SELECT to_regclass(:qualified) IS NOT NULL"), {"qualified": qualified})
        else:
            ready = await db.scalar(text(
                "SELECT to_regclass(current_schema() || '.fee_intake_applicability') IS NOT NULL"))
        cache["fee_intake_storage_ready"] = bool(ready)
    return cache["fee_intake_storage_ready"]


def validate_manifest(row):
    metadata = row.extraction_method or {}
    manifest = metadata.get(KEY)
    projected = projection({
        "id": row.id,
        "extractionMethod": metadata, "courseName": row.course_name,
        "courseWebsite": row.course_website, "intakeMonths": row.intake_months,
        "internationalFee": row.international_fee, "feeYear": row.fee_year,
        "feeTerm": row.fee_term, "currency": row.currency,
    })
    if (not intact(manifest) or not projected or not projected["snapshotMatchesCurrent"]
            or row.status not in {"pending", "review", "review_ready", "approved"}
            or manifest.get("retainedRowId") != row.id):
        raise ValueError("The saved intake-fee snapshot no longer matches this course; refresh its review.")
    schedules = manifest["schedules"]
    seen, years = set(), set()
    for s in schedules:
        if (type(s.get("feeYear")) is not int or not 2000 <= s["feeYear"] <= 2100
                or type(s.get("entryYear")) is not int or not 2000 <= s["entryYear"] <= 2100
                or s["entryYear"] != manifest.get("entryYear") or s["feeYear"] in years
                or isinstance(s.get("amount"), bool) or not isinstance(s.get("amount"), (float, int))
                or not math.isfinite(s["amount"]) or s["amount"] <= 0
                or (s["amount"], s.get("currency"), s.get("basis")) != (
                    row.international_fee, row.currency, row.fee_term)
                or not isinstance(s.get("currency"), str) or not re.fullmatch(r"[A-Z]{3}", s["currency"])
                or not isinstance(s.get("basis"), str) or not s["basis"].strip()
                or not isinstance(s.get("intakes"), list) or not s["intakes"]
                or s.get("applicabilityNeedsReview") is not False
                or not isinstance(s.get("sourceUrls"), list) or not s["sourceUrls"]):
            raise ValueError("Incomplete or conflicting intake-specific tuition requires review.")
        years.add(s["feeYear"])
        for month in s["intakes"]:
            if month not in MONTHS or month in seen:
                raise ValueError("Intake applicability is missing, duplicated or conflicting.")
            seen.add(month)
        canonical = urlsplit(row.course_website)
        for url in s["sourceUrls"]:
            if not isinstance(url, str):
                raise ValueError("An intake tuition source URL is invalid.")
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or parsed.netloc != canonical.netloc
                    or parsed.username or parsed.password or parsed.query or parsed.fragment
                    or not (parsed.path.rstrip("/") == canonical.path.rstrip("/")
                            or parsed.path.startswith(canonical.path.rstrip("/") + "/"))):
                raise ValueError("Intake tuition requires course-owned official source URLs.")
    if seen != set(row.intake_months or []) or len(seen) != len(row.intake_months or []):
        raise ValueError("Published intakes must exactly match the retained fee schedules.")
    return deepcopy(manifest)


async def approval_manifest(db, row, *, confirmed=False):
    hold = await publication_hold(db, row)
    if not hold:
        return None
    if not confirmed or not (row.extraction_method or {}).get(KEY):
        raise ValueError(hold)
    manifest = validate_manifest(row)
    if not await storage_ready(db):
        raise ValueError("Intake-fee publishing storage is not installed. Apply the reviewed migration before approval.")
    return manifest


async def protect_course(db, course_id):
    """Serialize legacy edits with approval before checking for linked tuition."""
    await db.execute(select(Course.id).where(Course.id == course_id).with_for_update())
    if await storage_ready(db) and await db.scalar(select(FeeIntakeApplicability.fee_id).join(
            Fee, Fee.id == FeeIntakeApplicability.fee_id).where(Fee.course_id == course_id).limit(1)):
        raise ValueError(EDIT_HOLD)


async def persist(db, course_id, manifest):
    for s in manifest["schedules"]:
        fee = Fee(course_id=course_id, international_fee=s["amount"], fee_term=s["basis"],
                  fee_year=s["feeYear"], currency=s["currency"])
        db.add(fee)
        await db.flush()
        db.add(FeeIntakeApplicability(
            fee_id=fee.id, entry_year=s["entryYear"], months=s["intakes"],
            source_urls=s["sourceUrls"], source_digest=manifest["fingerprint"]))


async def schedules_by_course(db, course_ids):
    if not course_ids or not await storage_ready(db):
        return {}
    rows = (await db.execute(select(Fee, FeeIntakeApplicability).outerjoin(
        FeeIntakeApplicability, Fee.id == FeeIntakeApplicability.fee_id
    ).where(Fee.course_id.in_(course_ids)).order_by(Fee.fee_year, Fee.id))).all()
    grouped = {}
    for fee, link in rows:
        grouped.setdefault(fee.course_id, []).append((fee, link))
    mapped = [cid for cid, values in grouped.items() if any(link for _, link in values)]
    actual_intakes = {}
    if mapped:
        for cid, month, year in (await db.execute(select(
                Intake.course_id, Intake.intake_month, Intake.intake_year
        ).where(Intake.course_id.in_(mapped)))).all():
            actual_intakes.setdefault(cid, []).append((year, month))
    result = {}
    for cid, values in grouped.items():
        if not any(link for _, link in values):
            continue
        schedules, seen, invalid = [], set(), False
        prices = {(f.international_fee, f.currency, f.fee_term) for f, _ in values}
        # The initial consolidated-schedule contract supports equal prices
        # across years. Never let a legacy edit change search-budget semantics.
        invalid = len(prices) != 1
        for fee, link in values:
            if (not link or not isinstance(link.months, list) or not link.months
                    or any(m not in MONTHS or (link.entry_year, m) in seen for m in link.months)
                    or len(set(link.months)) != len(link.months)
                    or not isinstance(link.source_urls, list) or not link.source_urls
                    or any(not isinstance(u, str) or not u.startswith("https://") for u in link.source_urls)
                    or not fee.currency or not fee.fee_term or fee.fee_year is None
                    or fee.international_fee is None or not math.isfinite(fee.international_fee)
                    or fee.international_fee <= 0):
                invalid = True
                continue
            seen.update((link.entry_year, m) for m in link.months)
            schedules.append({
                "id": fee.id,
                "feeYear": fee.fee_year, "amount": fee.international_fee,
                "currency": fee.currency, "basis": fee.fee_term,
                "entryYear": link.entry_year, "intakes": link.months,
                "sourceUrls": link.source_urls,
            })
        actual = actual_intakes.get(cid, [])
        if set(actual) != seen or len(actual) != len(seen):
            invalid = True
        if len({link.source_digest for _, link in values if link}) != 1:
            invalid = True
        result[cid] = {"schedules": [] if invalid else schedules, "reviewRequired": invalid}
    return result


async def attach(db, rows, *, id_key="id", snake=False):
    lookup = await schedules_by_course(db, [r[id_key] for r in rows])
    for row in rows:
        value = lookup.get(row[id_key])
        if value is None:
            continue
        row["fee_schedules" if snake else "feeSchedules"] = value["schedules"]
        row["fee_schedule_review_required" if snake else "feeScheduleReviewRequired"] = value["reviewRequired"]
        # A scalar year would falsely imply every intake uses the latest year.
        for key in ("international_fee", "internationalFee", "international_fee_yearly",
                    "currency", "feeCurrency", "fee_term", "feeTerm", "fee_year", "feeYear"):
            if key in row:
                row[key] = None
