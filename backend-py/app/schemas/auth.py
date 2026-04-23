from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field


class LoginBody(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1)


class LoginResponse(BaseModel):
    user: dict


class MeResponse(BaseModel):
    user: dict | None = None
