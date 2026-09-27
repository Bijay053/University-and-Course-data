"""One-off, fail-closed ULaw singleton adoption. Never invokes approval or merges IDs."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

UNIVERSITY = 92
JOB = "job_435ad2d18e35"
REVISION = "ulaw-reviewed23-singleton-adoption-v1"
PAIRS = (
    (42727, 9723), (42728, 9722), (42815, 9703), (42821, 9701),
    (42822, 9700), (42823, 9699), (42824, 9698), (42919, 9680),
    (42930, 9679), (42931, 9678), (42932, 9677), (42936, 9674),
    (42937, 9673), (42940, 9672), (42941, 9671), (42942, 9670),
    (42943, 9669), (42944, 9668), (42957, 9664), (42958, 9663),
    (42959, 9662), (42960, 9661), (42961, 9660),
)
CONFIRM = "ADOPT-EXACT-REVIEWED23-PRESERVE-ALL-IDS"


class Refused(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise Refused(message)


def digest(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()).hexdigest()


def file_hash():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def fee_tuple(row):
    return tuple(row.get(k) for k in (
        "international_fee", "currency", "fee_term", "fee_year",
    ))


def validate_campus_evidence(row, locations):
    """Local strict adoption gate; never infer physical campuses from a price."""
    from app.services.scraper.campus_fee_split import SCOPE, _matches
    from app.services.scraper.extractors.ulaw_fees import validated_fee_variants
    from app.services.scraper.published_offerings import offering_identity, validate_offering_cohort

    authority = validated_fee_variants(row)
    require(authority and authority["status"] == "uniform",
            f"Missing validated campus fee evidence: {row['id']}")
    require(row["course_location"] == ", ".join(locations),
            f"Campus tuple mismatch: {row['id']}")
    scope = (row.get("extraction_method") or {}).get(SCOPE)
    if scope:
        require(scope.get("locations") == locations
                and scope.get("source_url") == row["course_website"],
                f"Historical/incoming scope mismatch: {row['id']}")
        obj = SimpleNamespace(**row)
        validate_offering_cohort([obj], offering_identity(obj, scope))
    # Only these existing vetted regional labels may represent a physical
    # campus without an exact label match. Do not broaden _matches implicitly.
    regional = {
        "outside london", "outside of london", "non-london", "non london",
        "london", "all locations", "all campuses", "london and outside london",
        "birmingham/manchester",
    }

    def matches(label, campus):
        return label == campus or (
            " ".join(label.casefold().split()) in regional and _matches(label, campus)
        )

    selected = authority["selected"]
    require(all(option["source_url"] == row["course_website"] for option in selected),
            f"Nonexact historical/incoming source URL: {row['id']}")
    require(all(isinstance(option["campus"], str)
                and any(matches(option["campus"], campus) for campus in locations)
                for option in selected)
            and all(any(matches(option["campus"], campus) for option in selected)
                    for campus in locations),
            f"Selected fee campus does not attest scoped campus: {row['id']}")
    return authority


def plan(snapshot):
    from app.services.scraper.campus_fee_split import SCOPE
    from app.services.scraper.extractors.ulaw_fees import validated_fee_variants
    from app.services.scraper.published_offerings import (
        canonical_degree_level, offering_identity, validate_offering_cohort,
    )

    courses = {r["id"]: r for r in snapshot["courses"]}
    staged = {r["id"]: r for r in snapshot["scraped_courses"]}
    plans = []
    for sid, cid in PAIRS:
        require(sid in staged and cid in courses, f"Missing reviewed pair {sid}:{cid}")
        row, course = staged[sid], courses[cid]
        require(row["scrape_job_id"] == JOB and row["university_id"] == UNIVERSITY,
                f"Wrong staged job/university: {sid}")
        require(row["status"] not in ("approved", "published", "rejected")
                and row.get("course_id") is None, f"Staged state changed: {sid}")
        require(course["university_id"] == UNIVERSITY
                and not course.get("offering_identity"), f"Not an unadopted singleton: {cid}")
        require(not any(cid in (a["alias_course_id"], a["canonical_course_id"])
                        for a in snapshot["course_id_aliases"]), f"Alias involvement: {cid}")
        require(not any(o["course_id"] == cid for o in snapshot["course_offerings"]),
                f"Existing offerings: {cid}")
        scope = (row.get("extraction_method") or {}).get(SCOPE)
        require(bool(scope), f"Missing verified scope: {sid}")
        obj = SimpleNamespace(**row)
        identity = offering_identity(obj, scope)
        validate_offering_cohort([obj], identity)
        require(len(scope["locations"]) == 1, f"Not singleton campus: {sid}")
        validate_campus_evidence(row, scope["locations"])
        require(course["name"] in (row["course_name"], scope["original_name"]),
                f"Exact award name mismatch: {cid}")
        require(course["course_website"] == row["course_website"]
                and course["course_location"] == row["course_location"]
                and course["study_mode"] == row["study_mode"] == "On Campus"
                and canonical_degree_level(course["degree_level"])
                == canonical_degree_level(row["degree_level"]), f"Course tuple mismatch: {cid}")
        fees = [f for f in snapshot["fees"] if f["course_id"] == cid]
        require(len(fees) == 1 and fee_tuple(fees[0]) == fee_tuple(row)
                and all(v is not None for v in fee_tuple(row)), f"Scalar fee mismatch: {cid}")
        # A route/name is not variant proof: require historically published,
        # linked, validated source evidence uniquely attesting this exact tuple.
        variants = set()
        witnesses = []
        for historical in snapshot["scraped_courses"]:
            if historical.get("course_id") != cid:
                continue
            require(historical["status"] in ("approved", "published"),
                    f"Unexpected linked staged state: {historical['id']}")
            require(historical["course_website"] == row["course_website"]
                    and historical["course_location"] == row["course_location"]
                    and historical["study_mode"] == row["study_mode"]
                    and historical["course_name"] in (row["course_name"], scope["original_name"])
                    and canonical_degree_level(historical["degree_level"])
                    == canonical_degree_level(row["degree_level"])
                    and fee_tuple(historical) == fee_tuple(row),
                    f"Contradictory historical tuple: {historical['id']}")
            authority = validate_campus_evidence(historical, scope["locations"])
            variants.update(v["study_variant"] for v in authority["selected"])
            witnesses.append(historical["id"])
        selected = row["extraction_method"]["fee_variants"]["selected"]
        require(witnesses and variants == {v["study_variant"] for v in selected}
                and len(variants) == 1,
                f"Historical variant not uniquely proven: {cid}")
        require(all(v["source_url"] == row["course_website"] for v in selected),
                f"Nonexact selected source URL: {sid}")
        for other in snapshot["scraped_courses"]:
            if other["id"] == sid or other.get("course_id") == cid:
                continue
            other_scope = (other.get("extraction_method") or {}).get(SCOPE)
            same_route = other["course_website"] == row["course_website"]
            other_name = (other.get("course_name") or "").casefold()
            award_name = scope["original_name"].casefold()
            if same_route and not other_scope:
                require(not (other_name == award_name
                             or other_name.startswith(award_name + " — ")),
                        f"Unscoped same-award evidence requires review: {other['id']}")
            if other_scope and other["course_website"] == row["course_website"]:
                other_obj = SimpleNamespace(**other)
                authority = validated_fee_variants(other_obj)
                require(authority is not None, f"Unvalidated same-route evidence: {other['id']}")
                require(offering_identity(other_obj, other_scope) != identity,
                        f"Additional staged identity collision: {other['id']}")
        for other in snapshot["courses"]:
            if other["id"] == cid:
                continue
            name = other["name"].casefold()
            award = scope["original_name"].casefold()
            require(other.get("offering_identity") != identity
                    and not (other["course_website"] == row["course_website"]
                             and (name == award or name.startswith(award + " — "))),
                    f"Published collision: {cid}/{other['id']}")
        plans.append((row, course, scope, identity, witnesses))
    require(len({p[3] for p in plans}) == 23, "Reviewed identities collide")
    return plans


async def snapshot(db, *, lock=False):
    # Exhaustive university inventory, not an arbitrary LIMIT. Global aliases
    # include both directions; full-row hashes pin evidence and unrelated state.
    predicates = {
        "courses": "university_id = 92",
        "scraped_courses": "university_id = 92",
        "fees": "course_id IN (SELECT id FROM courses WHERE university_id = 92)",
        "course_offerings": "course_id IN (SELECT id FROM courses WHERE university_id = 92)",
        "course_id_aliases": "TRUE",
    }
    result = {}
    for table, predicate in predicates.items():
        rows = (await db.execute(text(
            f"SELECT * FROM {table} WHERE {predicate}" + (" FOR UPDATE" if lock else "")
        ))).mappings().all()
        result[table] = sorted((dict(r) for r in rows), key=lambda r: json.dumps(r, sort_keys=True, default=str))
    return result


async def archive(db, audit_id, sha, actor, event, payload):
    await db.execute(text("""
        INSERT INTO course_identity_reconciliation_audit
        (audit_id, manifest_sha256, approval_revision, actor, event_type,
         entity_table, entity_key, row_payload)
        VALUES (:id, :sha, :revision, :actor, :event, 'reviewed23_snapshot',
                CAST(:key AS jsonb), CAST(:payload AS jsonb))
    """), dict(id=audit_id, sha=sha, revision=REVISION, actor=actor, event=event,
               key=json.dumps({"university_id": UNIVERSITY, "pairs": PAIRS}),
               payload=json.dumps(payload, default=str)))


async def execute(db, *, apply=False, expected_file=None, expected_snapshot=None,
                  confirm=None, actor=None):
    """Caller owns a FRESH SERIALIZABLE transaction with no prior queries.

    Does not commit. Apply takes a table writer barrier BEFORE the first
    snapshot-acquiring SELECT, so writers that ignore advisory locks cannot
    introduce phantom identities during verification. Dry-run is READ ONLY.
    """
    from app.services.scraper.replay_extraction import review_restore_lock_scope
    from app.services.scraper.published_offerings import persist_offerings
    sha = file_hash()
    if apply:
        require(expected_file == sha and bool(expected_snapshot)
                and confirm == CONFIRM and bool(actor and actor.strip()),
                "Apply requires exact file/snapshot hashes, confirmation and actor")
    else:
        await db.execute(text("SET TRANSACTION READ ONLY"))
    await db.execute(text("SET LOCAL lock_timeout = '5s'"))
    await db.execute(text("SET LOCAL statement_timeout = '60s'"))
    require((await db.execute(text("SHOW transaction_isolation"))).scalar() == "serializable",
            "SERIALIZABLE transaction required")
    if apply:
        # LOCK and SHOW are utility commands: neither establishes the MVCC
        # snapshot. Do not move any SELECT (including advisory locking) above
        # this barrier. SHARE ROW EXCLUSIVE conflicts with every table writer.
        await db.execute(text("""
            LOCK TABLE courses, scraped_courses, fees, course_offerings,
                course_id_aliases, course_identity_reconciliation_audit
            IN SHARE ROW EXCLUSIVE MODE
        """))
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:scope))"),
                         {"scope": review_restore_lock_scope(UNIVERSITY)})
    before = await snapshot(db, lock=apply)
    pre = digest(before)
    runs = (await db.execute(text("""
        SELECT row_payload, manifest_sha256 FROM course_identity_reconciliation_audit
        WHERE approval_revision = :revision AND event_type = 'run'
    """ + (" FOR UPDATE" if apply else "")), {"revision": REVISION})).mappings().all()
    if runs:
        require(len(runs) == 1 and runs[0]["manifest_sha256"] == sha
                and runs[0]["row_payload"]["after_sha256"] == pre,
                "Previously adopted state changed; refusing")
        require(not apply or expected_snapshot == runs[0]["row_payload"]["before_sha256"],
                "Idempotent retry requires original precondition hash")
        return {"count": 23, "status": "already_applied", "file_sha256": sha,
                "snapshot_sha256": pre}
    plans = plan(before)
    result = {"count": len(plans), "status": "dry_run", "file_sha256": sha,
              "snapshot_sha256": pre}
    if not apply:
        return result
    require(expected_snapshot == pre, "Snapshot/precondition hash changed")
    audit_id = str(uuid.uuid4())
    await archive(db, audit_id, sha, actor, "before", before)
    for row, course, scope, identity, witnesses in plans:
        # Only legacy identity metadata is corrected. Fees, staged evidence,
        # IDs, FKs and all prior reconciliation audit records remain untouched.
        obj = SimpleNamespace(**course)
        obj.offering_identity = identity
        await persist_offerings(db, obj, SimpleNamespace(**row), scope)
        await db.execute(text("""
            UPDATE courses SET name = :name, degree_level = :degree,
                offering_identity = :identity WHERE id = :id
        """), {"id": course["id"], "name": scope["original_name"],
               "degree": row["degree_level"], "identity": identity})
    after = await snapshot(db, lock=True)
    await archive(db, audit_id, sha, actor, "after", after)
    await archive(db, audit_id, sha, actor, "run", {
        "before_sha256": pre, "after_sha256": digest(after),
        "witnesses": {str(p[1]["id"]): p[4] for p in plans},
    })
    return {**result, "status": "applied", "after_sha256": digest(after), "audit_id": audit_id}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url-env", default="REVIEWED23_DATABASE_URL",
                        help="Environment variable holding the URL; never put a secret URL in argv")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    parser.add_argument("--file-sha256")
    parser.add_argument("--snapshot-sha256")
    parser.add_argument("--confirm")
    parser.add_argument("--actor")
    args = parser.parse_args()
    url = os.environ.get(args.database_url_env, "")
    require(bool(url), f"Required database URL environment variable: {args.database_url_env}")
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url.removeprefix("postgresql://")
    require(url.startswith("postgresql+asyncpg://"), "PostgreSQL asyncpg required")
    engine = create_async_engine(url, isolation_level="SERIALIZABLE")
    try:
        async with AsyncSession(engine) as db:
            async with db.begin():
                result = await execute(db, apply=args.apply, expected_file=args.file_sha256,
                                       expected_snapshot=args.snapshot_sha256,
                                       confirm=args.confirm, actor=args.actor)
                if not args.apply:
                    await db.rollback()
        print(json.dumps(result, indent=2))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())