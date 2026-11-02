"""Hermetic checks: no host systemd, package installs, Git writes or network."""
import importlib.util
from pathlib import Path
import subprocess

import pytest


DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
spec = importlib.util.spec_from_file_location("runtime_gate", DEPLOY / "release_runtime_prerequisites.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
TARGET = "a" * 40


@pytest.fixture
def host(monkeypatch):
    state = {
        "build_old": False, "service_old": False, "certifi": "2026.10.19",
        "files": "", "environment": 'PLAYWRIGHT_NODEJS_PATH=/usr/bin/node',
        "unset": "", "reload": "no", "calls": [],
        "policy": "module.exports.supported = () => true;",
        "requirements": "certifi>=2026.10.19",
        "paths": "scripts/check-node-security.cjs\nbackend-py/requirements.txt",
    }
    def fake(args, *, input=None):
        state["calls"].append((args, input))
        if "ls-tree" in args:
            assert args[-1] == TARGET
            return state["paths"]
        if "show" in args and "git" in args:
            assert args[-1].startswith(TARGET + ":")
            return state["policy"] if args[-1].endswith(".cjs") else state["requirements"]
        if args[:2] == ["systemctl", "show"]:
            return {"Environment": state["environment"], "EnvironmentFiles": state["files"],
                    "UnsetEnvironment": state["unset"], "NeedDaemonReload": state["reload"]}[args[3].split("=")[1]]
        if args[-1] == "-":
            if (args[0] == "sudo" and state["build_old"]) or (args[0] != "sudo" and state["service_old"]):
                raise gate.Refusal("node failed")
            assert input.startswith(state["policy"])
            assert "module.exports.supported(process.versions.node)" in input
            return ""
        if "importlib.metadata" in args[-1]:
            return state["certifi"]
        raise AssertionError(args)
    monkeypatch.setattr(gate, "run", fake)
    return state


def test_compatible_host_and_immutable_policy(host, capsys):
    gate.verify("/repo", TARGET)
    assert "RUNTIME_PREREQUISITES_OK" in capsys.readouterr().out
    assert len([c for c, _ in host["calls"] if c[-1] == "-"]) == 3
    assert not any(word in c for c, _ in host["calls"] for word in ("restart", "install", "checkout"))


@pytest.mark.parametrize("field", ["build_old", "service_old"])
def test_old_node_refuses_without_package_or_service_mutations(host, field):
    host[field] = True
    with pytest.raises(gate.Refusal, match="Node"):
        gate.verify("/repo", TARGET)
    assert not any("install" in c or "restart" in c for c, _ in host["calls"])


@pytest.mark.parametrize("version", ["2026.7.22", "unknown"])
def test_old_or_unknown_certifi(host, version):
    host["certifi"] = version
    with pytest.raises(gate.Refusal, match="certifi"):
        gate.verify("/repo", TARGET)


def test_file_override_wins_and_last_file_wins(host, tmp_path):
    first, last = tmp_path / "first", tmp_path / "last"
    first.write_text("SECRET=do-not-print\nPLAYWRIGHT_NODEJS_PATH=/old/node\n")
    last.write_text('PLAYWRIGHT_NODEJS_PATH="/patched/node"\n')
    host["files"] = f"{first} (ignore_errors=no) {last} (ignore_errors=no)"
    gate.verify("/repo", TARGET)
    commands = [c for c, _ in host["calls"]]
    assert ["/patched/node", "-"] in commands
    assert ["/old/node", "-"] not in commands
    assert "do-not-print" not in repr(host["calls"])


def test_bad_file_override_is_checked_not_unit_environment(host, tmp_path):
    env = tmp_path / "env"
    env.write_text("PLAYWRIGHT_NODEJS_PATH=/old/node")
    host["files"] = f"{env} (ignore_errors=no)"
    host["service_old"] = True
    with pytest.raises(gate.Refusal, match="service browser"):
        gate.verify("/repo", TARGET)
    assert ["/old/node", "-"] in [c for c, _ in host["calls"]]


@pytest.mark.parametrize("field,value", [
    ("environment", ""), ("unset", "PLAYWRIGHT_NODEJS_PATH"),
    ("unset", "PLAYWRIGHT_NODEJS_PATH=/usr/bin/node"), ("reload", "yes"),
    ("files", "/unparseable (invalid=yes)"),
])
def test_missing_unset_unloaded_or_unparseable_environment_fails_closed(host, field, value):
    host[field] = value
    with pytest.raises(gate.Refusal):
        gate.verify("/repo", TARGET)


def test_optional_missing_file_is_safe_required_missing_refuses(host, tmp_path):
    host["files"] = f"{tmp_path / 'absent'} (ignore_errors=yes)"
    gate.verify("/repo", TARGET)
    host["files"] = host["files"].replace("yes", "no")
    with pytest.raises(gate.Refusal, match="missing"):
        gate.verify("/repo", TARGET)


@pytest.mark.parametrize("content", [
    "PLAYWRIGHT_NODEJS_PATH='unterminated",
    "PLAYWRIGHT_NODEJS_PATH=$HOME/node",
    "PLAYWRIGHT_NODEJS_PATH=/usr/\\\nbin/node",
])
def test_ambiguous_environment_refuses_without_execution(tmp_path, content):
    env = tmp_path / "env"
    env.write_text(content)
    with pytest.raises(gate.Refusal):
        gate.environment_file_node(str(env), False, None)


def test_legacy_target_without_policy_or_requirement(host):
    host["paths"] = "backend-py/requirements.txt"
    host["requirements"] = "httpx>=0.27"
    gate.verify("/repo", TARGET)
    assert not any(c[0] == "systemctl" for c, _ in host["calls"])


def test_unsupported_requirement_refuses(host):
    host["requirements"] = "certifi~=2026.10"
    with pytest.raises(gate.Refusal, match="Unsupported"):
        gate.verify("/repo", TARGET)


def test_command_failure_redacts_output(monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args, output="SECRET", stderr="SECRET")
    monkeypatch.setattr(gate.subprocess, "run", fail)
    with pytest.raises(gate.Refusal) as exc:
        gate.run(["node", "-"])
    assert "SECRET" not in str(exc.value)


def test_gate_precedes_any_sample_consumer_or_checkout_mutation():
    script = (DEPLOY / "guarded_release.sh").read_text()
    production = script[script.index('if [ "$release_test_mode" != 1 ]; then'):]
    gate_index = production.index("verify_target_runtime")
    for later in ("verify_target_worker_schema", "initialize_release_evidence",
                  "release_sample preflight", 'cancel_consumer("scrape"',
                  'checkout "$repo_root"', "systemctl restart"):
        assert gate_index < production.index(later)
    assert '"$target:$checker"' in script
