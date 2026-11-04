"""Recover a truthful predecessor baseline; never publish or create acceptance."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from uuid import uuid4

try:
    from . import release_manifest
except ImportError:
    import release_manifest


class RecoveryRefused(RuntimeError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def regular_bytes(path: Path) -> bytes:
    for item in (path, *path.parents):
        if item.is_symlink():
            raise RecoveryRefused("symlink in recovery input")
    if not stat.S_ISREG(path.stat().st_mode):
        raise RecoveryRefused("recovery input is not a regular file")
    with path.open("rb") as stream:
        data = stream.read(release_manifest.MAX_RECEIPT_BYTES + 1)
    if len(data) > release_manifest.MAX_RECEIPT_BYTES:
        raise RecoveryRefused("recovery input exceeds size limit")
    return data


def evidence_snapshot(manifest: Path) -> dict[str, str]:
    result = {}
    total = 0
    for parent, directories, files in os.walk(manifest.parent, followlinks=False):
        if any((Path(parent) / name).is_symlink() for name in directories):
            raise RecoveryRefused("symlink in original evidence")
        for name in files:
            path = Path(parent) / name
            data = regular_bytes(path)
            total += len(data)
            if len(result) >= 32 or total > 32 * 1024 * 1024:
                raise RecoveryRefused("unexpected original evidence size")
            result[str(path.relative_to(manifest.parent))] = digest(data)
    if "manifest.json" not in result or manifest.name != "manifest.json":
        raise RecoveryRefused("original release manifest is unavailable")
    return result


def sync_directory(path: Path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class RecoveryPlan:
    predecessor: str
    target: str
    failed_manifest: Path

    def validate(self):
        if (
            any(not re.fullmatch(r"[a-f0-9]{40}", sha)
                for sha in (self.predecessor, self.target))
            or self.predecessor == self.target
        ):
            raise RecoveryRefused("two distinct exact revisions are required")
        regular_bytes(self.failed_manifest)


class Recovery:
    """All host writes are explicit; fixtures supply a disposable host adapter."""

    def __init__(self, host, plan: RecoveryPlan):
        self.host, self.plan = host, plan
        plan.validate()

    def check(self):
        self.host.fence(self.plan, heads={self.plan.target})
        self.host.verify_frontend(self.plan.predecessor)
        before = evidence_snapshot(self.plan.failed_manifest)
        release_manifest.verify_pre_restart_failure(
            self.plan.failed_manifest, self.plan.predecessor, self.plan.target,
        )
        if evidence_snapshot(self.plan.failed_manifest) != before:
            raise RecoveryRefused("original evidence changed during inspection")
        recipes = self.host.recipes(self.plan)
        workers = self.host.observe(self.plan.predecessor, paused=False)
        self.host.checkout_safe(self.plan.predecessor)
        return {
            "schema_version": 1, "kind": "partial_release_recovery",
            "recovery_id": uuid4().hex, "repo_root": str(self.host.repo),
            "predecessor": self.plan.predecessor, "target": self.plan.target,
            "failed_manifest": str(self.plan.failed_manifest),
            "evidence": before, "metadata_sha256": self.host.metadata_hash(),
            "recipes": recipes, "workers": sorted(workers),
            "phase": "captured", "status": "captured",
            "pause_attempted": False, "release_completed": False,
        }

    def verify_inputs(self, record, *, final=False):
        if (
            record.get("kind") != "partial_release_recovery"
            or record.get("schema_version") != 1
            or record.get("repo_root") != str(self.host.repo)
            or record.get("predecessor") != self.plan.predecessor
            or record.get("target") != self.plan.target
            or record.get("failed_manifest") != str(self.plan.failed_manifest)
            or not re.fullmatch(r"[a-f0-9]{32}", record.get("recovery_id", ""))
            or record.get("release_completed") is not False
            or not isinstance(record.get("recipes"), dict)
            or any(
                not re.fullmatch(r"backend-py/scraper_config/unis/[^/]+\.yaml", name)
                or not re.fullmatch(r"[a-f0-9]{64}", value.get("sha256", ""))
                or not isinstance(value.get("mode"), int)
                or not 0 <= value["mode"] <= 0o777
                for name, value in record.get("recipes", {}).items()
            )
            or not isinstance(record.get("workers"), list)
            or not record["workers"]
            or len(set(record["workers"])) != len(record["workers"])
            or any(not re.fullmatch(r"[A-Za-z0-9_.@:-]{1,255}", worker)
                   for worker in record["workers"])
            or (record.get("recipe_attempt") is not None and not
                re.fullmatch(r"[a-f0-9]{32}", record["recipe_attempt"]))
        ):
            raise RecoveryRefused("recovery state does not match this operation")
        self.host.fence(
            self.plan, heads={self.plan.predecessor} if final
            else {self.plan.predecessor, self.plan.target},
        )
        if self.host.metadata_hash() != record["metadata_sha256"]:
            raise RecoveryRefused("release metadata changed; refusing recovery")
        if evidence_snapshot(self.plan.failed_manifest) != record["evidence"]:
            raise RecoveryRefused("original acceptance evidence changed")
        release_manifest.verify_pre_restart_failure(
            self.plan.failed_manifest, self.plan.predecessor, self.plan.target,
        )

    def save(self, path, record, phase, **updates):
        record.update(phase=phase, **updates)
        release_manifest.write_manifest(path, record)

    def start(self, directory: Path):
        record = self.check()
        if any(item.is_symlink() for item in (directory, *directory.parents)):
            raise RecoveryRefused("symlink in recovery state directory")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink():
            raise RecoveryRefused("recovery directory must not be a symlink")
        root = directory / record["recovery_id"]
        root.mkdir(mode=0o700)  # A new recovery never adopts an earlier state.
        state = root / "state.json"
        self.save(state, record, "captured")
        print(f"RECOVERY_STATE={state}", flush=True)
        self.ensure_archive(state, record)
        self.host.bootstrap(record)
        return self._run(state, record)

    def verify_archive(self, state, record):
        if evidence_snapshot(state.parent / "original-evidence/manifest.json") != record["evidence"]:
            raise RecoveryRefused("original-evidence archive is incomplete or changed")

    def ensure_archive(self, state, record):
        self.verify_inputs(record)
        # Retain a private archive, never a new ordinary-smoke receipt directory.
        archive = state.parent / "original-evidence"
        staging = state.parent / "evidence-staging"
        for directory in (archive, staging):
            if any(item.is_symlink() for item in (directory, *directory.parents)):
                raise RecoveryRefused("symlink in evidence archive")
            directory.mkdir(mode=0o700, exist_ok=True)
        sync_directory(state.parent)
        for name, expected in record["evidence"].items():
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise RecoveryRefused("invalid evidence archive path")
            data = regular_bytes(self.plan.failed_manifest.parent / name)
            if digest(data) != expected:
                raise RecoveryRefused("evidence changed before archival")
            path = archive / name
            if any(item.is_symlink() for item in (path, *path.parents)):
                raise RecoveryRefused("symlink in evidence archive")
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not path.exists():
                # Copy outside the archive, fsync, then publish without replacing
                # anything. A kill during the copy leaves only an ignored staging
                # file, never a partial published receipt or accepted checkpoint.
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(dir=staging, delete=False) as stream:
                        temporary = Path(stream.name)
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    try:
                        os.link(temporary, path, follow_symlinks=False)
                    except FileExistsError:
                        pass  # The existing immutable copy must match below.
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
            if digest(regular_bytes(path)) != expected:
                raise RecoveryRefused("archived evidence changed; refusing overwrite")
            # Persist directory entries, including nested preflight directories.
            parent = path.parent
            while parent != state.parent:
                sync_directory(parent)
                parent = parent.parent
        self.verify_archive(state, record)
        self.verify_inputs(record)
        self.save(state, record,
                  "archive_ready" if record["phase"] == "captured" else record["phase"],
                  archive_complete=True)

    def resume(self, state: Path):
        record = json.loads(regular_bytes(state))
        self.verify_inputs(record)
        self.host.verify_frontend(self.plan.predecessor)
        if record.get("status") not in {
            "captured", "recovering", "failed_paused", "baseline_restored",
        }:
            raise RecoveryRefused("recovery requires explicit operator intervention")
        self.ensure_archive(state, record)
        if record["status"] == "baseline_restored":
            self.verify_inputs(record, final=True)
            self.host.verify_recipes(record)
            self.host.observe(self.plan.predecessor, paused=False,
                              workers=record["workers"])
            return state
        self.host.bootstrap(record)
        return self._run(state, record)

    def _run(self, state, record):
        # A resumed attempt may encounter newly queued work or changed workers.
        # Refuse read-only failures before owning any consumer change this time.
        self.verify_inputs(record)
        self.verify_archive(state, record)
        if record.get("archive_complete") is not True:
            raise RecoveryRefused("evidence archive is not durably complete")
        self.host.observe(self.plan.predecessor, paused=None,
                          workers=record["workers"])
        try:
            self.save(state, record, "pausing", status="recovering",
                      pause_attempted=True)
            self.host.pause(record["workers"])
            self.host.observe(self.plan.predecessor, paused=True,
                              workers=record["workers"])
            self.host.verify_frontend(self.plan.predecessor)
            self.save(state, record, "paused")
            self.verify_inputs(record)
            if self.host.recipe_manifest_exists(record):
                self.host.restore_recipes(record)
            self.host.verify_recipes(record)
            if self.host.head() == self.plan.target:
                self.host.checkout_safe(self.plan.predecessor)
                self.save(state, record, "preparing_recipes",
                          recipe_attempt=uuid4().hex)
                self.host.prepare_recipes(record)
                self.host.observe(self.plan.predecessor, paused=True,
                                  workers=record["workers"])
                self.verify_inputs(record)
                self.save(state, record, "checking_out")
                self.host.checkout(self.plan.predecessor)
                self.save(state, record, "restoring_recipes")
                self.host.restore_recipes(record)
            self.verify_inputs(record, final=True)
            self.host.verify_recipes(record)
            self.host.observe(self.plan.predecessor, paused=True,
                              workers=record["workers"])
            self.host.verify_frontend(self.plan.predecessor)
            self.save(state, record, "resuming")
            self.host.resume_consumers(record["workers"])
            self.host.observe(self.plan.predecessor, paused=False,
                              workers=record["workers"])
            self.host.verify_frontend(self.plan.predecessor)
            self.verify_inputs(record, final=True)
            self.host.verify_recipes(record)
            self.verify_archive(state, record)
            self.save(state, record, "baseline_restored",
                      status="baseline_restored")
            return state
        except BaseException:
            # Never restore a consumer over a mismatched or uncertain baseline.
            # A later explicit resume revalidates all inputs from retained state.
            if record.get("pause_attempted"):
                try:
                    if self.host.recipe_manifest_exists(record):
                        self.host.restore_recipes(record)
                    self.host.verify_recipes(record)
                except BaseException:
                    # Never overwrite a concurrent edit to make cleanup pass.
                    # The recipe journal and its byte-exact backups remain.
                    pass
                try:
                    self.host.pause(record["workers"])
                    self.host.observe(self.plan.predecessor, paused=True,
                                      workers=record["workers"])
                    status = "failed_paused"
                except BaseException:
                    self.save(state, record, record["phase"],
                              status="containment_failed")
                    self.host.stop_worker()
                    status = "worker_stopped"
                self.save(state, record, record["phase"], status=status)
            raise
