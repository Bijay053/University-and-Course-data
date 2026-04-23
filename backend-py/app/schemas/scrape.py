from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class StartScrapeBody(BaseModel):
    university_id: int = Field(..., alias="universityId")
    fast_mode: bool = Field(default=False, alias="fastMode")
    model_config = ConfigDict(populate_by_name=True)


class BulkScrapeBody(BaseModel):
    university_ids: list[int] = Field(..., alias="universityIds", min_length=1)
    fast_mode: bool = Field(default=False, alias="fastMode")
    model_config = ConfigDict(populate_by_name=True)


class ScrapeJobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    runtime_job_id: str
    university_id: int | None
    university_name: str | None
    job_type: str
    status: str
    imported: int
    skipped: int
    errors: int
    total_found: int
    current: int
    started_at: datetime
    completed_at: datetime | None
    error_message: str | None


class ScrapeStartResponse(BaseModel):
    job_id: str
    status: str = "queued"


class BulkScrapeResponse(BaseModel):
    session_id: str
    queued: int
