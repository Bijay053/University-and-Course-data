"""Scrapy spider bridge — runs a Scrapy spider in a subprocess.

Scrapy uses the Twisted event loop which is incompatible with the
asyncio/Celery stack in this codebase.  Running the spider in a separate
process sidesteps the conflict entirely: the subprocess owns its own event
loop, writes items to a temp JSON lines file via Scrapy's built-in feed
exporter, then exits.  The bridge reads the file and converts each item to
the standard link dict that the orchestrator's staging loop expects.

Supported item shapes
---------------------
Discovery-only (minimum — feeds normal per-course extraction):

    {"name": "Course Name", "url": "https://uni.edu/courses/xyz"}

Rich / pre-extracted (bypasses per-course HTTP fetch + extraction):

    {
        "name": "Course Name",
        "url": "https://...",
        "payload": {<scraped_courses columns>},
        "evidence": [{<field evidence rows>}]
    }

The orchestrator's ``_extract_only`` returns the prebuilt
``scrapy_result`` verbatim when it finds one — identical to how the
SearchStax provider works.

Spider location
---------------
All spiders live under ``backend-py/spiders/<name>.py``.
``ScrapyConfig.spider`` is the filename without ``.py``.
Copy ``backend-py/spiders/template_spider.py`` to get started.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.scraper.config.schema import ScrapyConfig

log = logging.getLogger("scraper.scrapy_bridge")

# Spiders directory — sits at backend-py/spiders/ (two levels above this file's
# package root at backend-py/app/services/scraper/).
_SPIDERS_DIR = Path(__file__).parent.parent.parent.parent / "spiders"


async def run_scrapy_spider(cfg: "ScrapyConfig", emit=None) -> list[dict]:
    """Run the named Scrapy spider in a subprocess; return link dicts.

    Each returned dict is ``{"name": str, "url": str}`` for discovery-only
    items, or additionally carries ``"scrapy_result"`` for rich items so the
    orchestrator can bypass per-course extraction.

    Falls back to an empty list on timeout or spider failure (the caller
    decides whether to continue with BFS/sitemap or abort the job).
    """
    spider_path = _SPIDERS_DIR / f"{cfg.spider}.py"
    if not spider_path.exists():
        raise FileNotFoundError(
            f"Scrapy spider not found: {spider_path}. "
            f"Create it from backend-py/spiders/template_spider.py."
        )

    async def _emit(msg: str) -> None:
        if emit:
            try:
                await emit("status", msg, phase="discover")
            except Exception:  # noqa: BLE001
                pass

    await _emit(f"[SCRAPY] Starting spider: {cfg.spider}")
    log.info("[SCRAPY] spider=%s timeout=%ds", cfg.spider, cfg.timeout_seconds)

    # Scrapy writes items here; we read it after the process exits.
    with tempfile.NamedTemporaryFile(
        suffix=".jsonl", delete=False, mode="w", prefix="scrapy_out_"
    ) as tf:
        out_file = tf.name

    try:
        cmd: list[str] = [
            sys.executable, "-m", "scrapy", "runspider",
            str(spider_path),
            "-o", f"{out_file}:jsonlines",
            # Silence Scrapy's own logs so they don't pollute Celery output.
            "-s", "LOG_ENABLED=False",
            "-s", "TELNETCONSOLE_ENABLED=False",
            "-s", "ROBOTSTXT_OBEY=False",
        ]
        for key, val in cfg.settings.items():
            cmd.extend(["-s", f"{key}={val}"])

        log.info("[SCRAPY] cmd: %s", " ".join(cmd))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
            cwd=str(_SPIDERS_DIR.parent),  # backend-py/ as cwd
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=float(cfg.timeout_seconds),
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            log.error(
                "[SCRAPY] spider=%s killed after %ds timeout",
                cfg.spider, cfg.timeout_seconds,
            )
            await _emit(f"[SCRAPY] Spider timed out after {cfg.timeout_seconds}s.")
            return []

        if proc.returncode not in (0, None):
            stderr_text = (stderr or b"").decode(errors="replace")[:800]
            log.warning(
                "[SCRAPY] spider=%s exited %d: %s",
                cfg.spider, proc.returncode, stderr_text,
            )

        # ── Parse output file ────────────────────────────────────────────────
        links: list[dict] = []
        try:
            raw = Path(out_file).read_text(encoding="utf-8")
        except FileNotFoundError:
            log.warning("[SCRAPY] Output file missing — spider produced no items.")
            raw = ""

        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue

            url = item.get("url") or item.get("course_url") or ""
            name = item.get("name") or item.get("course_name") or url
            if not url:
                continue

            link: dict = {"name": name, "url": url}

            # Rich mode: spider pre-extracted payload + evidence rows.
            if "payload" in item:
                link["scrapy_result"] = {
                    "name": name,
                    "url": url,
                    "payload": item["payload"],
                    "evidence": item.get("evidence", []),
                }

            links.append(link)

        if cfg.max_courses:
            links = links[: cfg.max_courses]

        await _emit(f"[SCRAPY] Spider returned {len(links)} course link(s).")
        log.info(
            "[SCRAPY] spider=%s fetched=%d (max_courses=%s)",
            cfg.spider, len(links), cfg.max_courses,
        )
        return links

    finally:
        try:
            os.unlink(out_file)
        except OSError:
            pass
