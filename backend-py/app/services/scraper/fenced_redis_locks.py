"""Generation-aware Redis locks for autonomous verification.

The primary university key stays a job ID for compatibility with normal scrape
workers and startup checks. A sidecar carries the generation. Redis CAS scripts
never replace another job's lock or release a successor generation's lock.
Only a transaction-locked current DB owner with recorded revocation lineage
may adopt a predecessor's lock (including legacy job-ID-only values).
"""
import json

from sqlalchemy import text

from app.services.worker_fencing import current_owner, guard_transaction, OwnershipLost

_CAN_ADOPT = """
local function adoptable(token)
    if token == ARGV[2] then return true end
    local revoked = cjson.decode(ARGV[3])
    if not token then return #revoked > 0 end
    for _, generation in ipairs(revoked) do
        if token == generation then return true end
    end
    return false
end
"""

_ACQUIRE_UNIVERSITY = _CAN_ADOPT + """
local holder = redis.call('GET', KEYS[1])
if holder and holder ~= ARGV[1] then return 0 end
if holder and not adoptable(redis.call('GET', KEYS[2])) then return 0 end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[4])
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[4])
return 1
"""

_RELEASE_UNIVERSITY = """
if redis.call('GET', KEYS[1]) == ARGV[1]
and redis.call('GET', KEYS[2]) == ARGV[2] then
    redis.call('DEL', KEYS[1], KEYS[2])
    return 1
end
return 0
"""

_RELEASE_LEGACY_UNIVERSITY = """
if redis.call('GET', KEYS[1]) == ARGV[1]
and not redis.call('GET', KEYS[2]) then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

_ACQUIRE_SLOT = _CAN_ADOPT + """
local score = redis.call('ZSCORE', KEYS[1], ARGV[1])
if score then
    if not adoptable(redis.call('HGET', KEYS[2], ARGV[1])) then return 0 end
elseif redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[5]) then
    return 0
end
redis.call('ZADD', KEYS[1], ARGV[4], ARGV[1])
redis.call('HSET', KEYS[2], ARGV[1], ARGV[2])
return 1
"""

_RELEASE_SLOT = """
if redis.call('HGET', KEYS[2], ARGV[1]) == ARGV[2] then
    redis.call('ZREM', KEYS[1], ARGV[1])
    redis.call('HDEL', KEYS[2], ARGV[1])
    return 1
end
return 0
"""

_ACQUIRE_LEGACY_SLOT = """
local stale = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[2])
for _, member in ipairs(stale) do
    if redis.call('HEXISTS', KEYS[2], member) == 0 then
        redis.call('ZREM', KEYS[1], member)
    end
end
local active = redis.call('ZCARD', KEYS[1])
if redis.call('HEXISTS', KEYS[2], ARGV[4]) == 1 then return active end
if active >= tonumber(ARGV[3]) then return active end
redis.call('ZADD', KEYS[1], ARGV[1], ARGV[4])
return -1
"""

_RELEASE_LEGACY_SLOT = """
if redis.call('HEXISTS', KEYS[2], ARGV[1]) == 0 then
    return redis.call('ZREM', KEYS[1], ARGV[1])
end
return 0
"""


async def _authority(db, job_id):
    owner = current_owner.get()
    if owner is None or owner.key != f"verification:{job_id}":
        raise OwnershipLost("University-lock recovery requires its current verification generation")
    await guard_transaction(db)
    # guard_transaction keeps the shared generation lock across the Redis Lua
    # command. Revocation/replacement cannot race this check and side effect.
    lineage = (await db.execute(text(
        "SELECT revoked_generations FROM autonomous_worker_claims WHERE claim_key = :key"
    ), {"key": owner.key})).scalar_one()
    return owner.generation, json.dumps(lineage)


async def acquire_university_lock(db, redis, key, job_id, *, ttl=14400):
    generation, lineage = await _authority(db, job_id)
    return bool(await redis.eval(
        _ACQUIRE_UNIVERSITY, 2, key, key + ":generation",
        job_id, generation, lineage, ttl,
    ))


async def release_university_lock(redis, key, job_id, generation):
    return bool(await redis.eval(
        _RELEASE_UNIVERSITY, 2, key, key + ":generation", job_id, generation,
    ))


async def replace_legacy_university_lock(redis, key, expected_job, job_id):
    """Normal stale-lock cleanup must not race a generation-aware adoption."""
    return bool(await redis.eval("""
        if redis.call('GET', KEYS[1]) == ARGV[1]
        and not redis.call('GET', KEYS[2]) then
            redis.call('SET', KEYS[1], ARGV[2], 'EX', 14400)
            return 1
        end
        return 0
    """, 2, key, key + ":generation", expected_job, job_id))


async def release_legacy_university_lock(redis, key, job_id):
    return bool(await redis.eval(
        _RELEASE_LEGACY_UNIVERSITY, 2, key, key + ":generation", job_id,
    ))


def cleanup_legacy_university_lock(redis, key, observed_holder):
    """Synchronous startup cleanup; absence is never permission to delete.

    The DB status lookup happens outside Redis. Compare the originally observed
    holder AND sidecar absence atomically after that lookup, never bulk-delete
    candidate keys. Sidecar keys themselves are never cleanup candidates.
    """
    if not observed_holder or key.endswith(":generation"):
        return False
    return bool(redis.eval(
        _RELEASE_LEGACY_UNIVERSITY, 2, key, key + ":generation", observed_holder,
    ))


async def acquire_global_slot(db, redis, key, job_id, *, now, limit):
    generation, lineage = await _authority(db, job_id)
    return bool(await redis.eval(
        _ACQUIRE_SLOT, 2, key, key + ":generations",
        job_id, generation, lineage, now, limit,
    ))


async def release_global_slot(redis, key, job_id, generation):
    return bool(await redis.eval(
        _RELEASE_SLOT, 2, key, key + ":generations", job_id, generation,
    ))


async def acquire_legacy_global_slot(redis, key, job_id, *, now, stale_before, limit):
    """Sweep only ordinary slots; age is not evidence about fenced owners."""
    return int(await redis.eval(
        _ACQUIRE_LEGACY_SLOT, 2, key, key + ":generations",
        now, stale_before, limit, job_id,
    ))


async def release_legacy_global_slot(redis, key, job_id):
    return bool(await redis.eval(
        _RELEASE_LEGACY_SLOT, 2, key, key + ":generations", job_id,
    ))