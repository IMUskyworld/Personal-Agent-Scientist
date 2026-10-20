"""Plumbing test: the runner really spawns a subprocess, captures logs and parses metrics.

This is NOT a science experiment — it is the one place where the real subprocess
path is exercised locally (DEVELOPMENT.md §18). Real experiments belong on the 3090.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_cli import design, run_cli

pytestmark = pytest.mark.slow


def test_run_executes_real_subprocess_and_parses_metrics(tmp_path: Path) -> None:
    code, output = design(tmp_path)
    assert code == 0, output
    workdir = str(tmp_path / ".labloop")

    code, output = run_cli("run", "batch", "--yes", "--workdir", workdir, cwd=tmp_path)
    assert code == 0, output
    assert "3 个 run 完成" in output

    runs_dir = tmp_path / ".labloop" / "studies" / "batch" / "rounds" / "round-001" / "runs"
    run_dirs = sorted(path for path in runs_dir.iterdir() if path.is_dir())
    assert len(run_dirs) == 3
    for run_dir in run_dirs:
        payload = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        assert payload["status"] == "ok"
        assert payload["metrics"]["accuracy"] == 0.9
        assert payload["duration_s"] > 0
        assert payload["env"]["python"]
        stdout_log = (run_dir / "stdout.log").read_text(encoding="utf-8")
        assert "acc: 0.9" in stdout_log
        assert (run_dir / "command.txt").is_file()

    # resume：重入不再执行
    code, output = run_cli("run", "batch", "--yes", "--workdir", workdir, cwd=tmp_path)
    assert code == 2
    assert "都已完成" in output
