"""Lossless observation of HTML returned by real scraper transports.

This module deliberately does not choose a transport, retry, validate, or alter
results.  The isolated UEL runner uses it around the existing transport entry
points so fallback HTML is retained without changing scraper behaviour.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit


def is_uel_detail_source(url: str) -> bool:
    """Match only real, canonical UEL UG/PG course detail URLs."""
    parts = urlsplit(url)
    if (parts.hostname or "").lower() != "www.uel.ac.uk":
        return False
    return bool(
        re.fullmatch(
            r"/(?:undergraduate|postgraduate)/courses/[^/]+",
            parts.path.rstrip("/").lower(),
        )
    )


class SourceCapture:
    def __init__(
        self,
        audit_dir: Path,
        on_record: Callable[[], None] | None = None,
        is_detail_source: Callable[[str], bool] | None = None,
    ):
        self.audit_dir = audit_dir
        self.html_dir = audit_dir / "html"
        self.html_dir.mkdir(exist_ok=True)
        self.manifest_path = audit_dir / "html-manifest.json"
        self.source_manifest_path = audit_dir / "source-html-manifest.json"
        self.attempts_path = audit_dir / "html-fetch-attempts.json"
        self.on_record = on_record
        self.is_detail_source = is_detail_source or (lambda _url: True)
        self.manifest: list[dict[str, Any]] = []
        self.source_manifest: list[dict[str, Any]] = []
        self.attempts: list[dict[str, Any]] = []
        self.flush()

    def flush(self) -> None:
        """Persist emerging evidence after every attempt, including failures."""
        self._write_json(self.manifest_path, self.manifest)
        self._write_json(self.source_manifest_path, self.source_manifest)
        self._write_json(self.attempts_path, self.attempts)

    @staticmethod
    def _write_json(path: Path, value: Any) -> None:
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.replace(path)

    async def observe(
        self,
        transport: str,
        fetch: Callable[..., Awaitable[Any]],
        url: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Call ``fetch`` unchanged and record any HTML it returns.

        Non-string sentinels and ``None`` retain identity. Exceptions are
        recorded by type and re-raised as the original exception instance.
        """
        started = time.monotonic()
        try:
            result = await fetch(url, *args, **kwargs)
        except BaseException as exc:
            self.attempts.append(
                {
                    "url": url,
                    "transport": transport,
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
            )
            self._recorded()
            raise

        attempt: dict[str, Any] = {
            "url": url,
            "transport": transport,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        if isinstance(result, str):
            raw = result.encode("utf-8")
            digest = hashlib.sha256(raw).hexdigest()
            path = self.html_dir / f"{digest}.html"
            if not path.exists():
                path.write_bytes(raw)
            item = {
                **attempt,
                "sha256": digest,
                "bytes": len(raw),
                "file": str(path.relative_to(self.audit_dir)),
                "source_kind": (
                    "course_detail" if self.is_detail_source(url) else "supporting"
                ),
            }
            self.source_manifest.append(item)
            if item["source_kind"] == "course_detail":
                # Backward-compatible reporter input: it has historically
                # interpreted every row as a course detail document.
                self.manifest.append(item)
            attempt.update(
                status="html",
                sha256=digest,
                bytes=len(raw),
                file=item["file"],
            )
        else:
            attempt.update(status="no_html", result_type=type(result).__name__)
        self.attempts.append(attempt)
        self._recorded()
        return result

    def _recorded(self) -> None:
        self.flush()
        if self.on_record is not None:
            self.on_record()

    def wrap(
        self, transport: str, fetch: Callable[..., Awaitable[Any]]
    ) -> Callable[..., Awaitable[Any]]:
        async def observed(url: str, *args: Any, **kwargs: Any) -> Any:
            return await self.observe(transport, fetch, url, *args, **kwargs)

        return observed

    @property
    def errors(self) -> list[dict[str, Any]]:
        return [item for item in self.attempts if item["status"] == "error"]

    @property
    def no_html(self) -> list[dict[str, Any]]:
        return [item for item in self.attempts if item["status"] == "no_html"]