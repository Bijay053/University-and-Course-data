"""Evidence-first domain contracts, independent of legacy storage labels."""
from datetime import datetime
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_url: str
    snippet: str = Field(min_length=1, max_length=2000)
    checked_at: datetime
    confidence: Literal["high", "medium", "low"]
    scope: Literal["course", "campus", "general"]


class FieldValue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: object = None
    status: Literal["verified", "unverified", "conflict"] = "unverified"
    evidence: list[Evidence] = Field(default_factory=list)
    @model_validator(mode="after")
    def require_proof(self):
        if self.status == "verified" and (not self.evidence or any(e.confidence != "high" for e in self.evidence)):
            raise ValueError("Verified values require high-confidence evidence")
        if self.status == "conflict" and len(self.evidence) < 2:
            raise ValueError("A conflict must preserve both source excerpts")
        return self


class Fee(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: Decimal = Field(ge=0)
    basis: Literal["annual", "full_course", "per_credit", "per_unit", "per_semester", "per_term"]
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    year: int | None = Field(default=None, ge=2000, le=2100)
    indicative: bool = False
    scholarship_adjusted: bool = False
    standard_load: Decimal | None = Field(default=None, gt=0)
    terms_per_year: Decimal | None = Field(default=None, gt=0)
    duration_years: Decimal | None = Field(default=None, gt=0)

    def annual_equivalent(self):
        if self.basis == "annual":
            return self.amount
        if self.basis == "full_course" and self.duration_years is not None:
            return self.amount / self.duration_years
        if self.basis in {"per_credit","per_unit"} and self.standard_load is not None:
            return self.amount * self.standard_load
        if self.basis in {"per_semester","per_term"} and self.terms_per_year is not None:
            return self.amount * self.terms_per_year
        return None


class Intake(BaseModel):
    model_config = ConfigDict(extra="forbid")
    months: list[str] = Field(default_factory=list)
    original_wording: str
    availability: Literal["open", "closed", "unknown"] = "unknown"
    campus: str | None = None
    audience: Literal["international", "domestic", "unspecified"] = "unspecified"
    status: Literal["verified", "unverified", "conflict"] = "unverified"


class Duration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    full_time_years: Decimal | None = Field(default=None, gt=0)
    minimum_years: Decimal | None = Field(default=None, gt=0)
    maximum_years: Decimal | None = Field(default=None, gt=0)
    condition: str | None = None
    @model_validator(mode="after")
    def ordered_range(self):
        if self.minimum_years is not None and self.maximum_years is not None and self.minimum_years > self.maximum_years:
            raise ValueError("Duration minimum cannot exceed maximum")
        return self


class EnglishRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    test: str
    variant: str | None = None
    overall: Decimal | None = None
    bands: dict[str, Decimal] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)
