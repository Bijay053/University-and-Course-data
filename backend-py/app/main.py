"""FastAPI application entry point.

Mounts every router under ``/api/...`` so the path layout matches the existing
Node/Express server bit-for-bit; the React frontend will not need a single
URL change at cutover time.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import import_routes as _import_routes
from app.routers import backup as _backup
from app.routers import (
    acronyms,
    assessment_notes,
    auth,
    courses,
    dashboard,
    health,
    reviews,
    scrape,
    search,
    universities,
)

logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
log = logging.getLogger("uniportal")


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    log.info("Python backend starting up (debug=%s)", settings.debug)
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
app.include_router(dashboard.router, prefix="/api/dashboard", tags=["dashboard"])
app.include_router(acronyms.router, prefix="/api/settings", tags=["settings"])
app.include_router(_import_routes.router, prefix="/api")
app.include_router(_backup.router, prefix="/api", tags=["backup"])
app.include_router(assessment_notes.router, prefix="/api", tags=["assessment-notes"])
