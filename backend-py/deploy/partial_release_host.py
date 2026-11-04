"""Production adapter for operator-controlled partial-release recovery."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import sys

try:
    from . import verify_frontend_release as frontend
    from .partial_release_recovery import RecoveryRefused, digest, regular_bytes
except ImportError:
    import verify_frontend_release as frontend
    from partial_release_recovery import RecoveryRefused, digest, regular_bytes


class RecoveryHost:
    def __init__(self, repo: Path, *, owner_command=(), python=None,
                 expected_origin=None, account=None):
        self.repo = repo.absolute()
        self.owner_command = list(owner_command)
        self.python = python or sys.executable
        self.expected_origin, self.account = expected_origin, account
        self.metadata = self.repo / "backend-py/.release.env"
        self._smoke = None

    def run_owner(self, *args, input=None):
        env = dict(os.environ, GIT_NO_REPLACE_OBJECTS="1",
                   GIT_NO_LAZY_FETCH="1", GIT_ALLOW_PROTOCOL="")
        return subprocess.check_output(
            [*self.owner_command, "env", "GIT_NO_REPLACE_OBJECTS=1",
             "GIT_NO_LAZY_FETCH=1", "GIT_ALLOW_PROTOCOL=", *map(str, args)], input=input,
            cwd=self.repo, env=env, stderr=subprocess.DEVNULL, timeout=60,
        )

    def git(self, *args):
        return self.run_owner("git", "-C", self.repo, *args).decode().strip()

    def head(self):
        return self.git("rev-parse", "HEAD")

    def changes(self):
        # --exit-code forces content comparison. Name-only/name-status alone
        # can report stat-cache differences after byte-identical atomic restores.
        try:
            output = self.run_owner(
                "git", "-C", self.repo, "diff", "--exit-code",
                "--name-status", "--no-renames",
            )
        except subprocess.CalledProcessError as exc:
            if exc.returncode != 1:
                raise
            output = exc.output
        return output.decode().strip().splitlines()

    def metadata_hash(self):
        return digest(regular_bytes(self.metadata))

    def verify_frontend(self, predecessor):
        # Publication precedes metadata/service restart in the guarded release.
        # Never infer frontend identity from those still-predecessor identities.
        dist = self.repo / "artifacts/university-portal/dist/public"
        try:
            regular_bytes(dist / "index.html")
            frontend.verify_public_build(
                dist, "https://portal.agentsic.com/",
                f"UNIVERSITY_PORTAL_RELEASE:{predecessor}".encode(),
            )
        except (AssertionError, OSError, ValueError) as exc:
            raise RecoveryRefused(
                "published frontend does not prove the predecessor; "
                "explicit operator frontend recovery is required"
            ) from exc

    def fence(self, plan, *, heads):
        if not (self.repo / ".git").is_dir() or (self.repo / ".git").is_symlink():
            raise RecoveryRefused("a regular standalone production Git checkout is required")
        if self.head() not in heads:
            raise RecoveryRefused("checkout revision changed")
        for revision in (plan.predecessor, plan.target):
            self.git("cat-file", "-e", revision + "^{commit}")
        self.git("merge-base", "--is-ancestor", plan.predecessor, plan.target)
        if self.git("diff", "--cached", "--name-only"):
            raise RecoveryRefused("staged changes block recovery")
        if self.expected_origin and self.git("remote", "get-url", "origin") != self.expected_origin:
            raise RecoveryRefused("production origin does not match")
        # Do not fetch or execute a moving branch as recovery policy.

    def recipes(self, plan):
        result = {}
        for line in self.changes():
            status, _, name = line.partition("\t")
            if status != "M" or not re.fullmatch(
                r"backend-py/scraper_config/unis/[^/]+\.yaml", name,
            ):
                raise RecoveryRefused("unknown tracked changes block recovery")
            path = self.repo / name
            data = regular_bytes(path)
            for sha in (plan.predecessor, plan.target):
                entry = self.git("ls-tree", sha, "--", name)
                if not entry.startswith("100644 blob "):
                    raise RecoveryRefused("recipe is not regular in both revisions")
            result[name] = {"sha256": digest(data),
                            "mode": path.stat().st_mode & 0o777}
        return result

    def checkout_safe(self, predecessor):
        old_paths = set(self.git("ls-tree", "-r", "--name-only", predecessor).splitlines())
        now_paths = set(self.git("ls-tree", "-r", "--name-only", "HEAD").splitlines())
        for name in old_paths - now_paths:
            path = self.repo / name
            if path.exists() or path.is_symlink():
                raise RecoveryRefused("untracked or ignored path would collide")

    def workdir(self, record):
        return self.repo / ".git/partial-release-recovery" / record["recovery_id"]

    def recipe_path(self, record):
        attempt = record.get("recipe_attempt")
        return self.workdir(record) / (
            record["recovery_id"] + ("." + attempt if attempt else "") + ".recipes.json"
        )

    def bootstrap(self, record):
        source = self.run_owner(
            "git", "show",
            record["target"] + ":backend-py/deploy/reconcile_generated_configs.py",
        )
        directory = self.workdir(record)
        # Only this narrowly scoped new directory is owned by the repo owner.
        code = (
            "import os,pathlib,sys; p=pathlib.Path(sys.argv[1]); "
            "assert not p.parent.is_symlink() and not p.is_symlink(); "
            "p.mkdir(parents=True,exist_ok=True,mode=0o700); "
            "f=p/'reconciler.py'; b=sys.stdin.buffer.read(); "
            "assert not f.is_symlink(); "
            "assert not f.exists() or f.read_bytes()==b; "
            "f.write_bytes(b) if not f.exists() else None; f.chmod(0o600)"
        )
        self.run_owner(self.python, "-B", "-c", code, directory, input=source)

    def recipe_manifest_exists(self, record):
        return self.recipe_path(record).exists()

    def recipe_command(self, record, action, *args):
        source = self.run_owner(
            "git", "show",
            record["target"] + ":backend-py/deploy/reconcile_generated_configs.py",
        )
        helper = self.workdir(record) / "reconciler.py"
        if regular_bytes(helper) != source:
            raise RecoveryRefused("pinned reconciliation helper changed")
        self.run_owner(self.python, "-B", helper, action, *args,
                       "--manifest", self.recipe_path(record))

    def prepare_recipes(self, record):
        self.verify_recipes(record)
        if self.recipe_manifest_exists(record):
            raise RecoveryRefused("refusing to overwrite an earlier recipe journal")
        self.recipe_command(record, "prepare-tracked", "--repo-root", self.repo,
                            "--target", record["predecessor"])

    def restore_recipes(self, record):
        path = self.recipe_path(record)
        manifest = json.loads(regular_bytes(path))
        if (
            manifest.get("repo_root") != str(self.repo)
            or manifest.get("original_head") != record["target"]
            or manifest.get("target") != record["predecessor"]
            or {e["path"] for e in manifest.get("entries", [])} != set(record["recipes"])
            or len(manifest["entries"]) != len(record["recipes"])
        ):
            raise RecoveryRefused("recipe journal does not match captured inputs")
        for entry in manifest["entries"]:
            name = entry["path"]
            expected = record["recipes"][name]
            base = f".git/release-tracked-config-backups/{path.name}/{name}"
            if (
                entry["operator_sha256"] != expected["sha256"]
                or digest(bytes.fromhex(entry["operator_content_hex"])) != expected["sha256"]
                or entry["mode"] != expected["mode"]
                or entry["backup"] != base
                or entry["quarantine"] != base + ".worktree"
                or bytes.fromhex(entry["head_content_hex"]) != self.run_owner(
                    "git", "show", record["target"] + ":" + name)
                or entry["target_sha256"] != digest(self.run_owner(
                    "git", "show", record["predecessor"] + ":" + name))
            ):
                raise RecoveryRefused("recipe journal integrity failed")
        self.recipe_command(record, "restore-tracked")
        self.verify_recipes(record)

    def verify_recipes(self, record):
        for name, expected in record["recipes"].items():
            path = self.repo / name
            if (digest(regular_bytes(path)) != expected["sha256"]
                    or path.stat().st_mode & 0o777 != expected["mode"]):
                raise RecoveryRefused("operator recipe changed; refusing overwrite")
        changes = self.changes()
        if any(line.split("\t", 1)[0] != "M" or line.split("\t", 1)[-1]
               not in record["recipes"] for line in changes):
            raise RecoveryRefused("unrelated tracked changes appeared")

    def checkout(self, predecessor):
        if self.changes():
            raise RecoveryRefused("checkout still has tracked changes")
        self.checkout_safe(predecessor)
        self.git("checkout", "--no-overwrite-ignore", "--detach", predecessor)

    def smoke(self):
        if self._smoke is None:
            sys.path.insert(0, str(self.repo / "backend-py"))
            from deploy import safe_restart_smoke
            self._smoke = safe_restart_smoke
        return self._smoke

    def observe(self, predecessor, *, paused, workers=None):
        smoke = self.smoke()
        for unit in ("uni-api-py", "uni-celery"):
            smoke.verify_service_process_release_identity(unit, predecessor)
        if os.environ.get("RELEASE_REVISION") != predecessor:
            raise RecoveryRefused("loaded release metadata does not match predecessor")
        lines = [line.strip() for line in regular_bytes(self.metadata).decode().splitlines()
                 if line.strip() and not line.lstrip().startswith("#")]
        if lines != ["RELEASE_REVISION=" + predecessor]:
            raise RecoveryRefused("stored release metadata does not match predecessor")
        smoke.validate_database_rehearsal_requirement(
            Path(os.environ.get("DATABASE_REFRESH_REHEARSAL_PROOF",
                                str(smoke.DEFAULT_REHEARSAL_PROOF))),
            expected_account_id=self.account, max_age_hours=24,
        )
        async def idle():
            try:
                observation = await smoke._wait_for_worker_idle(30)
                smoke.validate_idle_counts(await smoke._active_counts())
                return observation
            finally:
                await smoke.engine.dispose()
        observation = asyncio.run(idle())
        complete = observation.get("complete_worker_replies") if isinstance(observation, dict) else None
        ping = complete.get("ping") if isinstance(complete, dict) else None
        if not isinstance(ping, dict) or not ping:
            raise RecoveryRefused("complete ping worker membership is unavailable")
        observed_workers = set(ping)
        if (any(not isinstance(complete.get(state), dict)
                or set(complete[state]) != observed_workers
                for state in ("active", "reserved", "scheduled"))
                or (workers is not None and observed_workers != set(workers))):
            raise RecoveryRefused("incomplete or changed worker membership")
        from app.tasks.celery_app import celery_app
        queues = celery_app.control.inspect(timeout=10).active_queues()
        if not isinstance(queues, dict) or set(queues) != observed_workers:
            raise RecoveryRefused("incomplete or changed worker membership")
        if any(not isinstance(qs, list)
               or any(not isinstance(q, dict) or not isinstance(q.get("name"), str)
                      or not q["name"] for q in qs) for qs in queues.values()):
            raise RecoveryRefused("malformed worker queue inspection")
        if paused is not None and any(
            any(q["name"] == "scrape" for q in qs) == paused
            for qs in queues.values()
        ):
            raise RecoveryRefused("unexpected scrape consumer state")
        return sorted(queues)

    def consumer(self, workers, *, enable):
        from app.tasks.celery_app import celery_app
        method = (celery_app.control.add_consumer if enable
                  else celery_app.control.cancel_consumer)
        replies = method("scrape", destination=workers, reply=True, timeout=10)
        observed = {}
        for reply in replies or []:
            if (not isinstance(reply, dict) or len(reply) != 1
                    or set(reply) & set(observed)):
                raise RecoveryRefused("consumer acknowledgements are ambiguous")
            observed.update(reply)
        if set(observed) != set(workers) or any(
            not isinstance(value, dict) or "error" in value
            or not isinstance(value.get("ok"), str) or not value["ok"]
            for value in observed.values()
        ):
            raise RecoveryRefused("consumer acknowledgements are incomplete")

    def pause(self, workers):
        self.consumer(workers, enable=False)

    def resume_consumers(self, workers):
        self.consumer(workers, enable=True)

    def stop_worker(self):
        subprocess.run(["systemctl", "stop", "uni-celery.service"], check=True, timeout=60)
        result = subprocess.run(["systemctl", "is-active", "--quiet", "uni-celery.service"])
        if result.returncode != 3:
            raise RecoveryRefused("could not verify worker stopped")
