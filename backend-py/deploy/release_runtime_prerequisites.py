"""Read-only runtime checks against an immutable, reviewed release target.

No application imports, package installation, sample dispatch, or service writes.
Only fixed diagnostic messages reach stdout/stderr; environment contents do not.
"""
from __future__ import annotations

import argparse
import re
import shlex
import subprocess
from pathlib import Path


class Refusal(RuntimeError):
    pass


def run(args: list[str], *, input: str | None = None) -> str:
    try:
        return subprocess.run(
            args, input=input, text=True, capture_output=True, check=True, timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise Refusal("Runtime inspection failed; review host access and runtime configuration.") from None


def target_file(repo: str, target: str, path: str) -> str:
    return run(["sudo", "-u", "ubuntu", "git", "-C", repo, "show", f"{target}:{path}"])


def check_node(binary: str, policy: str, *, build_user: bool = False) -> None:
    # Execute the reviewed target policy, not a stale checkout's policy. stdin
    # avoids requiring the build user to traverse private deployment directories.
    command = (["sudo", "-u", "ubuntu"] if build_user else []) + [binary, "-"]
    try:
        run(command, input=policy + "\nif (typeof module.exports.supported !== 'function' || "
            "!module.exports.supported(process.versions.node)) process.exit(1);\n")
    except Refusal:
        role = "build-user" if build_user else "service browser"
        raise Refusal(f"Incompatible {role} Node; obtain approval to install the target's Node baseline before release.") from None


def certifi_floor(requirements: str) -> tuple[int, ...] | None:
    lines = [s.strip() for s in requirements.splitlines()
             if re.match(r"^\s*certifi\b", s, re.I)]
    if not lines:
        return None
    if len(lines) != 1 or not re.fullmatch(r"certifi\s*>=\s*\d+(?:\.\d+)+(?:\s*#.*)?", lines[0], re.I):
        raise Refusal("Unsupported target certifi requirement; review the runtime gate before release.")
    return tuple(map(int, re.search(r"\d+(?:\.\d+)+", lines[0]).group().split(".")))


def environment_file_node(path: str, optional: bool, current: str | None) -> str | None:
    try:
        contents = Path(path).read_text()
    except FileNotFoundError:
        if optional:
            return current
        raise Refusal("A required service environment file is missing.") from None
    except OSError:
        raise Refusal("Cannot inspect a service environment file.") from None
    # Deliberately reject complicated syntax rather than incorrectly predict
    # systemd's effective value. Never source environment files as shell code.
    if "\\\n" in contents:
        raise Refusal("Multiline service environment requires manual prerequisite review.")
    for line in contents.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if re.match(r"PLAYWRIGHT_NODEJS_PATH\s*=", line):
            value = line.split("=", 1)[1].strip()
            if value.startswith(('"', "'")):
                if len(value) < 2 or value[-1] != value[0]:
                    raise Refusal("Unsupported browser-runtime environment syntax.")
                value = value[1:-1]
            if not re.fullmatch(r"/[A-Za-z0-9_./+-]+", value):
                raise Refusal("Browser-runtime environment must specify a plain absolute Node path.")
            current = value
    return current


def service_node(service: str) -> str:
    def prop(name: str) -> str:
        return run(["systemctl", "show", service, f"--property={name}", "--value"])
    if prop("NeedDaemonReload") != "no":
        raise Refusal("Service configuration is not loaded; review and reload approved units before release.")
    try:
        values = shlex.split(prop("Environment"))
    except ValueError:
        raise Refusal("Cannot parse service environment configuration.") from None
    current = next((v.split("=", 1)[1] for v in reversed(values)
                    if v.startswith("PLAYWRIGHT_NODEJS_PATH=")), None)
    files = prop("EnvironmentFiles")
    matches = list(re.finditer(r"(/\S+) \(ignore_errors=(yes|no)\)", files))
    if re.sub(r"(/\S+) \(ignore_errors=(yes|no)\)", "", files).strip():
        raise Refusal("Unsupported service environment-file configuration.")
    for match in matches:
        current = environment_file_node(match[1], match[2] == "yes", current)
    # UnsetEnvironment is applied after files and overrides Environment too.
    if any(v == "PLAYWRIGHT_NODEJS_PATH" or v == f"PLAYWRIGHT_NODEJS_PATH={current}"
           for v in shlex.split(prop("UnsetEnvironment"))):
        current = None
    if not current or not re.fullmatch(r"/[A-Za-z0-9_./+-]+", current):
        raise Refusal("Service browser Node is not explicitly configured; review EnvironmentFile overrides before release.")
    return current


def verify(repo: str, target: str) -> None:
    if not re.fullmatch(r"[a-f0-9]{40}", target):
        raise Refusal("Runtime gate requires an immutable full target revision.")
    paths = set(run(["sudo", "-u", "ubuntu", "git", "-C", repo, "ls-tree",
                     "-r", "--name-only", target]).splitlines())
    policy_path = "scripts/check-node-security.cjs"
    if policy_path in paths:
        policy = target_file(repo, target, policy_path)
        check_node("node", policy, build_user=True)
        for service in ("uni-api-py.service", "uni-celery.service"):
            check_node(service_node(service), policy)
    requirements_path = "backend-py/requirements.txt"
    if requirements_path in paths:
        floor = certifi_floor(target_file(repo, target, requirements_path))
        if floor is not None:
            version = run([f"{repo}/backend-py/.venv/bin/python", "-B", "-c",
                           "import importlib.metadata; print(importlib.metadata.version('certifi'))"])
            if not re.fullmatch(r"\d+(?:\.\d+)+", version) or tuple(map(int, version.split("."))) < floor:
                raise Refusal("Production certifi is below the target requirement; obtain approval for the certificate-package update.")
    print("RUNTIME_PREREQUISITES_OK")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--target", required=True)
    args = parser.parse_args()
    try:
        verify(args.repo, args.target)
    except (Refusal, ValueError, OSError):
        # Refusal text is authored above, never raw command output or env data.
        import sys
        exc = sys.exception()
        message = str(exc) if isinstance(exc, Refusal) else "Runtime configuration inspection failed."
        print(f"Release refused: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
