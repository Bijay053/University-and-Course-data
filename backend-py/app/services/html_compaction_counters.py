"""Per-run telemetry for conservative course-page HTML compaction."""
from __future__ import annotations

from contextvars import ContextVar


_stats: ContextVar[dict | None] = ContextVar("html_compaction_stats", default=None)


def reset_html_compaction_stats() -> None:
    _stats.set(
        {
            "attempts": 0,
            "accepted": 0,
            "fail_open": 0,
            "skipped_small": 0,
            "input_bytes": 0,
            "output_bytes": 0,
            "elapsed_ms": 0.0,
            "fail_open_reasons": {},
        }
    )


def note_html_compaction(
    *,
    outcome: str,
    input_bytes: int,
    output_bytes: int,
    elapsed_ms: float,
    reason: str | None = None,
) -> None:
    stats = _stats.get(None)
    if stats is None:
        return
    if outcome == "skipped_small":
        stats["skipped_small"] += 1
        return
    stats["attempts"] += 1
    stats["input_bytes"] += max(0, int(input_bytes))
    stats["output_bytes"] += max(0, int(output_bytes))
    stats["elapsed_ms"] += max(0.0, float(elapsed_ms))
    if outcome == "accepted":
        stats["accepted"] += 1
    else:
        stats["fail_open"] += 1
        key = reason or "unknown"
        reasons = stats["fail_open_reasons"]
        reasons[key] = reasons.get(key, 0) + 1


def get_html_compaction_stats() -> dict:
    stats = _stats.get(None)
    if stats is None:
        return {}
    result = dict(stats)
    result["fail_open_reasons"] = dict(stats["fail_open_reasons"])
    attempts = result["attempts"]
    accepted = result["accepted"]
    input_bytes = result["input_bytes"]
    output_bytes = result["output_bytes"]
    result["acceptance_rate"] = round(accepted / attempts, 4) if attempts else 0.0
    result["reduction_rate"] = (
        round(1.0 - output_bytes / input_bytes, 4) if input_bytes else 0.0
    )
    result["elapsed_ms"] = round(result["elapsed_ms"], 1)
    return result