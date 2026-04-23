from __future__ import annotations

from pydantic import BaseModel, Field


class SearchCourseRow(BaseModel):
    course_id: int
    course_name: str
    university_id: int
    university_name: str
    degree_level: str | None = None
    course_location: str | None = None
    duration: float | None = None
    duration_term: str | None = None
    international_fee: float | None = None
    ielts_overall: float | None = None
    intake_months: list[str] | None = None
    rank: float | None = None


class SearchCourseResponse(BaseModel):
    results: list[SearchCourseRow] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    limit: int = 20


class SearchOptionsResponse(BaseModel):
    countries: list[str] = Field(default_factory=list)
    cities: list[str] = Field(default_factory=list)
    universities: list[dict] = Field(default_factory=list)
    degree_levels: list[str] = Field(default_factory=list)
    intake_months: list[str] = Field(default_factory=list)


class SearchStatsResponse(BaseModel):
    total_universities: int = 0
    total_courses: int = 0
    countries: int = 0
    average_fee: float | None = None
