"""The runner must kill the whole process tree, not just the shell it started.

A training run is usually ``cmd /c yolo train …``: terminating only the shell
would leave the trainer alive, holding the GPU (DEVELOPMENT.md §11).
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from labloop.execute.runner import spawn

pytestmark = pytest.mark.slow

SLEEPER = """
import pathlib, sys, time

heartbeat = pathlib.Path(sys.argv[1])
while True:
    with heartbeat.open("a", encoding="utf-8") as handle:
        handle.write("x")
    time.sleep(0.05)
"""


def _size(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


def test_timeout_kills_the_whole_process_tree(tmp_path: Path) -> None:
    script = tmp_path / "sleeper.py"
    script.write_text(SLEEPER, encoding="utf-8")
    heartbeat = tmp_path / "heartbeat.txt"
    log = tmp_path / "stdout.log"
    command = subprocess.list2cmdline([sys.executable, str(script), str(heartbeat)])

    exit_code, timed_out = spawn(command, cwd=tmp_path, timeout_s=1.5, env_extra={}, log_path=log)
    assert timed_out is True
    assert exit_code is None or exit_code != 0
    assert heartbeat.exists(), "子进程至少启动过"

    # 子进程必须已经死了：心跳不再增长
    time.sleep(0.5)
    before = _size(heartbeat)
    time.sleep(1.0)
    assert _size(heartbeat) == before, "超时后子进程仍在运行（进程树没有被杀掉）"
    assert log.is_file()


def test_successful_run_streams_output_to_the_log(tmp_path: Path) -> None:
    script = tmp_path / "printer.py"
    script.write_text("print('acc: 0.9')\n", encoding="utf-8")
    log = tmp_path / "stdout.log"
    command = subprocess.list2cmdline([sys.executable, str(script)])

    exit_code, timed_out = spawn(command, cwd=tmp_path, timeout_s=30, env_extra={}, log_path=log)
    assert exit_code == 0
    assert timed_out is False
    assert "acc: 0.9" in log.read_text(encoding="utf-8")
