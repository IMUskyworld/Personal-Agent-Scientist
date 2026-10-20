"""Windows quoting regression: the command must reach the shell verbatim.

``cmd /c`` strips one leading and one trailing quote, and Python re-escapes
quotes when it builds a command line from a list. Together they used to break
every command that contained a quoted path (e.g. ``"C:\\Program Files\\…\\python.exe"``).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from labloop.execute.runner import spawn

pytestmark = pytest.mark.slow


def _run(command: str, tmp_path: Path) -> tuple[int | None, str]:
    log = tmp_path / "stdout.log"
    exit_code, timed_out = spawn(command, cwd=tmp_path, timeout_s=30, env_extra={}, log_path=log)
    assert timed_out is False
    return exit_code, log.read_text(encoding="utf-8")


def test_quoted_executable_path_runs(tmp_path: Path) -> None:
    script = tmp_path / "printer.py"
    script.write_text("print('acc: 0.9')\n", encoding="utf-8")
    command = subprocess.list2cmdline([sys.executable, str(script)])
    exit_code, output = _run(command, tmp_path)
    assert exit_code == 0
    assert "acc: 0.9" in output


def test_quoted_argument_survives(tmp_path: Path) -> None:
    exit_code, output = _run('echo "hello world"', tmp_path)
    assert exit_code == 0
    assert "hello world" in output


def test_shell_operators_still_work(tmp_path: Path) -> None:
    exit_code, output = _run("echo a && echo b", tmp_path)
    assert exit_code == 0
    assert "a" in output and "b" in output
