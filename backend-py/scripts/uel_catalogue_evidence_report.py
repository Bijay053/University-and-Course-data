#!/usr/bin/env python3
"""Read-only, source-panel audit of an isolated UEL catalogue run.

This deliberately re-reads the captured UEL HTML rather than treating the
production variant parser as evidence.  The variant parser is used only as a
second enumeration whose route identities can be compared with the independent
DOM walk.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg
from bs4 import BeautifulSoup
from bs4.element import Tag


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ACTIVE = ROOT / ".local" / "uel-audit" / "active.json"
DB_PREFIX = "uel_audit_"
SKILLS = ("listening", "speaking", "writing", "reading")


def abort(message: str) -> "NoReturn":
    raise SystemExit(f"SAFETY ABORT: {message}")


def compact_text(node: Tag | None) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")


def source_url(url: str) -> str:
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    elif "://" not in url and url:
        url = "https://" + url.lstrip("/")
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k != "uel_variant"]
    hostname = (parts.hostname or "").casefold()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return urlunsplit((
        parts.scheme.casefold(), hostname,
        parts.path.rstrip("/"), urlencode(sorted(query)), "",
    ))


def route_url(url: str, key: str | None = None) -> str:
    if key is None:
        candidate = url.strip()
        if "://" not in candidate:
            candidate = "https://" + candidate.lstrip("/")
        key = next(
            (value for name, value in parse_qsl(urlsplit(candidate).query)
             if name == "uel_variant"),
            "",
        )
    base = source_url(url)
    if not key:
        return base
    parts = urlsplit(base)
    query = parse_qsl(parts.query, keep_blank_values=True) + [("uel_variant", key)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(sorted(query)), ""))


def route_level(url: str) -> str:
    path = urlsplit(url).path.casefold()
    if path.startswith("/undergraduate/courses/"):
        return "UG"
    if path.startswith("/postgraduate/courses/"):
        return "PG"
    return "unknown"


def route_kind(level: str, label: str) -> str:
    low = label.casefold()
    if level == "UG":
        return "foundation" if "foundation" in low else "standard"
    if "placement" in low:
        return "placement"
    if re.search(r"\bmfa\b", low):
        return "MFA"
    if re.search(r"\bma\b", low):
        return "MA"
    return "standard"


def owned_requirement_panel(
    soup: BeautifulSoup, label: str,
) -> tuple[list[Tag], list[str]]:
    """Resolve only explicit source bindings from a route label to a panel."""
    panels: list[Tag] = []
    errors: list[str] = []
    wanted = slug(re.sub(r"\s*\(including contextual offer\)\s*$", "", label,
                         flags=re.I))

    for button in soup.select("[data-details-screen][data-label]"):
        button_label = re.sub(
            r"\s*\(including contextual offer\)\s*$", "",
            str(button.get("data-label") or ""), flags=re.I,
        )
        if slug(button_label) != wanted:
            continue
        owner = button.find_parent("dialog")
        target_id = str(button.get("data-details-screen") or "")
        screen = owner.find(id=target_id) if owner and target_id else None
        if screen is None:
            errors.append(f"data-details-screen target {target_id!r} is missing")
            continue
        screen_label = re.sub(
            r"\s*\(including contextual offer\)\s*$", "",
            str(screen.get("data-label") or ""), flags=re.I,
        )
        if slug(screen_label) != wanted:
            errors.append(
                f"details screen label {screen_label!r} does not own {label!r}"
            )
            continue
        panels.append(screen)

    prefix = "Full entry requirements for "
    for button in soup.select("[data-modal-id][aria-label]"):
        aria = str(button.get("aria-label") or "")
        if not aria.casefold().startswith(prefix.casefold()):
            continue
        if slug(aria[len(prefix):]) != wanted:
            continue
        target_id = str(button.get("data-modal-id") or "")
        modal = soup.find(id=target_id) if target_id else None
        if modal is None:
            errors.append(f"data-modal-id target {target_id!r} is missing")
            continue
        # A chooser modal is not itself route-owned; its labelled child screen
        # must be selected by the data-details-screen contract above.
        if modal.select("[data-details-screen]"):
            if not panels:
                errors.append("chooser modal has no valid route-owned details screen")
            continue
        panels.append(modal)

    unique: list[Tag] = []
    seen: set[int] = set()
    for panel in panels:
        if id(panel) not in seen:
            seen.add(id(panel))
            unique.append(panel)
    if not unique:
        errors.append(f"no explicitly bound eligibility panel for {label!r}")
    elif len(unique) > 1:
        texts = {compact_text(panel) for panel in unique}
        if len(texts) > 1:
            errors.append(f"multiple differing eligibility panels for {label!r}")
    return unique, errors


def ielts_statements(panels: list[Tag]) -> tuple[dict[str, float], list[str]]:
    """Extract explicit IELTS numbers without using UEL extractor helpers."""
    scores: dict[str, float] = {}
    snippets: list[str] = []
    for panel in panels:
        for node in panel.select("p, li"):
            text = compact_text(node)
            if not re.search(r"\bIELTS\b", text, re.I):
                continue
            snippets.append(text[:500])
            overall_patterns = (
                r"\boverall\s+IELTS\s+([0-9](?:\.[05])?)",
                r"\bIELTS\b.{0,35}\bscore\s+of\s+"
                r"([0-9](?:\.[05])?)\s+overall\b",
                r"\boverall\s+([0-9](?:\.[05])?)\s+for\s+IELTS\b",
                r"\bIELTS(?:\s+Academic)?\s+(?:English\s+)?(?:overall\s+)?"
                r"(?:score\s+)?(?:of\s+)?([0-9](?:\.[05])?)",
            )
            for pattern in overall_patterns:
                match = re.search(pattern, text, re.I)
                if match:
                    scores.setdefault("overall", float(match.group(1)))
                    break
            for match in re.finditer(
                r"(?<![\d.])([0-9](?:\.[05])?)(?![\d.])\s+in\s+"
                r"((?:writing|speaking|listening|reading)\b[^\d]{0,100})",
                text, re.I,
            ):
                for skill in re.findall(
                    r"\b(writing|speaking|listening|reading)\b",
                    match.group(2), re.I,
                ):
                    scores.setdefault(skill.casefold(), float(match.group(1)))
            # Some panels put the named skills before their shared score:
            # "Writing and Speaking 6.0, Listening and Reading 5.5".
            for match in re.finditer(
                r"((?:writing|speaking|listening|reading)\b[^.;\d]{0,70}?)"
                r"(?<![\d.])([0-9](?:\.[05])?)(?![\d.])",
                text, re.I,
            ):
                for skill in re.findall(
                    r"\b(writing|speaking|listening|reading)\b",
                    match.group(1), re.I,
                ):
                    scores.setdefault(skill.casefold(), float(match.group(2)))
            # Research and professional templates also use "6.5 for writing".
            for match in re.finditer(
                r"(?<![\d.])([0-9](?:\.[05])?)(?![\d.])\s+for\s+"
                r"((?:writing|speaking|listening|reading)\b[^\d]{0,100})",
                text, re.I,
            ):
                for skill in re.findall(
                    r"\b(writing|speaking|listening|reading)\b",
                    match.group(2), re.I,
                ):
                    scores.setdefault(skill.casefold(), float(match.group(1)))
            each = re.search(
                r"(?:minimum(?:\s+score)?\s+(?:of\s+)?|no\s+less\s+than\s+|"
                r"no\s+(?:component|element|skill|band)\s+(?:score\s+)?less\s+than\s+)"
                r"([0-9](?:\.[05])?).{0,35}\b(?:each|every|all)\s+"
                r"(?:component|element|skill|band)", text, re.I,
            )
            if not each:
                each = re.search(
                    r"no\s+(?:component|element|skill|band)\s+(?:score\s+)?"
                    r"(?:is\s+)?less\s+than\s+([0-9](?:\.[05])?)",
                    text, re.I,
                )
            if each:
                for skill in SKILLS:
                    scores.setdefault(skill, float(each.group(1)))
    return scores, list(dict.fromkeys(snippets))


def independent_routes(html: str, url: str) -> tuple[list[dict], list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    level = route_level(url)
    errors: list[str] = []
    groups: dict[str, dict] = {}
    option_tabs = soup.select(".course-options-content-div")
    for tab_number, tab in enumerate(option_tabs, 1):
        for details in tab.select(".course-option-details"):
            header = details.find_previous_sibling()
            heading = header.select_one(".degree-type") if isinstance(header, Tag) else None
            label = compact_text(heading)
            if not label:
                errors.append(
                    f"tab {tab_number}: course option has no preceding degree-type label"
                )
                continue
            key = slug(label)
            rows = details.select(
                ".course-option-details-item-wrapper, "
                ".course-option-details__list-item"
            )
            if not rows:
                errors.append(f"route {label!r} has no recognized option rows")
                continue
            group = groups.setdefault(key, {
                "label": label, "audiences": [], "attendances": [],
            })
            for row in rows:
                audience = compact_text(row.select_one(".application-type"))
                attendance = compact_text(row.select_one(".attendance-type"))
                group["audiences"].append(audience)
                group["attendances"].append(attendance)
                if not re.search(r"\b(?:international|home) applicant\b",
                                 audience, re.I):
                    errors.append(
                        f"route {label!r} has malformed applicant eligibility "
                        f"{audience!r}"
                    )
                if not re.search(r"\b(?:full|part)[- ]?time\b", attendance, re.I):
                    errors.append(
                        f"route {label!r} has malformed attendance {attendance!r}"
                    )

    if not groups:
        # Single-award templates are represented by the source page identity.
        title = compact_text(soup.select_one("h1"))
        label = title or urlsplit(url).path.rsplit("/", 1)[-1]
        groups[""] = {"label": label, "audiences": [], "attendances": []}
        errors.append("no labelled course-option route groups; audited as base route")

    is_multi = len(groups) > 1
    routes: list[dict] = []
    for key, group in groups.items():
        label = group["label"]
        panels, panel_errors = owned_requirement_panel(soup, label)
        bands, snippets = ielts_statements(panels)
        audiences = group["audiences"]
        international = any(re.search(r"\binternational applicant\b", item, re.I)
                            for item in audiences)
        home = any(re.search(r"\bhome applicant\b", item, re.I)
                   for item in audiences)
        routes.append({
            "route_id": route_url(url, key if is_multi else ""),
            "source_url": source_url(url),
            "level": level,
            "label": label,
            "route_kind": route_kind(level, label),
            "international_eligible": international,
            "home_eligible": home,
            "home_only": home and not international,
            "audience_examples": list(dict.fromkeys(audiences))[:6],
            "ielts": bands,
            "ielts_snippets": snippets[:4],
            "eligibility_template_errors": panel_errors,
        })
    return routes, errors


def safe_audit_metadata(
    active_path: Path, allow_partial: bool, database_override: str | None,
) -> tuple[dict, Path, str]:
    active_path = active_path.resolve()
    audit_root = (ROOT / ".local" / "uel-audit").resolve()
    if active_path.parent != audit_root:
        abort("active metadata must be directly under .local/uel-audit")
    active = json.loads(active_path.read_text(encoding="utf-8"))
    audit_dir = (ROOT / str(active.get("audit_dir", ""))).resolve()
    if audit_dir.parent != audit_root or not audit_dir.name.startswith(
            str(active.get("audit_run_id", ""))):
        abort("active audit directory failed local containment/run-id guard")
    evidence_file = audit_dir / "provisioning-evidence.json"
    if evidence_file.exists():
        evidence = json.loads(evidence_file.read_text(encoding="utf-8"))
        db_name = str(evidence.get("disposable_database", {}).get("name", ""))
        if database_override and database_override != db_name:
            abort("database override disagrees with completed provisioning metadata")
    elif allow_partial and database_override:
        # During a run the parent provisioner cannot write its final evidence
        # file yet. The explicit name is still subjected to prefix and live
        # current_database()/server-address checks below.
        db_name = database_override
    else:
        abort("isolated database metadata is not available yet")
    if not db_name.startswith(DB_PREFIX):
        abort("disposable database does not have the uel_audit_ prefix")
    manifest = audit_dir / "html-manifest.json"
    if not manifest.exists() and not allow_partial:
        abort("run is not complete: html-manifest.json is not available")
    return active, audit_dir, db_name


async def isolated_connection(db_name: str):
    configured = os.environ.get("DATABASE_URL", "")
    parts = urlsplit(configured.replace("postgresql+asyncpg://", "postgresql://", 1))
    if parts.scheme not in {"postgres", "postgresql"}:
        abort("DATABASE_URL is not PostgreSQL")
    if not parts.hostname:
        abort("DATABASE_URL has no PostgreSQL host")
    netloc = ""
    if parts.username:
        netloc += parts.username
        if parts.password is not None:
            netloc += ":" + parts.password
        netloc += "@"
    host = f"[{parts.hostname}]" if ":" in (parts.hostname or "") else parts.hostname
    netloc += host or ""
    if parts.port:
        netloc += f":{parts.port}"
    target = urlunsplit(("postgresql", netloc, "/" + db_name, "", ""))
    conn = await asyncpg.connect(
        target, server_settings={"default_transaction_read_only": "on"},
    )
    identity = await conn.fetchrow(
        "SELECT current_database(), "
        "coalesce(inet_server_addr()::text, 'local-socket') AS server"
    )
    if identity["current_database"] != db_name or identity["server"] not in {
        "127.0.0.1", "::1", "local-socket",
    }:
        conn.close()
        abort("connected database identity/local-server guard failed")
    return conn


async def load_database(
    conn, run_id: str | None,
) -> tuple[str, list[dict], list[dict], dict]:
    if run_id:
        job = await conn.fetchrow(
            "SELECT runtime_job_id,status,total_found,current,imported,skipped,"
            "errors,error_message FROM scrape_runtime_jobs "
            "WHERE runtime_job_id=$1", run_id,
        )
    else:
        job = await conn.fetchrow(
            "SELECT runtime_job_id,status,total_found,current,imported,skipped,"
            "errors,error_message FROM scrape_runtime_jobs "
            "WHERE runtime_job_id LIKE 'uel_audit_%' "
            "ORDER BY created_at DESC LIMIT 1"
        )
    if not job:
        abort("no isolated UEL audit job exists in the disposable database")
    job_id = job["runtime_job_id"]
    staged = [
        dict(row) for row in await conn.fetch(
            "SELECT id,course_name,canonical_course_url,degree_level,"
            "international_eligible,eligibility_status,eligibility_reason,"
            "ielts_overall,ielts_listening,ielts_speaking,ielts_writing,"
            "ielts_reading,status,scrape_warnings FROM scraped_courses "
            "WHERE scrape_job_id=$1 ORDER BY id", job_id,
        )
    ]
    logs = [
        dict(row) for row in await conn.fetch(
            "SELECT sequence,event,payload FROM scrape_runtime_logs "
            "WHERE runtime_job_id=$1 ORDER BY sequence", job_id,
        )
    ]
    return job_id, staged, logs, dict(job)


def compact_log_evidence(logs: list[dict]) -> dict:
    counts = Counter()
    drop_examples: list[dict] = []
    for row in logs:
        payload = row.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {"unparsed_payload": payload}
        kind = str(payload.get("kind") or row.get("event") or "unknown")
        counts[kind] += 1
        haystack = json.dumps(payload, default=str)
        if re.search(r"drop|skip|reject|error|fail|malform|eligib", haystack, re.I):
            drop_examples.append({
                "sequence": row.get("sequence"),
                "event": row.get("event"),
                "kind": kind,
                "evidence": haystack[:800],
            })
    return {
        "event_kind_counts": dict(sorted(counts.items())),
        "drop_error_examples": drop_examples[:100],
        "drop_error_event_count": len(drop_examples),
    }


def runtime_route_outcomes(logs: list[dict]) -> dict[str, list[dict]]:
    outcomes: dict[str, list[dict]] = defaultdict(list)
    for row in logs:
        payload = row.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                continue
        kind = str(payload.get("kind") or "")
        url = str(payload.get("url") or "")
        if kind not in {"skipped", "extract_error"} or not url:
            continue
        outcomes[route_url(url)].append({
            "kind": kind,
            "reason": payload.get("reason"),
            "detail": payload.get("detail") or payload.get("error"),
            "sequence": row.get("sequence"),
            "logged_url": url,
        })
    return outcomes


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--active", type=Path, default=DEFAULT_ACTIVE)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--partial", action="store_true",
                        help="allow partial DB report without final HTML manifest")
    parser.add_argument(
        "--database",
        help="explicit uel_audit_ database name while provisioning is still running",
    )
    args = parser.parse_args()

    active, audit_dir, db_name = safe_audit_metadata(
        args.active, args.partial, args.database,
    )
    progress_file = audit_dir / "progress.json"
    progress = json.loads(progress_file.read_text()) if progress_file.exists() else {}
    expected_job = str(progress.get("job_id") or "") or None
    conn = await isolated_connection(db_name)
    try:
        job_id, staged, logs, job = await load_database(conn, expected_job)
    finally:
        await conn.close()

    manifest_file = audit_dir / "html-manifest.json"
    capture_errors: list[str] = []
    manifest = json.loads(manifest_file.read_text()) if manifest_file.exists() else []
    if not manifest and args.partial:
        # The runner writes the manifest atomically at the end. For an emerging
        # report, recover URL ownership from each document's canonical metadata.
        for path in sorted((audit_dir / "html").glob("*.html")):
            raw = path.read_bytes()
            soup = BeautifulSoup(raw, "html.parser")
            canonical = soup.select_one('link[rel~="canonical"][href]')
            og_url = soup.select_one('meta[property="og:url"][content]')
            url = (
                str(canonical.get("href") or "") if canonical else
                str(og_url.get("content") or "") if og_url else ""
            )
            if not url:
                capture_errors.append(
                    f"{path.name}: no canonical or og:url for partial URL ownership"
                )
                continue
            manifest.append({
                "url": url,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "file": str(path.relative_to(audit_dir)),
            })
    documents: dict[str, dict] = {}
    html_root = (audit_dir / "html").resolve()
    for item in manifest:
        input_url = source_url(str(item.get("url", "")))
        path = (audit_dir / str(item.get("file", ""))).resolve()
        if path.parent != html_root:
            abort("manifest HTML path escaped the run's html directory")
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != item.get("sha256"):
            abort(f"captured HTML digest mismatch for {path.name}")
        captured_soup = BeautifulSoup(raw, "html.parser")
        canonical_node = captured_soup.select_one('link[rel~="canonical"][href]')
        canonical_url = source_url(
            str(canonical_node.get("href") or "") if canonical_node else input_url
        )
        if canonical_url in documents:
            documents[canonical_url]["input_urls"].append(input_url)
            documents[canonical_url]["captures"].append({
                "path": path, "sha256": digest,
            })
            if documents[canonical_url]["sha256"] != digest:
                capture_errors.append(
                    f"{canonical_url}: multiple differing captured documents"
                )
            continue
        documents[canonical_url] = {
            "path": path,
            "sha256": digest,
            "input_urls": [input_url],
            "captures": [{"path": path, "sha256": digest}],
        }

    routes: list[dict] = []
    template_errors: list[dict] = []
    parser_disagreements: list[dict] = []
    for url, document in sorted(documents.items()):
        html = document["path"].read_text(encoding="utf-8")
        page_routes, page_errors = independent_routes(html, url)
        routes.extend(page_routes)
        template_errors.extend({"source_url": url, "error": error}
                               for error in page_errors)
        try:
            # Import only after all database guards have run. This parser is a
            # comparator, never the owner of source labels/panels or IELTS.
            from app.services.scraper.extractors.uel_variants import parse_uel_variants
            parsed = parse_uel_variants(html, url)
            parser_ids = sorted(route_url(item.url.split("?", 1)[0], item.key)
                                for item in parsed)
            independent_ids = sorted(item["route_id"] for item in page_routes)
            if parsed and parser_ids != independent_ids:
                parser_disagreements.append({
                    "source_url": url,
                    "independent_route_ids": independent_ids,
                    "parser_route_ids": parser_ids,
                })
        except Exception as exc:
            parser_disagreements.append({
                "source_url": url, "parser_error": f"{type(exc).__name__}: {exc}",
                "independent_route_ids": [item["route_id"] for item in page_routes],
            })

    route_by_id = {item["route_id"]: item for item in routes}
    duplicate_source_ids = [
        route_id for route_id, count in Counter(item["route_id"] for item in routes).items()
        if count > 1
    ]
    staged_by_id: dict[str, list[dict]] = defaultdict(list)
    for row in staged:
        staged_by_id[route_url(str(row.get("canonical_course_url") or ""))].append(row)

    source_missing_staged = sorted(set(route_by_id) - set(staged_by_id))
    staged_missing_source = sorted(set(staged_by_id) - set(route_by_id))
    duplicate_staged_ids = sorted(
        route_id for route_id, rows in staged_by_id.items() if len(rows) > 1
    )
    comparisons: list[dict] = []
    for identity in sorted(set(route_by_id) & set(staged_by_id)):
        source = route_by_id[identity]
        for row in staged_by_id[identity]:
            differences = {}
            source_scores = source["ielts"]
            for field in ("overall", *SKILLS):
                db_value = row.get(f"ielts_{field}")
                source_value = source_scores.get(field)
                if ((db_value is None) != (source_value is None) or
                        (db_value is not None and source_value is not None and
                         float(db_value) != float(source_value))):
                    differences[f"ielts_{field}"] = {
                        "source": source_value, "staged": db_value,
                    }
            if (
                row.get("international_eligible") is None
                or bool(row["international_eligible"])
                != bool(source["international_eligible"])
            ):
                differences["international_eligible"] = {
                    "source": source["international_eligible"],
                    "staged": row["international_eligible"],
                }
            comparisons.append({
                "route_id": identity,
                "course_name": row["course_name"],
                "source_label": source["label"],
                "differences": differences,
                "source_ielts_snippets": source["ielts_snippets"] if differences else [],
            })

    logged_outcomes = runtime_route_outcomes(logs)
    captured_inputs = {
        item for document in documents.values() for item in document["input_urls"]
    }
    pipeline_sources = {
        source_url(str(row.get("canonical_course_url") or ""))
        for row in staged if row.get("canonical_course_url")
    }
    for row in logs:
        payload = row.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                continue
        if payload.get("url"):
            pipeline_sources.add(source_url(str(payload["url"])))
    drop_explanations: list[dict] = []
    unexplained_drops: list[str] = []
    for identity in source_missing_staged:
        matches = logged_outcomes.get(identity, [])
        matched_by = "exact_route"
        if not matches:
            matches = logged_outcomes.get(source_url(identity), [])
            matched_by = "source_page"
        if matches:
            drop_explanations.append({
                "route_id": identity,
                "matched_by": matched_by,
                "runtime_outcomes": matches,
            })
        else:
            unexplained_drops.append(identity)

    kind_counts = Counter(
        f"{item['level']}:{item['route_kind']}" for item in routes
    )
    home_only = [item["route_id"] for item in routes if item["home_only"]]
    band_examples: dict[str, list[dict]] = defaultdict(list)
    for item in routes:
        signature = json.dumps(item["ielts"], sort_keys=True)
        if item["ielts"] and len(band_examples[signature]) < 4:
            band_examples[signature].append({
                "route_id": item["route_id"],
                "label": item["label"],
                "snippets": item["ielts_snippets"],
            })

    report = {
        "audit_run_id": active.get("audit_run_id"),
        "audit_dir": str(audit_dir.relative_to(ROOT)),
        "database_guard": {
            "database": db_name, "required_prefix": DB_PREFIX,
            "local_only": True, "read_only": True,
        },
        "job_id": job_id,
        "job": job,
        "final_manifest_present": manifest_file.exists(),
        "source_pages": {
            "UG": sorted(url for url in documents if route_level(url) == "UG"),
            "PG": sorted(url for url in documents if route_level(url) == "PG"),
            "unknown": sorted(url for url in documents if route_level(url) == "unknown"),
        },
        "source_capture_aliases": [
            {
                "canonical_url": canonical,
                "input_urls": document["input_urls"],
                "sha256": [item["sha256"] for item in document["captures"]],
            }
            for canonical, document in sorted(documents.items())
            if len(document["input_urls"]) > 1
        ],
        "pipeline_source_urls_without_capture": sorted(
            pipeline_sources - captured_inputs
        ),
        "counts": {
            "captured_source_pages": len(documents),
            "source_routes": len(routes),
            "staged_rows": len(staged),
            "source_route_kinds": dict(sorted(kind_counts.items())),
            "home_only_routes": len(home_only),
            "source_missing_staged": len(source_missing_staged),
            "staged_missing_source": len(staged_missing_source),
            "ielts_differing_rows": sum(
                any(key.startswith("ielts_") for key in item["differences"])
                for item in comparisons
            ),
            "eligibility_differing_rows": sum(
                "international_eligible" in item["differences"]
                for item in comparisons
            ),
        },
        "home_only_route_ids": home_only,
        "routes": routes,
        "route_identity_audit": {
            "identity_basis": "normalized canonical URL plus uel_variant; never name",
            "duplicate_source_route_ids": duplicate_source_ids,
            "duplicate_staged_route_ids": duplicate_staged_ids,
            "source_missing_staged": source_missing_staged,
            "staged_missing_source": staged_missing_source,
            "parser_enumeration_disagreements": parser_disagreements,
        },
        "eligibility_template_errors": template_errors + [
            {"source_url": item["source_url"], "error": error}
            for item in routes for error in item["eligibility_template_errors"]
        ],
        "staged_comparisons": comparisons,
        "ielts_band_source_examples": dict(band_examples),
        "capture_errors": capture_errors,
        "runtime_drop_evidence": compact_log_evidence(logs),
        "drop_explanations": drop_explanations,
        "unexplained_drops": {
            "source_route_ids_without_staged_row": unexplained_drops,
            "note": (
                "Review runtime_drop_evidence for an explicit matching event. "
                "Any route left here is not explained by route identity."
            ),
        },
    }
    output = args.output or (audit_dir / "uel-catalogue-evidence-report.json")
    output = output.resolve()
    if output.parent != audit_dir:
        abort("report output must remain inside the active audit directory")
    output.write_text(json.dumps(report, default=str, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    print(json.dumps({
        "report": str(output.relative_to(ROOT)),
        "job_status": job.get("status"),
        "source_pages": len(documents),
        "source_routes": len(routes),
        "staged_rows": len(staged),
        "source_missing_staged": len(source_missing_staged),
        "staged_missing_source": len(staged_missing_source),
        "ielts_differing_rows": report["counts"]["ielts_differing_rows"],
        "eligibility_template_errors": len(report["eligibility_template_errors"]),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))