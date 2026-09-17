"""Report redundant generated overlays without blocking healthy releases."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import TextIO

try:
    from .reconcile_generated_configs import find_redundant_generated_overlays
except ImportError:  # Direct execution: python deploy/post_checkout_overlay_audit.py
    from reconcile_generated_configs import find_redundant_generated_overlays


CORRUPTION_EXIT = 42
_CORRUPTION_MARKERS = (
    re.compile(r"\bmissing (?:blob|tree|commit|object)\b", re.I),
    re.compile(r"\bbroken link from\b", re.I),
    re.compile(r"\binvalid sha1 pointer\b", re.I),
    re.compile(r"\bfatal:\s+bad object\b", re.I),
    re.compile(r"\bobject corrupt or missing\b", re.I),
    re.compile(r"\bhash mismatch\b", re.I),
)


def _safe_emit(stream: TextIO, message: str) -> bool:
    try:
        print(message, file=stream)
        stream.flush()
        return True
    except (BrokenPipeError, OSError):
        try:
            descriptor = stream.fileno()
            replacement = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(replacement, descriptor)
            finally:
                os.close(replacement)
        except (AttributeError, OSError):
            pass
        return False


def _valid_overlay_path(value: object) -> str:
    if not isinstance(value, str) or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValueError("invalid redundant-overlay audit path")
    path = PurePosixPath(value)
    expected_root = PurePosixPath("backend-py/scraper_config/runtime_unis")
    if path.is_absolute() or ".." in path.parts or path.parent != expected_root:
        raise ValueError("invalid redundant-overlay audit path")
    return value


def repository_corruption_confirmed(repo_root: Path) -> bool:
    """Return true only when git-fsck output contains known corruption evidence."""
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={repo_root}",
                "fsck",
                "--connectivity-only",
                "--no-dangling",
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode == 0:
        return False
    output = f"{result.stdout}\n{result.stderr}"
    return any(pattern.search(output) for pattern in _CORRUPTION_MARKERS)


def _failure_result(repo_root: Path, warning: str) -> int:
    _safe_emit(sys.stderr, warning)
    return CORRUPTION_EXIT if repository_corruption_confirmed(repo_root) else 0


def _write_evidence(
    evidence_path: Path | None,
    *,
    status: str,
    overlays: list[str] | None = None,
) -> bool:
    """Write only the sanitized audit state consumed by release evidence."""
    if evidence_path is None:
        return True
    payload: dict[str, object] = {"status": status}
    if status == "ok":
        safe_overlays = overlays or []
        payload["redundant_overlay_count"] = len(safe_overlays)
        payload["redundant_overlay_paths"] = safe_overlays
    try:
        evidence_path.write_text(
            json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        evidence_path.chmod(0o600)
    except OSError:
        return False
    return True


def _failure_result_with_evidence(
    repo_root: Path,
    warning: str,
    evidence_path: Path | None,
) -> int:
    _write_evidence(evidence_path, status="warning")
    return _failure_result(repo_root, warning)


def run_post_checkout_audit(
    repo_root: Path,
    *,
    evidence_path: Path | None = None,
) -> int:
    """Emit one atomic summary, containing every ordinary failure."""
    repo_root = repo_root.resolve()
    try:
        proofs = find_redundant_generated_overlays(repo_root)
        overlays = [_valid_overlay_path(proof.get("overlay")) for proof in proofs]
        lines = [f"REDUNDANT_CONFIG_OVERLAY_COUNT={len(overlays)}"]
        lines.extend(
            f"REDUNDANT_CONFIG_OVERLAY_PATH={overlay}" for overlay in overlays
        )
    except Exception:  # noqa: BLE001 - release audit must fail open unless corrupt
        return _failure_result_with_evidence(
            repo_root,
            "REDUNDANT_CONFIG_OVERLAY_AUDIT_WARNING=failed_non_blocking",
            evidence_path,
        )

    if repository_corruption_confirmed(repo_root):
        _write_evidence(evidence_path, status="warning")
        _safe_emit(
            sys.stderr,
            "REDUNDANT_CONFIG_OVERLAY_AUDIT_WARNING="
            "repository_corruption_confirmed",
        )
        return CORRUPTION_EXIT
    if not _write_evidence(evidence_path, status="ok", overlays=overlays):
        return _failure_result(
            repo_root,
            "REDUNDANT_CONFIG_OVERLAY_AUDIT_WARNING=evidence_failed_non_blocking",
        )
    if not _safe_emit(sys.stdout, "\n".join(lines)):
        return _failure_result_with_evidence(
            repo_root,
            "REDUNDANT_CONFIG_OVERLAY_AUDIT_WARNING=report_failed_non_blocking",
            evidence_path,
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--evidence-path", type=Path)
    args = parser.parse_args()
    return run_post_checkout_audit(
        args.repo_root,
        evidence_path=args.evidence_path,
    )


if __name__ == "__main__":
    raise SystemExit(main())