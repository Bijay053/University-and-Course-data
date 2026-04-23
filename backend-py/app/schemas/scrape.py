"""Schemas matching Node's API for frontend compatibility."""
from __future__ import annotations
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field


class StartScrapeBody(BaseModel):
    """Accepts Node's superset of fields. Only `url` OR `universityId` required."""
    url: str | None = None
    university_id: int | None = Field(default=None, alias="universityId")
    university_name: str | None = Field(default=None, alias="universityName")
    university_country: str | None = Field(default=None, alias="universityCountry")
    university_city: str | None = Field(default=None, alias="universityCity")
    fee_page: str | None = Field(default=None, alias="feePage")
    requirements_page: str | None = Field(default=None, alias="requirementsPage")
    scholarship_page: str | None = Field(default=None, alias="scholarshipPage")
    academic_requirements_page: str | None = Field(default=None, alias="academicRequirementsPage")
    fast_mode: bool = Field(default=False, alias="fastMode")
    bulk_mode: bool = Field(default=False, alias="bulkMode")
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class BulkScrapeBody(BaseModel):
    university_ids: list[int] = Field(..., alias="universityIds", min_length=1)
    fast_mode: bool = Field(default=False, alias="fastMode")
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class ScrapeStartResponse(BaseModel):
    """Match Node's response shape — UI expects these field names."""
    job_id: str = Field(..., serialization_alias="jobId")
    runtime_job_id: str = Field(..., serialization_alias="runtimeJobId")
    status: str = "queued"
    ok: bool = True
    model_config = ConfigDict(populate_by_name=True)


class BulkScrapeResponse(BaseModel):
    session_id: str = Field(..., serialization_alias="sessionId")
    queued: int
    ok: bool = True
    model_config = ConfigDict(populate_by_name=True)


class ScrapeJobRead(BaseModel):
    runtime_job_id: str
    university_id: int | None = None
    university_name: str | None = None
    job_type: str | None = None
    status: str
    imported: int = 0
    skipped: int = 0
    errors: int = 0
    total_found: int = 0
    current: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None
    model_config = ConfigDict(from_attributes=True)
