#!/usr/bin/env python3
"""Provision and run the UEL audit in a schema-only disposable local database."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parents[2]
AUDIT_ROOT = ROOT / ".local" / "uel-audit"
DB_PREFIX = "uel_audit_"
RUNTIME_PATHS = (
    "backend-py/app", "backend-py/scraper_config",
    "backend-py/requirements.txt", "backend-py/pyproject.toml",
)


def fail(message: str) -> None:
    raise SystemExit(f"SAFETY ABORT: {message}")


def pg_env(parts, database: str) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("DATABASE_URL", None)
    env["PGHOST"] = parts.hostname or ""
    env["PGPORT"] = str(parts.port or 5432)
    env["PGUSER"] = unquote(parts.username or "")
    env["PGDATABASE"] = database
    if parts.password is not None:
        env["PGPASSWORD"] = unquote(parts.password)
    return env


def run(command: list[str], *, env: dict[str, str], input_file=None, output_file=None) -> None:
    subprocess.run(
        command,
        env=env,
        stdin=input_file,
        stdout=output_file,
        stderr=subprocess.PIPE,
        check=True,
        text=False,
    )


def scalar(sql: str, env: dict[str, str]) -> str:
    result = subprocess.run(
        ["psql", "-XAt", "-v", "ON_ERROR_STOP=1", "-c", sql],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def scrub_log(raw: bytes, secrets_to_remove: list[str]) -> str:
    text = raw.decode("utf-8", "replace")
    configured_secrets = [
        value for key, value in os.environ.items()
        if re.search(r"TOKEN|SECRET|PASSWORD|API_KEY|ACCESS_KEY", key)
        and len(value) >= 8
    ]
    for value in [*secrets_to_remove, *configured_secrets]:
        if value:
            text = text.replace(value, "[REDACTED]")
    # Do not retain accidental serialized request configuration.
    kept = []
    for line in text.splitlines():
        lowered = line.lower()
        if "request_payload" in lowered or "request config" in lowered:
            kept.append("[REDACTED request configuration log line]")
        else:
            kept.append(line)
    return "\n".join(kept) + "\n"


def verify_runtime_release(expected: str, root: Path = ROOT) -> str:
    """Permit later audit/docs commits only when released runtime bytes match."""
    if not re.fullmatch(r"[0-9a-f]{40}", expected):
        fail("expected release must be an exact full Git commit SHA")
    kind = subprocess.check_output(
        ["git", "cat-file", "-t", expected], cwd=root, text=True,
    ).strip()
    if kind != "commit":
        fail("expected release does not identify a commit")
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True,
    ).strip()
    if subprocess.run(
        ["git", "diff", "--quiet", expected, "--", *RUNTIME_PATHS], cwd=root,
    ).returncode:
        fail("runtime source/config/dependencies differ from the authorized release")
    if subprocess.check_output(
        ["git", "ls-files", "--others", "--exclude-standard", "--", *RUNTIME_PATHS],
        cwd=root, text=True,
    ).strip():
        fail("untracked runtime files prevent proving released-source equivalence")
    return revision


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-release", required=True,
                        help="full SHA independently verified on deployed API and worker")
    args = parser.parse_args()
    revision = verify_runtime_release(args.expected_release)
    raw_url = os.environ.get("DATABASE_URL", "")
    if not raw_url:
        fail("DATABASE_URL is not configured")
    parts = urlsplit(raw_url.replace("postgresql+asyncpg://", "postgresql://", 1))
    if parts.scheme not in {"postgres", "postgresql"}:
        fail("configured database is not PostgreSQL")
    source_db = unquote(parts.path.lstrip("/"))
    if not source_db or source_db in {"postgres", "template0", "template1"}:
        fail("refusing unsafe source database name")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{stamp}_{secrets.token_hex(3)}"
    target_db = f"{DB_PREFIX}{stamp.lower().replace('t', '_').replace('z', '')}_{secrets.token_hex(3)}"
    if target_db == source_db or not target_db.startswith(DB_PREFIX):
        fail("disposable target identity check failed")

    audit_dir = AUDIT_ROOT / run_id
    audit_dir.mkdir(parents=True, exist_ok=False)
    (AUDIT_ROOT / "active.json").write_text(
        json.dumps(
            {
                "audit_run_id": run_id,
                "audit_dir": str(audit_dir.relative_to(ROOT)),
                "state": "provisioning",
                "authorized_revision": args.expected_release,
                "harness_revision": revision,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    schema_file = audit_dir / "schema.sql"
    source_env = pg_env(parts, source_db)
    admin_env = pg_env(parts, "postgres")
    target_env = pg_env(parts, target_db)

    source_identity = scalar(
        "BEGIN READ ONLY; SELECT current_database() || '|' || "
        "coalesce(inet_server_addr()::text,'local-socket') || '|' || "
        "coalesce(inet_server_port()::text,'0'); COMMIT;",
        source_env,
    ).splitlines()[-2]
    source_identity_parts = source_identity.split("|")
    if (
        len(source_identity_parts) != 3
        or source_identity_parts[1] not in {"127.0.0.1", "::1", "local-socket"}
    ):
        fail("configured PostgreSQL server did not prove a local server address")
    # Unlike the application fail-open limiter, the audit must flag a missing
    # prerequisite before spending time/credits on a supposedly representative run.
    import redis
    redis_client = redis.Redis.from_url(
        "redis://127.0.0.1:6379/15", socket_connect_timeout=2, socket_timeout=2,
    )
    try:
        redis_client.ping()
    except redis.RedisError:
        fail("isolated Redis DB 15 is unavailable; start Redis before running")
    finally:
        redis_client.close()
    source_review_before = scalar(
        "BEGIN READ ONLY; SELECT count(*) FROM scraped_courses; COMMIT;", source_env
    ).splitlines()[-2]

    created = False
    try:
        run(["createdb", target_db], env=admin_env)
        created = True
        with schema_file.open("wb") as out:
            run(["pg_dump", "--schema-only", "--no-owner", "--no-privileges"], env=source_env, output_file=out)
        # pg_dump 17 emits this session setting even for a PostgreSQL 16 source.
        # It is not a schema object and older servers reject it.
        schema_file.write_text(schema_file.read_text().replace("SET transaction_timeout = 0;\n", ""))
        with schema_file.open("rb") as src:
            run(["psql", "-X", "-v", "ON_ERROR_STOP=1"], env=target_env, input_file=src)

        target_identity = scalar(
            "SELECT current_database() || '|' || "
            "coalesce(inet_server_addr()::text,'local-socket') || '|' || "
            "coalesce(inet_server_port()::text,'0');",
            target_env,
        )
        if not target_identity.startswith(target_db + "|"):
            fail("new process database identity check failed")
        if target_identity == source_identity:
            fail("source and disposable database identities match")

        clean_files = [
            "backend-py/app/services/scraper/orchestrator.py",
            "backend-py/app/services/scraper/uel_transport.py",
            "backend-py/app/services/scraper/extractors/uel_variants.py",
            "backend-py/scraper_config/unis/uel.yaml",
        ]
        dirty = subprocess.run(
            ["git", "diff", "--quiet", "--", *clean_files], cwd=ROOT
        ).returncode
        if dirty:
            fail("released UEL source/config files have uncommitted differences")
        hashes = {
            path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
            for path in clean_files
        }

        target_url = urlunsplit(
            (
                "postgresql",
                (
                    quote(unquote(parts.username or ""), safe="")
                    + ((":" + quote(unquote(parts.password), safe="")) if parts.password is not None else "")
                    + "@"
                    + (f"[{parts.hostname}]" if ":" in (parts.hostname or "") else (parts.hostname or ""))
                    + (f":{parts.port}" if parts.port else "")
                ),
                "/" + target_db,
                "",
                "",
            )
        )
        child_env = os.environ.copy()
        child_env.update(
            {
                "DATABASE_URL": target_url,
                "DATABASE_REQUIRE_TLS": "false",
                "REDIS_URL": "redis://127.0.0.1:6379/15",
                "MAX_CONCURRENT_SCRAPES": "0",
                "PER_UNI_TIMEOUT_SECONDS": "3500",
                "SCRAPE_TASK_SOFT_TIME_LIMIT_S": "3540",
                "SCRAPE_TASK_HARD_TIME_LIMIT_S": "3570",
                "UEL_AUDIT_DIR": str(audit_dir),
                "UEL_AUDIT_EXPECTED_DB": target_db,
                "UEL_AUDIT_RUN_ID": run_id,
            }
        )
        child_env["PYTHONPATH"] = "."
        command = [sys.executable, "-u", "-B", "scripts/uel_isolated_catalogue_runner.py"]
        process = subprocess.Popen(
            command,
            cwd=ROOT / "backend-py",
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            raw_log, _ = process.communicate(timeout=3600)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGINT)
            try:
                raw_log, _ = process.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                raw_log, _ = process.communicate()
            process.returncode = 124

        (audit_dir / "run.log").write_text(
            scrub_log(raw_log, [raw_url, target_url, unquote(parts.password or "")]),
            encoding="utf-8",
        )
        source_review_after = scalar(
            "BEGIN READ ONLY; SELECT count(*) FROM scraped_courses; COMMIT;", source_env
        ).splitlines()[-2]
        evidence = {
            "audit_run_id": run_id,
            "started_utc": stamp,
            "process_exit_code": process.returncode,
            "timed_out_at_60_minutes": process.returncode == 124,
            "source_database": {
                "name": source_db,
                "review_rows_before": int(source_review_before),
                "review_rows_after": int(source_review_after),
                "review_rows_preserved": source_review_before == source_review_after,
                "access": "schema-only pg_dump plus read-only count queries",
            },
            "disposable_database": {"name": target_db, "retained_for_local_audit": True},
            "isolation": {
                "schema_only": True,
                "redis_url": "redis://127.0.0.1:6379/15",
                "scheduled_tasks_started": False,
                "autonomous_task_dispatch": "blocked in runner",
                "approvals_allowed": False,
            },
            "release": {
                "git_revision": args.expected_release,
                "harness_revision": revision,
                "runtime_paths_verified": list(RUNTIME_PATHS),
                "sha256": hashes,
            },
        }
        (audit_dir / "provisioning-evidence.json").write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(audit_dir)
        return process.returncode
    except subprocess.CalledProcessError as exc:
        if exc.stderr:
            print(scrub_log(exc.stderr, [raw_url, unquote(parts.password or "")]), file=sys.stderr)
        fail(f"local PostgreSQL command failed ({exc.cmd[0]}, exit {exc.returncode})")
    finally:
        # Retain a successfully provisioned audit DB. A failed pre-run clone has
        # no evidence value and is removed immediately.
        if created and not (audit_dir / "run-summary.json").exists():
            subprocess.run(["dropdb", "--if-exists", target_db], env=admin_env)


if __name__ == "__main__":
    raise SystemExit(main())