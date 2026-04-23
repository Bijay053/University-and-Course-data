"""Bug #4 regression test: UniversityCreate must reject Unknown / blank /
single-character country and city values."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.university import UniversityCreate


def _ok():
    return {"name": "Test U", "country": "Australia", "city": "Sydney"}


def test_valid_university():
    u = UniversityCreate(**_ok())
    assert u.country == "Australia"


@pytest.mark.parametrize("bad", ["Unknown", "unknown", "UNKNOWN", "u", "", "  "])
def test_rejects_bad_country(bad):
    with pytest.raises(ValidationError):
        UniversityCreate(**{**_ok(), "country": bad})


@pytest.mark.parametrize("bad", ["Unknown", "unknown", "UNKNOWN", "x"])
def test_rejects_bad_city(bad):
    with pytest.raises(ValidationError):
        UniversityCreate(**{**_ok(), "city": bad})
