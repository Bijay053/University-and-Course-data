from __future__ import annotations

from pydantic import BaseModel, Field


class LoginBody(BaseModel):
    email: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class LoginResponse(BaseModel):
    user: dict


class MeResponse(BaseModel):
    user: dict | None = None
