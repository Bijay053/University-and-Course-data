"""Pydantic schemas for university CRUD.

Bug #4 fixes baked in: ``country`` and ``city`` are required, must be at
least 2 chars, and ``Unknown`` (in any case) is rejected. The Node API
silently accepted ``"Unknown"`` for both, which broke the location filter
on the public Course Search page.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


def _reject_unknown(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be empty")
    if cleaned.lower() == "unknown":
        raise ValueError(f"{field_name} must not be 'Unknown'")
    if len(cleaned) < 2:
        raise ValueError(f"{field_name} must be at least 2 characters")
    return cleaned


class UniversityBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    country: str = Field(..., min_length=2, max_length=100)
    city: str = Field(..., min_length=2, max_length=100)
    website: HttpUrl | None = None
    description: str | None = None
    logo_url: str | None = None
    scrape_url: HttpUrl | None = None
    fee_page_url: str | None = None
    requirements_page_url: str | None = None
    scholarship_page_url: str | None = None
    academic_requirements_page_url: str | None = None
    featured: bool = False
    featured_priority: int = 0

    @field_validator("country")
    @classmethod
    def _country(cls, v: str) -> str:
        return _reject_unknown(v, "country")

    @field_validator("city")
    @classmethod
    def _city(cls, v: str) -> str:
        return _reject_unknown(v, "city")


class UniversityCreate(UniversityBase):
    pass


class UniversityUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    country: str | None = Field(default=None, min_length=2)
    city: str | None = Field(default=None, min_length=2)
    website: HttpUrl | None = None
    description: str | None = None
    logo_url: str | None = None
    scrape_url: HttpUrl | None = None
    fee_page_url: str | None = None
    requirements_page_url: str | None = None
    scholarship_page_url: str | None = None
    academic_requirements_page_url: str | None = None
    featured: bool | None = None
    featured_priority: int | None = None

    @field_validator("country")
    @classmethod
    def _country(cls, v: str | None) -> str | None:
        return _reject_unknown(v, "country") if v else v

    @field_validator("city")
    @classmethod
    def _city(cls, v: str | None) -> str | None:
        return _reject_unknown(v, "city") if v else v


class UniversityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    country: str
    city: str
    website: str | None = None
    description: str | None = None
    logo_url: str | None = None
    scrape_url: str | None = None
    fee_page_url: str | None = None
    requirements_page_url: str | None = None
    scholarship_page_url: str | None = None
    academic_requirements_page_url: str | None = None
    featured: bool = False
    featured_priority: int = 0
    course_count: int = 0
    created_at: datetime
    updated_at: datetime


class UniversityListResponse(BaseModel):
    data: list[UniversityRead]
    total: int
    page: int
    limit: int


class BulkImportResult(BaseModel):
    created: int = 0
    skipped: int = 0
    errors: list[str] = Field(default_factory=list)


# --- UI camelCase compatibility shim ---
_CAMEL_ALIASES = {
    "scrape_url": "scrapeUrl",
    "fee_page_url": "feePageUrl",
    "requirements_page_url": "requirementsPageUrl",
    "academic_requirements_page_url": "academicRequirementsPageUrl",
    "scholarship_page_url": "scholarshipPageUrl",
    "logo_url": "logoUrl",
    "course_count": "courseCount",
    "featured_priority": "featuredPriority",
    "created_at": "createdAt",
    "updated_at": "updatedAt",
}

_orig_uni_dump = UniversityRead.model_dump

def _uni_dump_with_camel(self, *args, **kwargs):
    d = _orig_uni_dump(self, *args, **kwargs)
    for snake, camel in _CAMEL_ALIASES.items():
        if snake in d and camel not in d:
            d[camel] = d[snake]
    return d

UniversityRead.model_dump = _uni_dump_with_camel
