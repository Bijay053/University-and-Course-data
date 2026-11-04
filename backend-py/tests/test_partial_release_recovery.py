"""Real disposable Git transitions; fake services only. Never production or AWS."""
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import signal
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest

from deploy import partial_release_recovery as recovery
from deploy import release_manifest as manifests
from deploy.partial_release_host import RecoveryHost
from deploy.recover_partial_release import release_lock


DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
SMOKE = Path(__file__).parent / "helpers/release_smoke_fixture.py"


class FixtureHost(RecoveryHost):
    def __init__(self, repo, predecessor, controls):
        super().__init__(repo)
        self.predecessor, self.controls = predecessor, controls
        self.fail_resume = False
        self.fail_containment = False
        self.busy = False
        self.service_revision = predecessor
        self.events = []
        self.after_prepare = None

    def control(self):
        return json.loads(self.controls.read_text())

    def update(self, **updates):
        record = self.control()
        record.update(updates)
        self.controls.write_text(json.dumps(record))

    def verify_frontend(self, predecessor):
        self.events.append("frontend")
        state = self.control()
        if any(state[key] != predecessor for key in ("frontend_local", "frontend_public")):
            raise recovery.RecoveryRefused("frontend requires explicit operator recovery")

    def observe(self, predecessor, *, paused, workers=None):
        self.events.append("observe")
        state = self.control()
        if (self.busy or not state["running"]
                or self.service_revision != predecessor
                or self.metadata.read_text() != f"RELEASE_REVISION={predecessor}\n"):
            raise recovery.RecoveryRefused("fixture runtime is not idle predecessor")
        if workers is not None and set(state["workers"]) != set(workers):
            raise recovery.RecoveryRefused("fixture worker membership changed")
        if paused is not None and state["enabled"] == paused:
            raise recovery.RecoveryRefused("fixture consumer state mismatch")
        return state["workers"]

    def pause(self, workers):
        self.events.append("pause")
        if self.fail_containment and self.control().get("resume_attempted"):
            raise recovery.RecoveryRefused("fixture cannot confirm cancellation")
        self.update(enabled=False)

    def resume_consumers(self, workers):
        self.events.append("resume")
        self.update(enabled=True, resume_attempted=True)
        if self.fail_resume:
            raise recovery.RecoveryRefused("fixture partial resume acknowledgement")

    def stop_worker(self):
        self.events.append("stop")
        self.update(enabled=False, running=False)

    def prepare_recipes(self, record):
        super().prepare_recipes(record)
        if self.after_prepare:
            self.after_prepare()


@pytest.fixture
def installation(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(repo), *args], text=True, stderr=subprocess.PIPE,
        ).strip()
    git("init", "-b", "main")
    git("config", "user.name", "Recovery fixture")
    git("config", "user.email", "recovery@example.invalid")
    (repo / ".gitignore").write_text("backend-py/.release.env\nretired.txt\n")
    helper = repo / "backend-py/deploy/reconcile_generated_configs.py"
    helper.parent.mkdir(parents=True)
    helper.write_bytes((DEPLOY / "reconcile_generated_configs.py").read_bytes())
    names = ["canterbury_1759", "hope_2221", "law_1902", "londonmet", "mdx", "port_2174"]
    files = []
    for name in names:
        path = repo / f"backend-py/scraper_config/unis/{name}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("extraction:\n  label: predecessor\n")
        files.append(path)
    (repo / "retired.txt").write_text("predecessor-only file\n")
    git("add", "-f", "retired.txt")
    git("add", ".")
    git("commit", "-m", "predecessor")
    predecessor = git("rev-parse", "HEAD")
    git("rm", "retired.txt")
    for path in files:
        path.write_text("extraction:\n  label: target\n")
    (repo / "target-only.py").write_text("# new source, not yet running\n")
    git("add", ".")
    git("commit", "-m", "target")
    target = git("rev-parse", "HEAD")
    originals = {path: f"extraction:\n  label: operator-{i}\n".encode()
                 for i, path in enumerate(files)}
    for path, data in originals.items():
        path.write_bytes(data)
    metadata = repo / "backend-py/.release.env"
    metadata.write_text(f"RELEASE_REVISION={predecessor}\n")
    manifest = manifests.initialize(tmp_path / "release-manifests", predecessor, target)
    manifests.run_sample(manifest, "preflight", [
        sys.executable, "-B", str(SMOKE), "--revision", predecessor,
        "--case", "valid",
    ])
    manifests.fail(manifest)
    evidence = {p: p.read_bytes() for p in manifest.parent.rglob("*") if p.is_file()}
    controls = tmp_path / "controls.json"
    controls.write_text(json.dumps({
        "workers": ["uni-celery@fixture"], "enabled": True, "running": True,
        "frontend_local": predecessor, "frontend_public": predecessor,
    }))
    host = FixtureHost(repo, predecessor, controls)
    plan = recovery.RecoveryPlan(predecessor, target, manifest)
    return host, plan, recovery.Recovery(host, plan), originals, evidence, tmp_path / "states"


def state_path(root):
    paths = list(root.glob("*/state.json"))
    assert len(paths) == 1
    return paths[0]


def assert_originals(originals, evidence):
    assert all(path.read_bytes() == data for path, data in originals.items())
    assert all(path.read_bytes() == data for path, data in evidence.items())


def test_check_is_read_only_and_requires_exact_distinct_revisions(installation):
    host, plan, tool, originals, evidence, states = installation
    before = host.metadata.read_bytes()
    record = tool.check()
    assert record["release_completed"] is False
    assert record["status"] == "captured"
    assert not states.exists()
    assert not (host.repo / ".git/partial-release-recovery").exists()
    assert host.head() == plan.target and host.control()["enabled"]
    assert host.metadata.read_bytes() == before
    assert_originals(originals, evidence)
    for old, new in [(plan.target, plan.target), ("main", plan.target)]:
        with pytest.raises(recovery.RecoveryRefused):
            recovery.Recovery(host, recovery.RecoveryPlan(old, new, plan.failed_manifest))


def test_recovery_restores_baseline_without_publishing_or_reusing_receipts(installation):
    host, plan, tool, originals, evidence, states = installation
    state = tool.start(states)
    record = json.loads(state.read_text())
    assert record["status"] == "baseline_restored"
    assert record["release_completed"] is False
    assert host.head() == plan.predecessor
    assert host.git("rev-parse", "main") == plan.target  # No branch rewrite.
    assert host.control()["enabled"] and host.control()["running"]
    assert "stop" not in host.events
    assert_originals(originals, evidence)
    assert host.metadata.read_text() == f"RELEASE_REVISION={plan.predecessor}\n"
    for name, expected in record["evidence"].items():
        archived = state.parent / "original-evidence" / name
        assert hashlib.sha256(archived.read_bytes()).hexdigest() == expected
    assert not list(state.parent.glob("post_restart/*"))
    assert not list(plan.failed_manifest.parent.glob("post_restart/*"))
    assert tool.resume(state) == state
    assert state.stat().st_mode & 0o777 == 0o600
    assert state.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("mismatch", ["busy", "runtime", "metadata", "head", "staged", "source"])
def test_unsafe_start_refused_without_consumer_or_checkout_mutation(installation, mismatch):
    host, plan, tool, originals, evidence, states = installation
    if mismatch == "busy":
        host.busy = True
    elif mismatch == "runtime":
        host.service_revision = plan.target
    elif mismatch == "metadata":
        host.metadata.write_text(f"RELEASE_REVISION={plan.target}\n")
    elif mismatch == "head":
        host.git("commit", "--allow-empty", "-m", "concurrent revision")
    elif mismatch == "staged":
        path = host.repo / "unexpected.txt"
        path.write_text("operator work\n")
        host.git("add", str(path))
    else:
        (host.repo / "target-only.py").write_text("# operator edit\n")
    with pytest.raises((recovery.RecoveryRefused, subprocess.CalledProcessError)):
        tool.start(states)
    assert host.control()["enabled"] and host.control()["running"]
    assert not states.exists()
    assert not any(x in host.events for x in ("pause", "resume", "stop"))
    assert all(path.read_bytes() == data for path, data in evidence.items())


@pytest.mark.parametrize("ignored", [False, True])
def test_untracked_and_ignored_collisions_are_never_overwritten(installation, ignored):
    host, plan, tool, originals, evidence, states = installation
    if not ignored:
        # A symlink collision is rejected even when its target does not exist.
        path = host.repo / "retired.txt"
        path.symlink_to("operator-target")
    else:
        path = host.repo / "retired.txt"
        path.write_text("private runtime content\n")
    with pytest.raises(recovery.RecoveryRefused, match="collide"):
        tool.start(states)
    assert host.control()["enabled"] and host.head() == plan.target
    if ignored:
        assert path.read_text() == "private runtime content\n"
    else:
        assert path.is_symlink()
    assert_originals(originals, evidence)


def test_collision_appearing_after_pause_restores_recipes_and_leaves_paused(installation):
    host, plan, tool, originals, evidence, states = installation
    collision = host.repo / "retired.txt"
    host.after_prepare = lambda: collision.write_text("concurrent runtime file\n")
    with pytest.raises(recovery.RecoveryRefused):
        tool.start(states)
    assert host.head() == plan.target
    assert not host.control()["enabled"]
    assert collision.read_text() == "concurrent runtime file\n"
    assert json.loads(state_path(states).read_text())["status"] == "failed_paused"
    assert_originals(originals, evidence)


def test_concurrent_recipe_edit_is_not_overwritten_and_original_backup_remains(installation):
    host, plan, tool, originals, evidence, states = installation
    edited = next(iter(originals))
    host.after_prepare = lambda: edited.write_bytes(b"extraction: {operator: new}\n")
    with pytest.raises(recovery.RecoveryRefused):
        tool.start(states)
    assert edited.read_bytes() == b"extraction: {operator: new}\n"
    record = json.loads(state_path(states).read_text())
    recipe_record = json.loads(host.recipe_path(record).read_text())
    for entry in recipe_record["entries"]:
        path = host.repo / entry["path"]
        assert (host.repo / entry["backup"]).read_bytes() == originals[path]
    assert not host.control()["enabled"]
    assert all(path.read_bytes() == data for path, data in evidence.items())


@pytest.mark.parametrize("phase", [
    "captured", "archive_ready", "pausing", "paused", "preparing_recipes", "checking_out",
    "restoring_recipes", "resuming", "baseline_restored",
])
def test_process_death_can_be_resumed_from_durable_checkpoint(installation, phase):
    host, plan, tool, originals, evidence, states = installation
    def child():
        save = tool.save
        def checkpoint(path, record, step, **updates):
            save(path, record, step, **updates)
            if step == phase:
                os._exit(73)  # No exception handler or EXIT trap can run.
        tool.save = checkpoint
        tool.start(states)
    process = multiprocessing.get_context("fork").Process(target=child)
    process.start()
    process.join(timeout=30)
    if process.is_alive():
        process.kill()
        process.join()
        pytest.fail("disposable recovery child did not finish")
    assert process.exitcode == 73
    state = state_path(states)
    recovery.Recovery(host, plan).resume(state)
    assert json.loads(state.read_text())["status"] == "baseline_restored"
    assert host.head() == plan.predecessor and host.control()["enabled"]
    assert_originals(originals, evidence)
    record = json.loads(state.read_text())
    assert record["archive_complete"] is True
    assert recovery.evidence_snapshot(
        state.parent / "original-evidence/manifest.json",
    ) == record["evidence"]


def test_caught_interruption_retains_journal_and_pauses_safely(installation):
    host, plan, tool, originals, evidence, states = installation
    host.after_prepare = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        tool.start(states)
    assert not host.control()["enabled"]
    assert_originals(originals, evidence)
    host.after_prepare = None
    tool.resume(state_path(states))
    assert host.control()["enabled"] and host.head() == plan.predecessor
    assert_originals(originals, evidence)


@pytest.mark.parametrize("containment_fails", [False, True])
def test_consumer_resume_failure_is_never_reported_as_success(installation, containment_fails):
    host, plan, tool, originals, evidence, states = installation
    host.fail_resume, host.fail_containment = True, containment_fails
    with pytest.raises(recovery.RecoveryRefused):
        tool.start(states)
    record = json.loads(state_path(states).read_text())
    assert record["status"] == ("worker_stopped" if containment_fails else "failed_paused")
    assert record["release_completed"] is False
    assert not host.control()["enabled"]
    assert host.control()["running"] is not containment_fails
    assert_originals(originals, evidence)
    if containment_fails:
        with pytest.raises(recovery.RecoveryRefused, match="operator"):
            tool.resume(state_path(states))


def test_original_receipt_tampering_blocks_resume_without_replacing_proof(installation):
    host, plan, tool, originals, evidence, states = installation
    host.fail_resume = True
    with pytest.raises(recovery.RecoveryRefused):
        tool.start(states)
    state = state_path(states)
    receipt = next(path for path in evidence if path.name != "manifest.json")
    receipt.write_bytes(b"tampered\n")
    old_state = state.read_bytes()
    with pytest.raises(recovery.RecoveryRefused, match="evidence"):
        tool.resume(state)
    assert state.read_bytes() == old_state
    assert receipt.read_bytes() == b"tampered\n"
    archived = state.parent / "original-evidence" / str(receipt.relative_to(plan.failed_manifest.parent))
    assert archived.read_bytes() == evidence[receipt]
    assert not host.control()["enabled"]


def test_recovery_and_normal_release_share_exclusive_lock(tmp_path):
    lock = tmp_path / "release.lock"
    with release_lock(lock):
        with pytest.raises(recovery.RecoveryRefused, match="another release"):
            with release_lock(lock):
                pytest.fail("overlapping release acquired recovery lock")
    with release_lock(lock):
        pass
    script = (DEPLOY / "guarded_release.sh").read_text()
    assert "/run/university-portal-release.lock" in script
    assert script.index("flock -n 9") < script.index("verify_target_git_metadata\n")


def test_completed_or_post_restart_attempt_is_not_a_pre_restart_recovery(installation):
    host, plan, tool, originals, evidence, states = installation
    record = json.loads(plan.failed_manifest.read_text())
    record["phases"]["post_restart"] = {"status": "failed"}
    manifests.write_manifest(plan.failed_manifest, record)
    with pytest.raises(ValueError, match="pre-restart"):
        tool.start(states)
    assert host.control()["enabled"] and not states.exists()


def test_sigterm_cli_interruption_preserves_data_and_restores_handler(installation, monkeypatch):
    from deploy import recover_partial_release as cli
    host, plan, tool, originals, evidence, states = installation
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "RecoveryHost", lambda *a, **kw: host)
    monkeypatch.setattr(cli, "release_lock", lambda: release_lock(states.parent / "lock"))
    host.after_prepare = lambda: os.kill(os.getpid(), signal.SIGTERM)
    original_handler = signal.getsignal(signal.SIGTERM)
    original_umask = os.umask(0o077)
    os.umask(original_umask)
    result = cli.main([
        "recover", "--repo-root", str(host.repo),
        "--predecessor", plan.predecessor, "--target", plan.target,
        "--failed-manifest", str(plan.failed_manifest),
        "--expected-rehearsal-account-id", "151955775532",
        "--state-root", str(states),
    ])
    assert result == 1
    assert signal.getsignal(signal.SIGTERM) == original_handler
    after_umask = os.umask(original_umask)
    assert after_umask == original_umask
    assert not host.control()["enabled"]
    assert_originals(originals, evidence)


@pytest.fixture
def actual_observer(tmp_path, monkeypatch):
    """Exercise production adapter logic without importing real app clients."""
    host = RecoveryHost(tmp_path, account="151955775532")
    host.metadata.parent.mkdir(parents=True)
    host.metadata.write_text("RELEASE_REVISION=" + "a" * 40 + "\n")
    monkeypatch.setenv("RELEASE_REVISION", "a" * 40)
    replies = [{"worker": {"ok": "consumer changed"}}]
    queues = {"worker": [{"name": "scrape"}]}
    calls = []
    control = SimpleNamespace(
        add_consumer=lambda *a, **kw: calls.append(("add", kw)) or replies,
        cancel_consumer=lambda *a, **kw: calls.append(("cancel", kw)) or replies,
        inspect=lambda **kw: SimpleNamespace(active_queues=lambda: queues),
    )
    module = ModuleType("app.tasks.celery_app")
    module.celery_app = SimpleNamespace(control=control)
    monkeypatch.setitem(sys.modules, "app.tasks.celery_app", module)
    snapshot = {
        "ping": {"worker": {"ok": "pong"}},
        **{state: {"worker": []} for state in ("active", "reserved", "scheduled")},
    }
    async def idle(timeout):
        calls.append(("idle", timeout))
        return {"complete_worker_replies": snapshot}
    async def counts():
        return {}
    async def dispose():
        calls.append(("dispose", None))
    host._smoke = SimpleNamespace(
        DEFAULT_REHEARSAL_PROOF=Path("/not-read-by-fixture"),
        verify_service_process_release_identity=lambda *a: calls.append(("identity", a)),
        validate_database_rehearsal_requirement=lambda *a, **kw: calls.append(("proof", kw)),
        _wait_for_worker_idle=idle, _active_counts=counts,
        validate_idle_counts=lambda value: calls.append(("counts", value)),
        engine=SimpleNamespace(dispose=dispose),
        inspection_snapshot=snapshot,
    )
    return host, replies, queues, calls


def test_production_observer_checks_both_identities_proof_idle_and_complete_queues(actual_observer):
    host, replies, queues, calls = actual_observer
    assert host.observe("a" * 40, paused=False) == ["worker"]
    identities = [value for name, value in calls if name == "identity"]
    assert identities == [("uni-api-py", "a" * 40), ("uni-celery", "a" * 40)]
    proof = next(value for name, value in calls if name == "proof")
    assert proof["expected_account_id"] == "151955775532" and proof["max_age_hours"] == 24
    assert any(name == "dispose" for name, value in calls)
    queues["worker"] = []
    assert host.observe("a" * 40, paused=True, workers=["worker"]) == ["worker"]
    with pytest.raises(recovery.RecoveryRefused, match="membership"):
        host.observe("a" * 40, paused=True, workers=["other"])
    queues.clear()
    with pytest.raises(recovery.RecoveryRefused):
        host.observe("a" * 40, paused=True, workers=["worker"])


@pytest.mark.parametrize("bad", [
    [], [{"worker": {"error": "refused"}}], [{"worker": {"ok": False}}],
    [{"worker": {"ok": ""}}],
    [{"worker": {"ok": "yes"}}, {"worker": {"ok": "again"}}],
    [{"other": {"ok": "wrong worker"}}],
])
def test_consumer_control_refuses_missing_negative_or_duplicate_replies(actual_observer, bad):
    host, replies, queues, calls = actual_observer
    replies[:] = bad
    with pytest.raises(recovery.RecoveryRefused):
        host.pause(["worker"])


def test_consumer_commands_are_scoped_to_original_worker_set(actual_observer):
    host, replies, queues, calls = actual_observer
    host.pause(["worker"])
    host.resume_consumers(["worker"])
    assert calls == [
        ("cancel", {"destination": ["worker"], "reply": True, "timeout": 10}),
        ("add", {"destination": ["worker"], "reply": True, "timeout": 10}),
    ]


def test_active_queues_cannot_silently_omit_a_pinged_worker(actual_observer):
    host, replies, queues, calls = actual_observer
    snapshot = host._smoke.inspection_snapshot
    snapshot["ping"]["omitted"] = {"ok": "pong"}
    for state in ("active", "reserved", "scheduled"):
        snapshot[state]["omitted"] = []
    with pytest.raises(recovery.RecoveryRefused, match="membership"):
        host.observe("a" * 40, paused=False)
    assert not any(name in {"add", "cancel"} for name, value in calls)
    queues["worker"] = []
    with pytest.raises(recovery.RecoveryRefused, match="membership"):
        host.observe("a" * 40, paused=True, workers=["worker", "omitted"])
    assert not any(name in {"add", "cancel"} for name, value in calls)


@pytest.mark.parametrize("state", ["ping", "active", "reserved", "scheduled"])
def test_complete_task_observation_and_queues_must_share_exact_workers(actual_observer, state):
    host, replies, queues, calls = actual_observer
    host._smoke.inspection_snapshot[state].clear()
    with pytest.raises(recovery.RecoveryRefused, match="membership"):
        host.observe("a" * 40, paused=False)
    assert not any(name in {"add", "cancel"} for name, value in calls)


def test_new_work_before_resume_is_refused_without_cancelling_or_stopping_worker(installation):
    host, plan, tool, originals, evidence, states = installation
    host.fail_resume = True
    with pytest.raises(recovery.RecoveryRefused):
        tool.start(states)
    state = state_path(states)
    events = list(host.events)
    host.busy = True
    with pytest.raises(recovery.RecoveryRefused, match="idle predecessor"):
        tool.resume(state)
    assert host.control()["running"]
    assert host.events[len(events):] == ["frontend", "observe"]
    assert_originals(originals, evidence)


def test_missing_proof_or_changed_metadata_refuses_before_idle_or_controls(actual_observer):
    host, replies, queues, calls = actual_observer
    host.metadata.write_text("RELEASE_REVISION=" + "b" * 40 + "\n")
    with pytest.raises(recovery.RecoveryRefused, match="stored release"):
        host.observe("a" * 40, paused=False)
    assert not any(name in {"proof", "idle", "add", "cancel"} for name, value in calls)
    host.metadata.write_text("RELEASE_REVISION=" + "a" * 40 + "\n")
    def refuse(*args, **kwargs):
        raise RuntimeError("fixture missing signed proof")
    host._smoke.validate_database_rehearsal_requirement = refuse
    with pytest.raises(RuntimeError, match="signed proof"):
        host.observe("a" * 40, paused=False)
    assert not any(name in {"idle", "add", "cancel"} for name, value in calls)


def test_cli_defaults_to_explicit_action_and_has_no_service_or_gate_bypass():
    from deploy.recover_partial_release import parser
    with pytest.raises(SystemExit):
        parser().parse_args([])
    options = {option for action in parser()._actions for option in action.option_strings}
    assert not (options & {"--test-mode", "--skip-proof", "--force", "--publish",
                           "--restart", "--skip-idle", "--sample"})


def test_stat_cache_differences_do_not_become_false_recipe_changes(installation):
    host, plan, tool, originals, evidence, states = installation
    record = tool.check()
    record["recipe_attempt"] = "c" * 32
    host.bootstrap(record)
    host.prepare_recipes(record)
    assert host.changes() == []
    host.restore_recipes(record)
    assert_originals(originals, evidence)


def test_failed_worker_stop_retains_containment_failure_for_operator(installation):
    host, plan, tool, originals, evidence, states = installation
    host.fail_resume, host.fail_containment = True, True
    def refused_stop():
        raise recovery.RecoveryRefused("fixture could not verify stopped worker")
    host.stop_worker = refused_stop
    with pytest.raises(recovery.RecoveryRefused, match="stopped worker"):
        tool.start(states)
    record = json.loads(state_path(states).read_text())
    assert record["status"] == "containment_failed"
    assert record["release_completed"] is False
    assert_originals(originals, evidence)
    with pytest.raises(recovery.RecoveryRefused, match="operator"):
        tool.resume(state_path(states))


def test_worker_membership_change_aborts_before_checkout(installation):
    host, plan, tool, originals, evidence, states = installation
    host.after_prepare = lambda: host.update(workers=["uni-celery@fixture", "unexpected"])
    with pytest.raises(recovery.RecoveryRefused, match="membership"):
        tool.start(states)
    assert host.head() == plan.target
    assert not host.control()["running"]
    assert_originals(originals, evidence)


def test_recipe_journal_tampering_cannot_replace_captured_operator_bytes(installation):
    host, plan, tool, originals, evidence, states = installation
    host.fail_resume = True
    with pytest.raises(recovery.RecoveryRefused):
        tool.start(states)
    state = state_path(states)
    record = json.loads(state.read_text())
    path = host.recipe_path(record)
    journal = json.loads(path.read_text())
    journal["entries"][0]["operator_content_hex"] = b"forged: yaml\n".hex()
    path.write_text(json.dumps(journal))
    host.fail_resume = False
    with pytest.raises(recovery.RecoveryRefused, match="integrity"):
        tool.resume(state)
    assert not host.control()["enabled"]
    assert_originals(originals, evidence)


def test_interrupted_original_preflight_phase_is_read_not_rewritten(installation):
    host, plan, tool, originals, evidence, states = installation
    record = json.loads(plan.failed_manifest.read_text())
    record["status"] = "preflight_passed"  # Process died before an EXIT trap.
    manifests.write_manifest(plan.failed_manifest, record)
    original = plan.failed_manifest.read_bytes()
    state = tool.start(states)
    assert json.loads(state.read_text())["status"] == "baseline_restored"
    assert plan.failed_manifest.read_bytes() == original


@pytest.mark.parametrize("where", ["during_copy", "after_publication"])
def test_archive_copy_interruption_is_completed_before_consumer_changes(installation, where):
    host, plan, tool, originals, evidence, states = installation
    def child():
        if where == "during_copy":
            real_temporary = recovery.tempfile.NamedTemporaryFile
            class KilledCopy:
                def __init__(self, *args, **kwargs):
                    self.stream = real_temporary(*args, **kwargs)
                    self.name = self.stream.name
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    return self.stream.__exit__(*args)
                def write(self, data):
                    self.stream.write(data[:max(1, len(data) // 2)])
                    self.stream.flush()
                    os.fsync(self.stream.fileno())
                    os._exit(74)
            recovery.tempfile.NamedTemporaryFile = KilledCopy
        else:
            link = recovery.os.link
            def killed_link(*args, **kwargs):
                link(*args, **kwargs)
                os._exit(74)
            recovery.os.link = killed_link
        tool.start(states)
    process = multiprocessing.get_context("fork").Process(target=child)
    process.start()
    process.join(timeout=30)
    if process.is_alive():
        process.kill()
        process.join()
        pytest.fail("disposable archive-copy child did not finish")
    assert process.exitcode == 74
    assert host.control()["enabled"] and host.head() == plan.target
    state = state_path(states)
    assert json.loads(state.read_text()).get("archive_complete") is not True
    assert_originals(originals, evidence)
    recovery.Recovery(host, plan).resume(state)
    record = json.loads(state.read_text())
    assert record["status"] == "baseline_restored"
    assert record["archive_complete"] is True
    assert recovery.evidence_snapshot(
        state.parent / "original-evidence/manifest.json",
    ) == record["evidence"]
    assert host.control()["enabled"] and host.head() == plan.predecessor
    assert_originals(originals, evidence)


def test_archive_mismatch_never_overwrites_copy_or_changes_consumers(installation):
    host, plan, tool, originals, evidence, states = installation
    # Interrupt before archival without making any service or Git changes.
    def captured_only(state, record):
        raise KeyboardInterrupt()
    tool.ensure_archive = captured_only
    with pytest.raises(KeyboardInterrupt):
        tool.start(states)
    state = state_path(states)
    archive = state.parent / "original-evidence"
    archive.mkdir()
    copy = archive / "manifest.json"
    copy.write_bytes(b"changed archived evidence\n")
    before = state.read_bytes()
    with pytest.raises(recovery.RecoveryRefused, match="archived evidence"):
        recovery.Recovery(host, plan).resume(state)
    assert copy.read_bytes() == b"changed archived evidence\n"
    assert state.read_bytes() == before
    assert host.control()["enabled"] and host.head() == plan.target
    assert not any(name in host.events for name in ("pause", "resume", "stop"))
    assert_originals(originals, evidence)


def test_archive_is_verified_again_before_success_is_reported(installation):
    host, plan, tool, originals, evidence, states = installation
    resume = host.resume_consumers
    def tamper(workers):
        resume(workers)
        state = state_path(states)
        (state.parent / "original-evidence/unexpected.txt").write_bytes(b"not evidence")
    host.resume_consumers = tamper
    with pytest.raises(recovery.RecoveryRefused, match="archive"):
        tool.start(states)
    assert not host.control()["enabled"]
    record = json.loads(state_path(states).read_text())
    assert record["status"] == "failed_paused" and record["release_completed"] is False
    assert_originals(originals, evidence)


@pytest.mark.parametrize("where", ["frontend_local", "frontend_public"])
def test_target_frontend_with_old_services_refuses_recovery_without_writes(installation, where):
    host, plan, tool, originals, evidence, states = installation
    host.update(**{where: plan.target})
    with pytest.raises(recovery.RecoveryRefused, match="frontend"):
        tool.start(states)
    assert not states.exists()
    assert host.head() == plan.target and host.control()["enabled"]
    assert host.control()[where] == plan.target  # No blind frontend rollback.
    assert_originals(originals, evidence)


def test_abrupt_death_after_frontend_publication_cannot_claim_baseline(installation):
    host, plan, tool, originals, evidence, states = installation
    def child():
        # Simulate the real publication boundary: predecessor identities and
        # only the original preflight receipt, but target frontend now published.
        host.pause(["uni-celery@fixture"])
        host.update(frontend_local=plan.target, frontend_public=plan.target)
        os._exit(75)
    process = multiprocessing.get_context("fork").Process(target=child)
    process.start()
    process.join(timeout=30)
    if process.is_alive():
        process.kill()
        process.join()
        pytest.fail("disposable publication child did not finish")
    assert process.exitcode == 75
    with pytest.raises(recovery.RecoveryRefused, match="frontend"):
        tool.start(states)
    assert not states.exists() and not host.control()["enabled"]
    assert host.head() == plan.target
    assert host.metadata.read_text() == f"RELEASE_REVISION={plan.predecessor}\n"
    assert_originals(originals, evidence)


def test_frontend_change_during_recovery_leaves_consumers_paused(installation):
    host, plan, tool, originals, evidence, states = installation
    host.after_prepare = lambda: host.update(frontend_public=plan.target)
    with pytest.raises(recovery.RecoveryRefused, match="frontend"):
        tool.start(states)
    record = json.loads(state_path(states).read_text())
    assert record["status"] == "failed_paused"
    assert not host.control()["enabled"]
    with pytest.raises(recovery.RecoveryRefused, match="frontend"):
        tool.resume(state_path(states))
    assert_originals(originals, evidence)


def test_real_frontend_adapter_requires_local_and_public_predecessor_marker(tmp_path, monkeypatch):
    from deploy import verify_frontend_release as verifier
    host = RecoveryHost(tmp_path)
    dist = tmp_path / "artifacts/university-portal/dist/public"
    (dist / "assets").mkdir(parents=True)
    html = b'<script type="module" src="/assets/index-abcdefgh.js"></script>'
    (dist / "index.html").write_bytes(html)
    asset = dist / "assets/index-abcdefgh.js"
    marker = b"UNIVERSITY_PORTAL_RELEASE:" + b"a" * 40
    asset.write_bytes(marker)
    public = {"body": marker}
    def fetch(url):
        if "/assets/" in url:
            return verifier.FetchResponse(public["body"], {
                "cache-control": "public,max-age=31536000,immutable",
            })
        return verifier.FetchResponse(html, {"cache-control": "no-store,no-cache"})
    monkeypatch.setattr(verifier, "_fetch_response", fetch)
    host.verify_frontend("a" * 40)
    public["body"] = b"UNIVERSITY_PORTAL_RELEASE:" + b"b" * 40
    with pytest.raises(recovery.RecoveryRefused, match="published frontend"):
        host.verify_frontend("a" * 40)
    public["body"] = marker
    asset.write_bytes(b"UNIVERSITY_PORTAL_RELEASE:" + b"b" * 40)
    with pytest.raises(recovery.RecoveryRefused, match="published frontend"):
        host.verify_frontend("a" * 40)
