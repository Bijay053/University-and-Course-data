"""Route-parity smoke test.

Hits every URL the React UI under ``artifacts/university-portal`` calls
and asserts the response is registered (no framework 404) and not 5xx.

The previous "whack-a-mole" wave of bugs was caused by handlers being
deleted or never ported when the Node→Python rewrite happened. The UI
would then call an unregistered URL, get a generic 404 from the
framework, surface it as "Save failed" / "Error", and the user would
ping us. This test guards against that whole class.

Strategy
--------
1. **Route-table check** — for every (method, path-template) pair the
   UI calls, assert the path matches a real registered route. This
   catches "endpoint missing" without a single DB query.
2. **Live-request smoke** — run an async test under pytest-asyncio
   with one event loop, insert a throwaway university + course so
   we have known-good IDs, and call each endpoint with realistic
   payloads via ``httpx.AsyncClient`` + ``ASGITransport``. The
   handler MUST run (no 404 from the router) and MUST NOT raise
   (no 5xx). 4xx for invalid inputs is fine — proves the handler
   is doing its job.

Bugs this guards against (production triage list):
  * L — POST/DELETE /api/settings/acronyms returned 405
  * M — /api/import/excel returned 404 in some envs
  * N — POST /api/universities/:id/bulk-english returned 404
  * O — POST /api/universities/:id/bulk-academic returned 404
  * P — POST /api/universities/:id/bulk-scholarships returned 404
  * Q — PUT /api/scrape/staged/:id returned 405 (only DELETE existed)
"""
from __future__ import annotations

import io
from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.database import AsyncSessionLocal
from app.main import app


# ─── Route table builder ─────────────────────────────────────────────────
def _routes(ids: dict[str, int]) -> list[tuple[str, str, dict[str, Any] | None]]:
    """Build the (method, path, body) list using real IDs from the seed."""
    u = ids["uni_id"]
    c = ids["course_id"]
    return [
        # Health
        ("GET", "/api/health", None),
        ("GET", "/api/healthz", None),
        # Universities CRUD + featured toggle
        ("GET", "/api/universities", None),
        ("GET", f"/api/universities/{u}", None),
        ("GET", f"/api/universities/{u}/courses", None),
        (
            "PATCH",
            f"/api/universities/{u}/featured",
            {"featured": True, "featuredPriority": 5},
        ),
        # Bulk panels — Bugs N, O, P
        (
            "POST",
            f"/api/universities/{u}/bulk-english",
            {"courseIds": [c], "testType": "PTE", "overall": 65},
        ),
        (
            "POST",
            f"/api/universities/{u}/bulk-academic",
            {
                "courseIds": [c],
                "academicLevel": "Bachelor",
                "academicScore": 80,
                "scoreType": "Percentage",
                "academicCountry": "Bhutan",  # unique → no dup-409
            },
        ),
        (
            "POST",
            f"/api/universities/{u}/bulk-scholarships",
            {"courseIds": [c], "name": "Bulk Test Scholarship", "amount": 1000},
        ),
        # University-level cross-resource collections
        ("GET", f"/api/universities/{u}/scholarship-courses", None),
        ("GET", f"/api/universities/{u}/academic-requirements", None),
        # Per-course resources (raw-data tab)
        ("GET", f"/api/courses/{c}/scholarships", None),
        ("POST", f"/api/courses/{c}/scholarships", {"name": "S2"}),
        ("PATCH", f"/api/scholarships/{ids['sch_id']}", {"name": "Updated"}),
        ("GET", f"/api/courses/{c}/academic-requirements", None),
        (
            "POST",
            f"/api/courses/{c}/academic-requirements",
            {
                "academicLevel": "Bachelor",
                "academicScore": 75,
                "academicCountry": "Nepal",
            },
        ),
        ("PATCH", f"/api/academic-requirements/{ids['acad_id']}", {"academicScore": 82}),
        ("GET", f"/api/courses/{c}/english-requirements", None),
        (
            "POST",
            f"/api/courses/{c}/english-requirements",
            {"testType": "TOEFL", "overall": 90},
        ),
        ("PATCH", f"/api/english-requirements/{ids['eng_id']}", {"overall": 7.0}),
        ("GET", f"/api/courses/{c}/intakes", None),
        (
            "POST",
            f"/api/courses/{c}/intakes",
            {"intakeMonth": "July", "intakeYear": 2026},
        ),
        ("PATCH", f"/api/intakes/{ids['int_id']}", {"intakeYear": 2027}),
        ("GET", f"/api/courses/{c}/fees", None),
        (
            "POST",
            f"/api/courses/{c}/fees",
            {"internationalFee": 26000, "feeTerm": "Per Year", "currency": "AUD"},
        ),
        ("PATCH", f"/api/fees/{ids['fee_id']}", {"internationalFee": 27000}),
        # Settings — Bug L
        ("GET", "/api/settings/acronyms", None),
        ("POST", "/api/settings/acronyms", {"acronym": "ROUTEPARITY", "note": "x"}),
        ("GET", "/api/settings/academic-levels", None),
        (
            "POST",
            "/api/settings/academic-levels",
            {"name": "Route Parity Level", "sortOrder": 999},
        ),
        (
            "POST",
            "/api/settings/academic-levels/reorder",
            {"items": [{"id": 1, "sortOrder": 1}]},
        ),
        # Scrape — Bug Q + repair + backup
        (
            "PUT",
            f"/api/scrape/staged/{ids['sc_id']}",
            {"courseName": "Edited Title", "duration": 3, "durationTerm": "Year"},
        ),
        ("GET", f"/api/scrape/staged/{ids['sc_id']}/backup-match", None),
        (
            "POST",
            f"/api/scrape/staged/{ids['sc_id']}/apply-backup",
            {"forceOverwrite": False},
        ),
        (
            "POST",
            "/api/scrape/staged/bulk-apply-backup",
            {"stagedCourseIds": [ids["sc_id"]]},
        ),
        ("GET", f"/api/scrape/repair/missing/{u}", None),
        # Misc
        ("GET", "/api/scrape/active", None),
        ("GET", "/api/scrape/history", None),
        ("GET", "/api/dashboard/summary", None),
    ]


# ─── DB helpers (run inside the same loop as the test) ───────────────────
async def _seed_setup() -> dict[str, int]:
    async with AsyncSessionLocal() as db:
        uni_id = (
            await db.execute(
                text(
                    "INSERT INTO universities (name, country, city) "
                    "VALUES (:n, 'Australia', 'Sydney') RETURNING id"
                ),
                {"n": "ROUTE_PARITY_TEST_UNI"},
            )
        ).scalar_one()
        course_id = (
            await db.execute(
                text(
                    "INSERT INTO courses (university_id, name, status) "
                    "VALUES (:u, :n, 'active') RETURNING id"
                ),
                {"u": uni_id, "n": "Route Parity Test Course"},
            )
        ).scalar_one()
        sch_id = (
            await db.execute(
                text(
                    "INSERT INTO scholarships (course_id, name) "
                    "VALUES (:c, 'Test Scholarship') RETURNING id"
                ),
                {"c": course_id},
            )
        ).scalar_one()
        eng_id = (
            await db.execute(
                text(
                    "INSERT INTO english_requirements (course_id, test_type, overall) "
                    "VALUES (:c, 'IELTS', 6.5) RETURNING id"
                ),
                {"c": course_id},
            )
        ).scalar_one()
        acad_id = (
            await db.execute(
                text(
                    "INSERT INTO academic_requirements "
                    "(course_id, academic_level, academic_score, academic_country) "
                    "VALUES (:c, 'Bachelor', 75, 'India') RETURNING id"
                ),
                {"c": course_id},
            )
        ).scalar_one()
        int_id = (
            await db.execute(
                text(
                    "INSERT INTO intakes (course_id, intake_month) "
                    "VALUES (:c, 'February') RETURNING id"
                ),
                {"c": course_id},
            )
        ).scalar_one()
        fee_id = (
            await db.execute(
                text(
                    "INSERT INTO fees (course_id, international_fee, currency) "
                    "VALUES (:c, 25000, 'AUD') RETURNING id"
                ),
                {"c": course_id},
            )
        ).scalar_one()
        sc_id = (
            await db.execute(
                text(
                    "INSERT INTO scraped_courses "
                    "(scrape_job_id, university_id, course_name, status) "
                    "VALUES ('route-parity-test', :u, :n, 'pending') RETURNING id"
                ),
                {"u": uni_id, "n": "Route Parity Staged"},
            )
        ).scalar_one()
        await db.commit()
        return {
            "uni_id": uni_id,
            "course_id": course_id,
            "sch_id": sch_id,
            "eng_id": eng_id,
            "acad_id": acad_id,
            "int_id": int_id,
            "fee_id": fee_id,
            "sc_id": sc_id,
        }


async def _seed_teardown(ids: dict[str, int]) -> None:
    async with AsyncSessionLocal() as db:
        # FK cascades clear all child rows, so dropping the university and
        # the staged row is enough.
        await db.execute(
            text("DELETE FROM scraped_courses WHERE id = :i"), {"i": ids["sc_id"]}
        )
        await db.execute(
            text("DELETE FROM universities WHERE id = :i"), {"i": ids["uni_id"]}
        )
        # Best-effort cleanup of the parity acronym + academic level
        # we may have inserted via the smoke loop.
        await db.execute(
            text("DELETE FROM course_acronym_options WHERE acronym = 'ROUTEPARITY'")
        )
        await db.execute(
            text(
                "DELETE FROM academic_level_options WHERE name = 'Route Parity Level'"
            )
        )
        await db.commit()


# ─── Tests ────────────────────────────────────────────────────────────────
def test_every_route_is_in_the_app_route_table() -> None:
    """Pure routing check — does every (method, path) pair we plan to
    request actually map to a registered handler? No DB needed."""
    matchers: list[tuple[set[str], "re.Pattern[str]"]] = []
    for r in app.routes:
        if not isinstance(r, APIRoute):
            continue
        matchers.append((set(r.methods or set()), r.path_regex))

    fake_ids = {
        "uni_id": 1,
        "course_id": 1,
        "sch_id": 1,
        "eng_id": 1,
        "acad_id": 1,
        "int_id": 1,
        "fee_id": 1,
        "sc_id": 1,
    }
    missing: list[str] = []
    for method, path, _ in _routes(fake_ids):
        hit = any(method in methods and rx.match(path) for methods, rx in matchers)
        if not hit:
            missing.append(f"{method} {path}")
    assert not missing, "Routes missing from FastAPI registry:\n  " + "\n  ".join(
        missing
    )


@pytest.mark.asyncio
async def test_routes_smoke() -> None:
    """Live-request smoke: every endpoint dispatches cleanly. No
    framework 404 (= unrouted) and no 5xx (= server crash). Other 4xx
    is fine — proves the handler ran and rejected the input."""
    ids = await _seed_setup()
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as ac:
            failures: list[str] = []
            for method, path, body in _routes(ids):
                if body is None:
                    resp = await ac.request(method, path)
                else:
                    resp = await ac.request(method, path, json=body)
                # Distinguish framework 404 (== "no route matched") from
                # handler 404 (== "row not found"). Only the former is a
                # parity bug; the latter means the route exists and ran.
                unrouted_404 = (
                    resp.status_code == 404
                    and resp.headers.get("content-type", "").startswith(
                        "application/json"
                    )
                    and resp.json().get("detail") == "Not Found"
                )
                crashed = resp.status_code >= 500
                if unrouted_404 or crashed:
                    failures.append(
                        f"{method} {path} → {resp.status_code}: {resp.text[:200]}"
                    )
            assert not failures, (
                "Endpoints are unrouted or 5xx-ing:\n  " + "\n  ".join(failures)
            )

            # ── Shape contracts the React UI relies on.
            # Architect-flagged regressions: these used to return wrong
            # keys (looked fine to non-404/5xx loop, broke the UI).
            u, c = ids["uni_id"], ids["course_id"]

            # repair/missing → must wrap rows in `{courses: [...]}`
            r = await ac.get(f"/api/scrape/repair/missing/{u}")
            assert r.status_code == 200, r.text
            j = r.json()
            assert isinstance(j, dict) and "courses" in j and isinstance(
                j["courses"], list
            ), f"repair/missing must return {{courses: [...]}} — got {j!r}"

            # bulk-apply-backup → must return `{results, summary}` and
            # accept `ids` (UI key), not `stagedCourseIds` (legacy).
            r = await ac.post(
                "/api/scrape/staged/bulk-apply-backup",
                json={"ids": [ids["sc_id"]], "forceOverwrite": False},
            )
            assert r.status_code == 200, r.text
            j = r.json()
            assert "results" in j and "summary" in j, (
                f"bulk-apply-backup must return {{results, summary}} — got {j!r}"
            )
            assert {"matched", "noMatch", "failed"} <= j["summary"].keys(), (
                f"summary missing keys — got {j['summary']!r}"
            )

            # apply-backup single → must include `appliedFields` array
            r = await ac.post(
                f"/api/scrape/staged/{ids['sc_id']}/apply-backup",
                json={"forceOverwrite": False},
            )
            # 404 ("no backup match") is the realistic outcome in the
            # test DB — the handler ran and replied. 200 also fine.
            assert r.status_code in (200, 404), r.text
            if r.status_code == 200:
                j = r.json()
                assert "appliedFields" in j and isinstance(
                    j["appliedFields"], list
                ), f"apply-backup must include appliedFields[] — got {j!r}"

            # bulk-academic 409 → top-level `{error, conflicts}`, NOT
            # wrapped under FastAPI's `detail`. We just inserted a
            # (course, country=India) row in the seed — re-posting the
            # same combo must produce the conflict response.
            r = await ac.post(
                f"/api/universities/{u}/bulk-academic",
                json={
                    "courseIds": [c],
                    "academicLevel": "Bachelor",
                    "academicScore": 75,
                    "academicCountry": "India",  # duplicate of seed row
                },
            )
            assert r.status_code == 409, (
                f"bulk-academic dup must 409 — got {r.status_code}: {r.text}"
            )
            j = r.json()
            assert (
                j.get("error") == "duplicate" and "conflicts" in j
            ), f"bulk-academic 409 must be {{error, conflicts}} — got {j!r}"
    finally:
        await _seed_teardown(ids)


def test_import_excel_route_registered() -> None:
    """Bug M: /api/import/excel must accept a multipart upload. The
    dummy file will be rejected for content — what we're testing is
    that the route exists and a handler runs, not a framework 404."""
    with TestClient(app) as client:
        files = {
            "file": (
                "empty.xlsx",
                io.BytesIO(b"PK\x03\x04 not really xlsx"),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        }
        resp = client.post("/api/import/excel", files=files)
    unrouted = (
        resp.status_code == 404
        and resp.headers.get("content-type", "").startswith("application/json")
        and resp.json().get("detail") == "Not Found"
    )
    assert not unrouted and resp.status_code < 500, (
        f"POST /api/import/excel → {resp.status_code}. Body: {resp.text[:200]}"
    )
