"""Short-lived, single-use login challenges shared across API workers."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import HTTPException
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import settings

PREFIX = "portal:login-otp:v2:"
TTL = 600
COOLDOWN = 60
_RATE = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return count
"""
_CREATE = """
local old = redis.call('GET', KEYS[2])
if old then redis.call('DEL', ARGV[1] .. 'challenge:' .. old) end
redis.call('HSET', KEYS[1], 'user_id', ARGV[3], 'email', ARGV[4],
 'password_stamp', ARGV[5], 'digest', ARGV[6], 'attempts', 0)
redis.call('HSET', KEYS[1], 'delivered', 0)
redis.call('EXPIRE', KEYS[1], ARGV[7])
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[7])
return 1
"""
_VERIFY = """
local uid = redis.call('HGET', KEYS[1], 'user_id')
if not uid then return {0} end
if redis.call('HGET', KEYS[1], 'delivered') ~= '1' then return {0} end
local active = ARGV[1] .. 'active:' .. uid
if redis.call('GET', active) ~= ARGV[2] then return {0} end
if redis.call('HGET', KEYS[1], 'digest') ~= ARGV[3] then
 local n = redis.call('HINCRBY', KEYS[1], 'attempts', 1)
 if n >= 5 then redis.call('DEL', KEYS[1]) end
 return {-1}
end
local email = redis.call('HGET', KEYS[1], 'email')
local stamp = redis.call('HGET', KEYS[1], 'password_stamp')
redis.call('DEL', KEYS[1], active)
return {1, uid, email, stamp}
"""
_DELIVERED = """
if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
redis.call('HSET', KEYS[1], 'delivered', 1)
return 1
"""


@dataclass
class Challenge:
    token: str
    code: str | None
    expires_in: int = TTL
    resend_after: int = COOLDOWN


def stamp(value: str) -> str:
    return hmac.new(settings.session_secret.encode(), value.encode(), hashlib.sha256).hexdigest()


def _digest(token: str, code: str) -> str:
    return stamp(f"login-otp:{token}:{code}")


@asynccontextmanager
async def _redis():
    try:
        async with Redis.from_url(
            settings.redis_url, decode_responses=True,
            socket_connect_timeout=2, socket_timeout=2,
        ) as client:
            yield client
    except RedisError as exc:
        raise HTTPException(503, "Sign-in verification is temporarily unavailable. Please try again.") from exc


async def check_login_rate(address: str) -> None:
    async with _redis() as client:
        count = await client.eval(_RATE, 1, PREFIX + "login-rate:" + stamp(address), 900)
        if int(count) > 30:
            raise HTTPException(429, "Too many sign-in attempts. Try again in 15 minutes.")


async def issue(user) -> Challenge:
    async with _redis() as client:
        identity = stamp(user.email.lower())
        cooldown_key = PREFIX + "cooldown:" + identity
        if not await client.set(cooldown_key, "1", nx=True, ex=COOLDOWN):
            # Password verification has already passed. Recover the SAME proof,
            # without sending mail or renewing expiry/incorrect-attempt budgets.
            token = await client.get(PREFIX + f"active:{user.id}")
            if token:
                key = PREFIX + "challenge:" + token
                data = await client.hgetall(key)
                remaining = await client.ttl(key)
                if (
                    remaining > 0 and data.get("delivered") == "1"
                    and data.get("user_id") == str(user.id)
                    and data.get("email", "").lower() == user.email.lower()
                    and hmac.compare_digest(data.get("password_stamp", ""), stamp(user.password_hash))
                ):
                    return Challenge(token, None, remaining, max(1, await client.ttl(cooldown_key)))
            retry_after = max(1, await client.ttl(cooldown_key))
            raise HTTPException(429, "Please wait 60 seconds before requesting another code.",
                                headers={"Retry-After": str(retry_after)})
        count = await client.eval(_RATE, 1, PREFIX + "mail-rate:" + identity, 900)
        if int(count) > 5:
            raise HTTPException(429, "Too many verification emails. Try again in 15 minutes.")
        token = secrets.token_urlsafe(32)
        code = f"{secrets.randbelow(1_000_000):06d}"
        await client.eval(
            _CREATE, 2, PREFIX + "challenge:" + token, PREFIX + f"active:{user.id}",
            PREFIX, token, str(user.id), user.email, stamp(user.password_hash),
            _digest(token, code), TTL,
        )
        return Challenge(token, code)


async def mark_delivered(token: str) -> None:
    async with _redis() as client:
        marked = await client.eval(_DELIVERED, 1, PREFIX + "challenge:" + token)
    if not marked:
        raise HTTPException(401, "Verification expired. Please start sign-in again.")


async def discard(token: str) -> None:
    async with _redis() as client:
        await client.delete(PREFIX + "challenge:" + token)


async def consume(token: str, code: str) -> dict:
    async with _redis() as client:
        result = await client.eval(
            _VERIFY, 1, PREFIX + "challenge:" + token, PREFIX, token, _digest(token, code),
        )
    if not result or int(result[0]) != 1:
        raise HTTPException(401, "Invalid or expired verification code. Request a new code if needed.")
    return {"user_id": int(result[1]), "email": result[2], "password_stamp": result[3]}
