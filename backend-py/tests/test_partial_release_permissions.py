"""Opt-in Linux/root integration: real sudo, Git and reconciler; fake services."""
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from uuid import uuid4

import pytest

from deploy import recover_partial_release as cli


pytestmark = pytest.mark.skipif(
    os.environ.get("ALLOW_DISPOSABLE_RECOVERY_PERMISSION_TESTS") != "1",
    reason="requires explicit disposable Linux root/account/ACL opt-in",
)

# Share the exact failed-release fixture and fake service adapter with the
# hermetic suite, including when pytest uses --import-mode=importlib.
spec = importlib.util.spec_from_file_location(
    "_permission_recovery_fixture",
    Path(__file__).with_name("test_partial_release_recovery.py"),
)
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


def checked(*args):
    return subprocess.check_output(args, stderr=subprocess.PIPE, timeout=60)


@pytest.fixture(scope="module")
def service_account():
    """Create only a unique throwaway account/group; never reuse ubuntu."""
    if sys.platform != "linux" or os.geteuid() != 0:
        pytest.fail("explicit permission suite requires genuine Linux root")
    for tool in ("sudo", "git", "useradd", "userdel", "groupadd", "groupdel", "setfacl"):
        if not shutil.which(tool):
            pytest.fail(f"explicit permission suite requires {tool}")
    name = "upr_" + uuid4().hex[:12]
    root = Path(tempfile.mkdtemp(prefix="portal-recovery-permissions-"))
    root.chmod(0o755)
    group_created = user_created = False
    try:
        checked("groupadd", name)
        group_created = True
        checked("useradd", "--no-create-home", "--no-user-group", "--gid", name,
                "--home-dir", str(root / "home"), "--shell", "/bin/sh", name)
        user_created = True
        identity = pwd.getpwnam(name)
        home = root / "home"
        home.mkdir(mode=0o700)
        os.chown(home, identity.pw_uid, identity.pw_gid)
        assert identity.pw_uid != 0
        owner = ("sudo", "-n", "-H", "-u", name)
        assert int(checked(*owner, "id", "-u")) == identity.pw_uid
        # A Python executable in a root-private venv cannot be used by sudo.
        python = sys.executable
        assert int(checked(*owner, python, "-I", "-c",
                           "import os,yaml; print(os.geteuid())")) == identity.pw_uid
        yield SimpleNamespace(root=root, uid=identity.pw_uid,
                              gid=identity.pw_gid, owner=owner, python=python)
    finally:
        # No userdel -r, recursive chmod, sudoers edits or pre-existing accounts.
        # Even a failing account cleanup must not prevent the other cleanups.
        try:
            if user_created:
                checked("userdel", name)
                with pytest.raises(KeyError):
                    pwd.getpwnam(name)
        finally:
            try:
                if group_created:
                    checked("groupdel", name)
            finally:
                shutil.rmtree(root)


@pytest.fixture
def installation(service_account, monkeypatch):
    account = service_account
    base = Path(tempfile.mkdtemp(prefix="case-", dir=account.root))
    base.chmod(0o755)  # Only a newly allocated disposable directory.
    try:
        host, plan, _, originals, evidence, states = fixtures.installation.__wrapped__(base)
        for index, path in enumerate(originals):
            data = f"# operator café {index}\r\nextraction: {{label: retained}}\r\n".encode()
            path.write_bytes(data)
            path.chmod((0o600, 0o640, 0o644)[index % 3])
            originals[path] = data
        modes = {path: stat.S_IMODE(path.stat().st_mode) for path in originals}
        # The sole recursive ownership operation is confined to the NEW repo.
        for path in (host.repo, *host.repo.rglob("*")):
            assert not path.is_symlink()
            os.chown(path, account.uid, account.gid)
        host.owner_command = list(account.owner)
        host.python = account.python
        states.mkdir(mode=0o700)
        evidence_root = plan.failed_manifest.parent.parent
        evidence_root.chmod(0o700)
        for path in evidence:
            path.chmod(0o600)
        metadata = host.metadata.read_bytes()

        def host_factory(repo, **kwargs):
            assert repo == host.repo
            assert kwargs["owner_command"] == ("sudo", "-H", "-u", "ubuntu")
            return host

        # Only external service identity/control and production path selection
        # are substituted. Real CLI root/umask/signal/error handling stays intact.
        monkeypatch.setattr(cli, "RecoveryHost", host_factory)
        monkeypatch.setattr(cli, "release_lock",
                            lambda: fixtures.release_lock(base / "release.lock"))

        def invoke(action="recover", state=None):
            args = [
                action, "--repo-root", str(host.repo),
                "--predecessor", plan.predecessor, "--target", plan.target,
                "--failed-manifest", str(plan.failed_manifest),
                "--expected-rehearsal-account-id", "000000000000",
                "--state-root", str(states),
            ]
            if state is not None:
                args += ["--state", str(state)]
            return cli.main(args)

        yield SimpleNamespace(
            account=account, host=host, plan=plan, states=states, invoke=invoke,
            originals=originals, evidence=evidence, modes=modes, metadata=metadata,
        )
    finally:
        shutil.rmtree(base)


def owner_probe(case, path, operation):
    """Check actual kernel access as the owner, not root's os.access result."""
    return case.host.run_owner(
        case.account.python, "-I", "-c",
        "import pathlib,sys; p=pathlib.Path(sys.argv[1]); "
        "p.read_bytes() if sys.argv[2]=='read' else p.write_bytes(b'forbidden')",
        path, operation,
    )


def assert_private(case, state):
    paths = [case.states, state.parent, state,
             *state.parent.rglob("original-evidence"),
             *(state.parent / "original-evidence").rglob("*"),
             *case.evidence]
    for path in paths:
        assert path.stat().st_uid == 0
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
        if path.is_file():
            for operation in ("read", "write"):
                with pytest.raises(subprocess.CalledProcessError):
                    owner_probe(case, path, operation)
    for path, data in case.evidence.items():
        assert path.read_bytes() == data
        relative = path.relative_to(case.plan.failed_manifest.parent)
        assert (state.parent / "original-evidence" / relative).read_bytes() == data


def assert_owned(case, path, data=None, mode=None):
    info = path.stat()
    assert (info.st_uid, info.st_gid) == (case.account.uid, case.account.gid)
    if data is not None:
        assert path.read_bytes() == data
        assert owner_probe(case, path, "read") == b""  # command succeeds
    if mode is not None:
        assert stat.S_IMODE(info.st_mode) == mode


def assert_backups(case, state):
    record = json.loads(state.read_bytes())
    workdir = case.host.workdir(record)
    for path in (workdir.parent, workdir, *workdir.rglob("*")):
        assert_owned(case, path)
    assert_owned(case, workdir, mode=0o700)
    assert_owned(case, workdir / "reconciler.py", mode=0o600)
    manifest = json.loads(case.host.recipe_path(record).read_bytes())
    for entry in manifest["entries"]:
        original = case.host.repo / entry["path"]
        assert_owned(case, case.host.repo / entry["backup"],
                     case.originals[original], case.modes[original])
    return record


@contextmanager
def denied_directory(case, path, mechanism, *, deferred=False):
    """A reversible denial on a newly created fixture directory only."""
    mode = stat.S_IMODE(path.stat().st_mode)
    uid, gid = path.stat().st_uid, path.stat().st_gid
    def deny():
        if mechanism == "mode":
            path.chmod(0o500)
        else:
            # Named-user ACL must win over an otherwise writable group.
            # Changing only u:: would merely reproduce the mode-bit test.
            os.chown(path, 0, case.account.gid)
            path.chmod(0o770)
            checked("setfacl", "-m", f"u:{case.account.uid}:r-x,m::rwx", str(path))
        with pytest.raises(subprocess.CalledProcessError):
            owner_probe(case, path / "denial-probe", "write")
    try:
        if not deferred:
            deny()
        yield deny
    finally:
        if mechanism == "acl":
            checked("setfacl", "-b", str(path))
        os.chown(path, uid, gid)
        path.chmod(mode)


def assert_refused(case, capsys):
    assert case.invoke() == 1
    output = capsys.readouterr()
    assert "Recovery stopped" in output.err
    assert '"status": "baseline_restored"' not in output.out
    assert '"release_completed": true' not in output.out
    assert "resume" not in case.host.events
    assert "stop" not in case.host.events
    assert case.host.control()["running"]
    assert case.host.metadata.read_bytes() == case.metadata
    assert all(path.read_bytes() == data for path, data in case.evidence.items())


def test_root_cli_check_recover_and_resume_preserve_owner_and_private_state(installation, capsys):
    case = installation
    assert case.invoke("check") == 0
    assert not list(case.states.iterdir())
    assert not (case.host.repo / ".git/partial-release-recovery").exists()
    assert case.invoke() == 0
    state = fixtures.state_path(case.states)
    record = assert_backups(case, state)
    assert record["status"] == "baseline_restored"
    assert record["release_completed"] is False
    assert_private(case, state)
    for path, data in case.originals.items():
        assert_owned(case, path, data, case.modes[path])
    assert case.host.head() == case.plan.predecessor
    assert case.host.git("rev-parse", "main") == case.plan.target
    before = state.read_bytes()
    assert case.invoke("resume", state) == 0
    assert state.read_bytes() == before
    assert case.host.metadata.read_bytes() == case.metadata
    assert case.host.control()["enabled"] and case.host.control()["running"]
    assert "stop" not in case.host.events
    assert '"release_completed": true' not in capsys.readouterr().out


@pytest.mark.parametrize("mechanism", ["mode", "acl"])
def test_helper_workspace_denial_is_not_reported_as_recovery(installation, capsys, mechanism):
    case = installation
    parent = case.host.repo / ".git/partial-release-recovery"
    parent.mkdir(mode=0o700)
    os.chown(parent, case.account.uid, case.account.gid)
    with denied_directory(case, parent, mechanism):
        assert_refused(case, capsys)
        assert case.host.head() == case.plan.target
        assert case.host.control()["enabled"]
        assert "pause" not in case.host.events
        state = fixtures.state_path(case.states)
        assert json.loads(state.read_bytes())["status"] == "captured"
        assert_private(case, state)
    for path, data in case.originals.items():
        assert_owned(case, path, data, case.modes[path])


@pytest.mark.parametrize("mechanism", ["mode", "acl"])
def test_backup_denial_leaves_original_recipes_and_failed_private_journal(
    installation, capsys, mechanism,
):
    case = installation
    backup_root = case.host.repo / ".git/release-tracked-config-backups"
    backup_root.mkdir(mode=0o700)
    os.chown(backup_root, case.account.uid, case.account.gid)
    with denied_directory(case, backup_root, mechanism):
        assert_refused(case, capsys)
        state = fixtures.state_path(case.states)
        record = json.loads(state.read_bytes())
        assert record["status"] == "failed_paused"
        assert record["release_completed"] is False
        assert not case.host.control()["enabled"]
        assert case.host.head() == case.plan.target
        assert_private(case, state)
    for path, data in case.originals.items():
        assert_owned(case, path, data, case.modes[path])


@pytest.mark.parametrize("mechanism", ["mode", "acl"])
@pytest.mark.parametrize("concurrent_edit", [False, True])
def test_restore_denial_keeps_backups_and_never_overwrites_concurrent_edit(
    installation, capsys, mechanism, concurrent_edit,
):
    case = installation
    edited = next(iter(case.originals))
    new_data = b"# concurrent operator edit\r\nextraction: {label: new}\r\n"
    checkout = case.host.checkout
    with denied_directory(case, edited.parent, mechanism, deferred=True) as deny:
        # Permit preparation and checkout, then deny owner writes at the actual
        # restoration boundary; the concurrent edit also uses real sudo.
        def deny_after_checkout(predecessor):
            checkout(predecessor)
            if concurrent_edit:
                case.host.run_owner(case.account.python, "-I", "-c",
                                    "import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes("
                                    "bytes.fromhex(sys.argv[2]))", edited, new_data.hex())
            deny()

        case.host.checkout = deny_after_checkout
        assert_refused(case, capsys)
        state = fixtures.state_path(case.states)
        record = assert_backups(case, state)
        assert record["status"] == "failed_paused"
        assert not case.host.control()["enabled"]
        expected = (new_data if concurrent_edit else case.host.run_owner(
            "git", "show", case.plan.predecessor + ":" + str(edited.relative_to(case.host.repo)),
        ))
        assert_owned(case, edited, expected)
        assert_private(case, state)
        assert case.invoke("resume", state) == 1
        output = capsys.readouterr()
        assert '"status": "baseline_restored"' not in output.out
        assert json.loads(state.read_bytes())["status"] == "failed_paused"
        assert edited.read_bytes() == expected
