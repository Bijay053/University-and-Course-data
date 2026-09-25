from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CourseLocationOffering(BaseModel):
    id: str
    location: str


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
    @model_validator(mode="before")
    @classmethod
    def offerings_are_read_only(cls, value):
        if isinstance(value, dict) and {"offerings", "locations"} & value.keys():
            raise ValueError("Course offerings and locations are read-only; use evidence review")
        return value

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
    requestedCourseId: int | None = None
    offerings: list[CourseLocationOffering] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
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
