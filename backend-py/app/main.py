"""FastAPI application entry point.

Mounts every router under ``/api/...`` so the path layout matches the existing
Node/Express server bit-for-bit; the React frontend will not need a single
URL change at cutover time.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import import_routes as _import_routes
from app.routers import backup as _backup
from app.routers import (
    acronyms,
    api_discovery,
    assessment_notes,
    auth,
    bulk_repair,
    changes,
    country_intelligence,
    courses,
    dashboard,
    health,
    knowledge_graph,
    locations,
    monitoring,
    performance,
    per_course_resources,
    publishing,
    auto_repair,
    recovery,
    regression_alerts,
    reviews,
    scrape,
    scrape_health,
    scraper_configs,
    search,
    snapshots,
    universities,
    users,
    verification,
)

logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
log = logging.getLogger("uniportal")


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    log.info("Python backend starting up (debug=%s)", settings.debug)
    try:
        from app.routers.auth import ensure_admin_user

        await asyncio.wait_for(ensure_admin_user(), timeout=10)
    except TimeoutError:
        log.warning(
            "ensure_admin_user timed out after 10s (skipping so API can start)"
        )
    except Exception:  # noqa: BLE001 -- startup must never crash the API
        log.exception("ensure_admin_user failed (skipping)")
    try:
        from app.database import AsyncSessionLocal
        from sqlalchemy import text as _text

        async def _ensure_pg_trgm() -> None:
            async with AsyncSessionLocal() as _s:
                await _s.execute(_text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
                await _s.commit()

        await asyncio.wait_for(_ensure_pg_trgm(), timeout=10)
        log.info("pg_trgm extension ensured")
    except TimeoutError:
        log.warning(
            "pg_trgm setup timed out after 10s (skipping so API can start)"
        )
    except Exception:  # noqa: BLE001
        log.exception("pg_trgm setup failed (fuzzy search unavailable)")
    try:
        from app.services.snapshot_store import is_enabled as _snap_enabled, setup_lifecycle_rules
        if _snap_enabled():
            ok = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    None, setup_lifecycle_rules
                ),
                timeout=15,
            )
            if ok:
                log.info("[SNAPSHOT] enabled — S3 lifecycle rules applied (html=90d, json/pdf=365d)")
            else:
                log.warning(
                "[SNAPSHOT] enabled — lifecycle rule apply failed "
                "(grant s3:PutLifecycleConfiguration to the IAM user, "
                "or apply rules manually via AWS Console → S3 → Management → Lifecycle rules)"
            )
        else:
            log.info("[SNAPSHOT] disabled (set AWS_* env vars to enable; SNAPSHOT_ENABLED=false to suppress)")
    except TimeoutError:
        log.warning(
            "[SNAPSHOT] startup check timed out after 15s "
            "(skipping so API can start)"
        )
    except Exception:  # noqa: BLE001
        log.exception("[SNAPSHOT] startup check failed (non-fatal)")
    yield
    log.info("Python backend shutting down")


app = FastAPI(
    title="University Portal API (Python)",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# All routers mount under /api to match the Node API layout exactly.
app.include_router(health.router, prefix="/api", tags=["health"])
app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(universities.router, prefix="/api", tags=["universities"])
app.include_router(courses.router, prefix="/api", tags=["courses"])
app.include_router(search.router, prefix="/api/search", tags=["search"])
app.include_router(reviews.router, prefix="/api", tags=["reviews"])
app.include_router(scrape.router, prefix="/api/scrape", tags=["scrape"])
app.include_router(api_discovery.router, prefix="/api/scrape", tags=["scrape"])
app.include_router(scrape_health.router, prefix="/api/scrape", tags=["scrape-health"])
app.include_router(per_course_resources.router, prefix="/api", tags=["per-course"])
app.include_router(dashboard.router, prefix="/api/dashboard", tags=["dashboard"])
app.include_router(acronyms.router, prefix="/api/settings", tags=["settings"])
app.include_router(scraper_configs.router, prefix="/api/settings", tags=["settings"])
app.include_router(regression_alerts.router, prefix="/api/settings", tags=["settings"])
app.include_router(auto_repair.router, prefix="/api/settings", tags=["settings"])
app.include_router(_import_routes.router, prefix="/api")
app.include_router(_backup.router, prefix="/api", tags=["backup"])
app.include_router(assessment_notes.router, prefix="/api", tags=["assessment-notes"])
app.include_router(users.router, prefix="/api", tags=["users"])
app.include_router(bulk_repair.router, prefix="/api", tags=["bulk-repair"])
app.include_router(performance.router, prefix="/api/performance", tags=["performance"])
app.include_router(locations.router, prefix="/api", tags=["locations"])
app.include_router(verification.router, prefix="/api", tags=["verification"])
app.include_router(changes.router, prefix="/api", tags=["changes"])
app.include_router(knowledge_graph.router, prefix="/api", tags=["knowledge-graph"])
app.include_router(country_intelligence.router, prefix="/api", tags=["country-intelligence"])
app.include_router(monitoring.router, prefix="/api", tags=["monitoring"])
app.include_router(publishing.router, prefix="/api", tags=["publishing"])
app.include_router(snapshots.router, prefix="/api/scrape", tags=["snapshots"])
app.include_router(recovery.router, prefix="/api/scrape", tags=["recovery"])
