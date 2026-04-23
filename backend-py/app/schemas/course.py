from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CourseBase(BaseModel):
    name: str = Field(..., min_length=1)
    university_id: int
    category: str | None = None
    sub_category: str | None = None
    course_website: str | None = None
    duration: float | None = None
    duration_term: str | None = None
    study_mode: str | None = None
    degree_level: str | None = None
    study_load: str | None = None
    language: str | None = None
    description: str | None = None
    course_structure: str | None = None
    career_outcomes: str | None = None
    other_test: str | None = None
    other_test_score: str | None = None
    other_requirement: str | None = None
    course_location: str | None = None
    student_market: str | None = None
    delivery_mode: str | None = None
    international_eligible: bool | None = None
    on_campus_available: bool | None = None


class CourseCreate(CourseBase):
    pass


class CourseUpdate(BaseModel):
    name: str | None = None
    category: str | None = None
    sub_category: str | None = None
    course_website: str | None = None
    duration: float | None = None
    duration_term: str | None = None
    study_mode: str | None = None
    degree_level: str | None = None
    study_load: str | None = None
    language: str | None = None
    description: str | None = None
    course_structure: str | None = None
    career_outcomes: str | None = None
    other_test: str | None = None
    other_test_score: str | None = None
    other_requirement: str | None = None
    course_location: str | None = None
    student_market: str | None = None
    delivery_mode: str | None = None
    international_eligible: bool | None = None
    on_campus_available: bool | None = None
    status: str | None = None
    eligibility_status: str | None = None


class CourseRead(CourseBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    eligibility_status: str
    eligibility_reason: str | None = None
    eligibility_confidence: float | None = None
    approval_status: str
    approval_score: float | None = None
    approved_at: datetime | None = None
    last_reviewed_at: datetime | None = None
    last_edited_at: datetime | None = None
    last_edited_by: str | None = None
    created_at: datetime
    updated_at: datetime


class CourseListResponse(BaseModel):
    data: list[CourseRead]
    total: int
    page: int
    limit: int
