"""SQLAlchemy model for the discovery_url_cache table (fetch-layer brief, C1).

Caches the raw discovered course-link list per university for 7 days so a
re-scrape within that window skips the entire discovery phase (BFS crawl,
sitemap probes, browser rendering, Wayback sweeps — typically 2-5 minutes and
dozens of Scrape.do calls) and goes straight to extraction.

Write policy (enforced in orchestrator, not here):
* only written when the run discovered >= 5 links AND the run's fetch-fail
  rate stayed under 30% (a degraded run must not poison the cache);
* never written for SearchStax / API-provider universities whose link dicts
  embed large prebuilt payloads (those providers are already single-call fast
  and their payloads go stale).

Read policy: skipped when the run was started with forceDiscovery=true
(request_payload flag) or the entry is older than 7 days.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Integer, TIMESTAMP, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class DiscoveryUrlCache(Base):
    __tablename__ = "discovery_url_cache"

    university_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # List of {"name": str, "url": str} dicts — payload keys are stripped
    # before write so entries stay small and never carry stale prebuilt data.
    links: Mapped[list] = mapped_column(JSONB, nullable=False)
    link_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    discovered_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
