"""Contract tests for the Redis preflight in run_full_scraper_regression.sh."""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "run_full_scraper_regression.sh"


def _write_executable(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!/bin/sh\nset -eu\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run_preflight(
    tmp_path: Path,
    *,
    cli_body: str | None,
    server_body: str | None = None,
    python_body: str = 'echo "python $*" >> "$CALLS_LOG"',
) -> tuple[subprocess.CompletedProcess[str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"

    if cli_body is not None:
        _write_executable(bin_dir, "redis-cli", cli_body)
    if server_body is not None:
        _write_executable(bin_dir, "redis-server", server_body)
    _write_executable(bin_dir, "python", python_body)
    _write_executable(bin_dir, "sleep", ":")

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "CALLS_LOG": str(log),
        "TEST_STATE": str(tmp_path / "redis-ready"),
        "TEST_REDIS_HOST": "test.invalid",
        "TEST_REDIS_PORT": "16379",
    }
    result = subprocess.run(
        ["/bin/bash", str(SCRIPT), "tests/example.py"],
        cwd=SCRIPT.parents[1],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, log


def test_uses_already_available_redis_without_starting_or_stopping_it(
    tmp_path: Path,
) -> None:
    result, log = _run_preflight(
        tmp_path,
        cli_body="""
echo "redis-cli $*" >> "$CALLS_LOG"
printf 'PONG\\n'
""",
        server_body='echo "redis-server $*" >> "$CALLS_LOG"; exit 99',
    )

    assert result.returncode == 0, result.stderr
    calls = log.read_text()
    assert "redis-server" not in calls
    assert "shutdown" not in calls
    assert "python -m pytest -q tests/example.py" in calls


def test_starts_and_cleans_up_only_its_temporary_redis(tmp_path: Path) -> None:
    result, log = _run_preflight(
        tmp_path,
        cli_body="""
echo "redis-cli $*" >> "$CALLS_LOG"
case " $* " in
  *" shutdown nosave "*) rm -f "$TEST_STATE"; exit 0 ;;
esac
test -f "$TEST_STATE" && printf 'PONG\\n'
""",
        server_body="""
echo "redis-server $*" >> "$CALLS_LOG"
touch "$TEST_STATE"
""",
    )

    assert result.returncode == 0, result.stderr
    calls = log.read_text()
    assert "redis-server --bind test.invalid --port 16379" in calls
    assert "python -m pytest -q tests/example.py" in calls
    assert "redis-cli -h test.invalid -p 16379 shutdown nosave" in calls
    assert not (tmp_path / "redis-ready").exists()


def test_failed_pytest_preserves_exit_code_and_cleans_up_temporary_redis(
    tmp_path: Path,
) -> None:
    result, log = _run_preflight(
        tmp_path,
        cli_body="""
echo "redis-cli $*" >> "$CALLS_LOG"
case " $* " in
  *" shutdown nosave "*) rm -f "$TEST_STATE"; exit 0 ;;
esac
test -f "$TEST_STATE" && printf 'PONG\\n'
""",
        server_body="""
echo "redis-server $*" >> "$CALLS_LOG"
touch "$TEST_STATE"
""",
        python_body='echo "python $*" >> "$CALLS_LOG"; exit 7',
    )

    assert result.returncode == 7
    calls = log.read_text()
    assert "redis-server --bind test.invalid --port 16379" in calls
    assert "python -m pytest -q tests/example.py" in calls
    assert "redis-cli -h test.invalid -p 16379 shutdown nosave" in calls
    assert not (tmp_path / "redis-ready").exists()


def test_interrupted_pytest_cleans_up_temporary_redis(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    pytest_running = tmp_path / "pytest-running"

    _write_executable(
        bin_dir,
        "redis-cli",
        """
echo "redis-cli $*" >> "$CALLS_LOG"
case " $* " in
  *" shutdown nosave "*) rm -f "$TEST_STATE"; exit 0 ;;
esac
test -f "$TEST_STATE" && printf 'PONG\\n'
""",
    )
    _write_executable(
        bin_dir,
        "redis-server",
        """
echo "redis-server $*" >> "$CALLS_LOG"
touch "$TEST_STATE"
""",
    )
    _write_executable(
        bin_dir,
        "python",
        """
echo "python $*" >> "$CALLS_LOG"
touch "$PYTEST_RUNNING"
while :; do sleep 1; done
""",
    )

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "CALLS_LOG": str(log),
        "TEST_STATE": str(tmp_path / "redis-ready"),
        "PYTEST_RUNNING": str(pytest_running),
        "TEST_REDIS_HOST": "test.invalid",
        "TEST_REDIS_PORT": "16379",
    }
    process = subprocess.Popen(
        ["/bin/bash", str(SCRIPT), "tests/example.py"],
        cwd=SCRIPT.parents[1],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    try:
        deadline = time.monotonic() + 5
        while not pytest_running.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                pytest.fail("fake pytest process did not start before timeout")
            time.sleep(0.01)

        assert process.poll() is None
        os.killpg(process.pid, signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)

    assert process.returncode != 0, (stdout, stderr)
    calls = log.read_text()
    assert "redis-server --bind test.invalid --port 16379" in calls
    assert "python -m pytest -q tests/example.py" in calls
    assert "redis-cli -h test.invalid -p 16379 shutdown nosave" in calls
    assert not (tmp_path / "redis-ready").exists()


@pytest.mark.parametrize("missing_binary", ["redis-cli", "redis-server"])
def test_missing_redis_binary_is_a_setup_error(
    tmp_path: Path, missing_binary: str
) -> None:
    server_body = None if missing_binary == "redis-server" else ":"
    result, log = _run_preflight(
        tmp_path,
        cli_body=None if missing_binary == "redis-cli" else "printf 'PONG\\n'",
        server_body=server_body,
    )

    assert result.returncode == 2
    assert "requires redis-cli and redis-server" in result.stderr
    assert not log.exists() or "python " not in log.read_text()


def test_failed_readiness_is_a_setup_error_and_attempts_cleanup(
    tmp_path: Path,
) -> None:
    result, log = _run_preflight(
        tmp_path,
        cli_body="""
echo "redis-cli $*" >> "$CALLS_LOG"
exit 1
""",
        server_body='echo "redis-server $*" >> "$CALLS_LOG"',
    )

    assert result.returncode == 2
    assert "Redis did not become ready at test.invalid:16379" in result.stderr
    calls = log.read_text()
    assert "redis-server --bind test.invalid --port 16379" in calls
    assert "shutdown nosave" in calls
    assert "python " not in calls