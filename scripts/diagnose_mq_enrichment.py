"""Read-only MQ discovery diagnostic; saves public source data under /tmp."""
import asyncio
import hashlib
import json
import sys
from pathlib import Path

from app.services.scraper import http_fetcher as fetcher
from app.services.scraper import mq_browser_discover as mq
from app.services.scraper.config import set_uni_config
from app.services.scraper.config.loader import load_uni_config


async def main():
    out = Path("/tmp/mq-enrichment-diagnostic")
    out.mkdir(exist_ok=True)
    set_uni_config(load_uni_config(
        slug="mq", scrape_url="https://www.mq.edu.au/",
        name="Macquarie University",
    ))
    original_fetch = fetcher.fetch_html_scrape_do
    original_build = mq._build_scrapy_result
    captured = (
        {row["url"]: row for row in json.loads((out / "fetches.json").read_text())}
        if "--reuse-captured" in sys.argv else {}
    )
    outcomes = []
    rows = []

    async def traced_fetch(url, **kwargs):
        if url in captured:
            row = captured[url]
            fetcher._last_fetch_failure.set(
                {"kind": row.get("failure_kind"), "status_code": row.get("status")}
                if not row.get("file") else None
            )
            return (out / row["file"]).read_text() if row.get("file") else None
        body = await original_fetch(url, **kwargs)
        record = {"url": url, "bytes": len(body or "")}
        if body:
            key = hashlib.sha256(url.encode()).hexdigest()[:20]
            (out / f"{key}.txt").write_text(body)
            record["file"] = f"{key}.txt"
            if "page-data.json" in url:
                program = mq._extract_program_from_page_data(body)
                record["program_found"] = bool(program)
        else:
            failure = fetcher.get_last_fetch_failure() or {}
            record["failure_kind"] = failure.get("kind")
            record["status"] = failure.get("status_code")
        outcomes.append(record)
        filename = "public-probes.json" if captured else "fetches.json"
        (out / filename).write_text(json.dumps(outcomes, indent=2))
        return body

    def traced_build(name, url, meta, program):
        result = original_build(name, url, meta, program)
        rows.append({
            "name": name, "url": url, "program_found": bool(program),
            "fee": result["payload"].get("international_fee"),
            "fees": program.get("fees"),
        })
        (out / "fees.json").write_text(json.dumps(rows, indent=2))
        return result

    async def emit(message):
        print(message, flush=True)

    fetcher.fetch_html_scrape_do = traced_fetch
    mq._build_scrapy_result = traced_build
    try:
        await asyncio.wait_for(mq._discover_from_funnelback_api(emit), 900)
    except (mq.MqEnrichmentCoverageError, asyncio.TimeoutError) as exc:
        print(f"DIAGNOSTIC_STOP {type(exc).__name__}: {exc}", flush=True)
    finally:
        fetcher.fetch_html_scrape_do = original_fetch
        mq._build_scrapy_result = original_build
    print(f"DIAGNOSTIC_SAVED {out}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())