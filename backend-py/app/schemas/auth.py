from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

# Use a permissive regex (not Pydantic's EmailStr) because the default admin
# uses `admin@university-portal.local` and EmailStr rejects reserved TLDs
# like `.local`. We only need a sanity check here — actual delivery is
# the operator's responsibility once SMTP is configured.
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def _validate_email(v: str) -> str:
    v = (v or "").strip()
    if not _EMAIL_RE.match(v):
        raise ValueError("Enter a valid email address")
    return v.lower()


class LoginBody(BaseModel):
    email: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class LoginResponse(BaseModel):
    user: dict
    permissions: list[str] = []
    is_super_admin: bool = False


class MeResponse(BaseModel):
    user: dict | None = None
    permissions: list[str] = []
    is_super_admin: bool = False


class ForgotPasswordBody(BaseModel):
    email: str

    _v_email = field_validator("email")(_validate_email)


class ResetPasswordBody(BaseModel):
    token: str = Field(..., min_length=8)
    new_password: str = Field(..., min_length=8, max_length=128)


class GenericOk(BaseModel):
    ok: bool = True
    message: str | None = None
    # Dev-only: surfaced when SMTP is not configured so the operator can
    # still complete the reset flow during local dev.
    debug_reset_url: str | None = None


class UserOut(BaseModel):
    id: int
    email: str
    full_name: str
    is_active: bool
    is_super_admin: bool


class UserCreateBody(BaseModel):
    email: str
    full_name: str = Field("", max_length=120)
    password: str = Field(..., min_length=8, max_length=128)
    is_super_admin: bool = False
    permissions: list[str] = []

    _v_email = field_validator("email")(_validate_email)


class UserUpdateBody(BaseModel):
    full_name: str | None = Field(None, max_length=120)
    is_active: bool | None = None
    is_super_admin: bool | None = None
    new_password: str | None = Field(None, min_length=8, max_length=128)


class PermissionsUpdateBody(BaseModel):
    permissions: list[str]
