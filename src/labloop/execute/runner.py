"""Serial experiment execution (DEVELOPMENT.md §11): resume, timeout, budget, logs."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from labloop.domain.enums import RunStatus
from labloop.domain.models import ExperimentPlan, Note, RunResult, RunSpec, Study
from labloop.errors import RUN_FAILED, LabloopError
from labloop.execute.envrecord import collect_env
from labloop.execute.logstream import CHUNK_BYTES, LogStream
from labloop.execute.metrics import parse_metrics
from labloop.paths import StudyLayout
from labloop.recipes.model import Recipe

DEFAULT_TIMEOUT_S = 1800.0
TERMINATE_GRACE_S = 5.0

#: labloop 自己的密钥绝不下发给实验进程（§16：密钥只留在 labloop 侧）。
SECRET_ENV_KEYS = ("LABLOOP_LLM_API_KEY",)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def build_child_env(env_extra: dict[str, str], *, base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for the experiment process.

    Inherits the parent environment (training scripts legitimately need CUDA/PATH
    variables), injects the ``LABLOOP_*`` context variables, and **drops labloop's
    own secrets** so a training script can never print or upload the LLM API key.
    """
    env = dict(os.environ if base is None else base)
    env.update(env_extra)
    for key in SECRET_ENV_KEYS:
        env.pop(key, None)
    return env


def _taskkill(pid: int, *, force: bool = True) -> None:
    """Kill a process tree on Windows (``taskkill /T`` walks the child tree)."""
    argv = ["taskkill", "/PID", str(pid), "/T"] + (["/F"] if force else [])
    try:
        subprocess.run(argv, capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - best effort
        pass


def _signal_group(proc: subprocess.Popen[bytes], sig: int) -> bool:
    """Send a signal to the child's process group (POSIX only)."""
    killpg = getattr(os, "killpg", None)
    getpgid = getattr(os, "getpgid", None)
    if killpg is None or getpgid is None:  # pragma: no cover - Windows
        return False
    try:
        killpg(getpgid(proc.pid), sig)
    except (OSError, ProcessLookupError):  # pragma: no cover - already gone
        return False
    return True


def terminate_tree(proc: subprocess.Popen[bytes]) -> None:
    """Stop the whole process tree, not just the shell that started it.

    A training run is usually ``cmd /c yolo train …``: terminating only the shell
    would leave the trainer alive, holding the GPU. On POSIX the child gets its
    own process group (signalled as a group); on Windows ``taskkill /T`` walks the
    tree.
    """
    if proc.poll() is not None:
        return
    if os.name == "nt":
        _taskkill(proc.pid)
        return
    if not _signal_group(proc, signal.SIGTERM):
        proc.terminate()


def kill_tree(proc: subprocess.Popen[bytes]) -> None:
    """Force-kill the whole process tree (after the grace period expired)."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        _taskkill(proc.pid)
        return
    if not _signal_group(proc, signal.SIGKILL):
        proc.kill()


def _pump(stream: object, sink: LogStream) -> None:
    """Drain the child's output into the log file (runs in a helper thread)."""
    try:
        while True:
            chunk = stream.read1(CHUNK_BYTES)  # type: ignore[attr-defined]
            if not chunk:
                break
            sink.write(chunk)
    except (OSError, ValueError):  # pragma: no cover - pipe closed under us
        pass
    finally:
        sink.close()


def spawn(
    command: str,
    *,
    cwd: Path,
    timeout_s: float,
    env_extra: dict[str, str],
    log_path: Path,
) -> tuple[int | None, bool]:
    """Run one command; returns (exit_code, timed_out).

    Output is streamed into ``log_path`` while the process runs (never buffered in
    memory). Timeout: terminate the tree → 5s grace → kill the tree. Ctrl+C does
    the same and then re-raises.
    """
    # cmd /c must receive the command text *verbatim*: cmd strips one leading and
    # one trailing quote, so the command gets one extra quote pair around it.
    # The raw string is passed to Popen on purpose — a list would make Python
    # re-escape the quotes and break paths like "C:\Program Files\…\python.exe".
    command_line: str | list[str] = f'cmd /c "{command}"' if os.name == "nt" else ["sh", "-c", command]
    env = build_child_env(env_extra)
    popen_kwargs: dict[str, object] = {}
    if os.name == "nt":
        # Own process group: Ctrl+C in the labloop console must not race the child.
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    sink = LogStream(log_path)
    try:
        proc = subprocess.Popen(
            command_line,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            **popen_kwargs,  # type: ignore[arg-type]
        )
    except OSError:
        sink.close()
        raise
    pump = threading.Thread(target=_pump, args=(proc.stdout, sink), daemon=True)
    pump.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_tree(proc)
        try:
            proc.wait(timeout=TERMINATE_GRACE_S)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            proc.wait()
    except KeyboardInterrupt:
        terminate_tree(proc)
        try:
            proc.wait(timeout=TERMINATE_GRACE_S)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            proc.wait()
        pump.join(timeout=TERMINATE_GRACE_S)
        raise
    pump.join(timeout=TERMINATE_GRACE_S)
    return proc.returncode, timed_out


@dataclass
class RunRoundOutcome:
    results: list[RunResult] = field(default_factory=list)
    stopped: str | None = None  # None | "max_runs" | "budget"


class Runner:
    def __init__(
        self,
        *,
        layout: StudyLayout,
        study: Study,
        plan: ExperimentPlan,
        recipe: Recipe,
        timeout_s: float | None = None,
        redo: bool = False,
        spawner: Callable[..., tuple[int | None, bool]] = spawn,
        env_collector: Callable[[], object] = collect_env,
        echo: Callable[[str], None] | None = None,
    ) -> None:
        self._layout = layout
        self._study = study
        self._plan = plan
        self._recipe = recipe
        self._timeout_s = timeout_s
        self._redo = redo
        self._spawner = spawner
        self._env_collector = env_collector
        self._echo = echo or (lambda _message: None)
        self._cwd = Path(study.cwd)

    def pending(self) -> list[RunSpec]:
        return [spec for spec in self._plan.runs if self._redo or not self._is_done(spec.id)]

    def _is_done(self, run_id: str) -> bool:
        result_path = self._layout.runs_dir(self._plan.round) / run_id / "result.json"
        if not result_path.is_file():
            return False
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return payload.get("status") == RunStatus.OK.value

    def run_round(self, *, max_runs: int | None = None, deadline: float | None = None) -> RunRoundOutcome:
        if not self._cwd.is_dir():
            raise LabloopError(f"实验目录不存在：{self._cwd}", code=RUN_FAILED, details={"cwd": str(self._cwd)})
        outcome = RunRoundOutcome()
        pending = self.pending()
        if max_runs is not None and len(pending) > max_runs:
            self._echo(f"待执行 {len(pending)} 个 run，按 --max-runs={max_runs} 截断")
            outcome.stopped = "max_runs"
            pending = pending[:max_runs]
        for spec in pending:
            if deadline is not None and time.monotonic() >= deadline:
                self._echo("预算耗尽，停止执行")
                outcome.stopped = "budget"
                break
            self._echo(f"▶ {spec.id}：{spec.command}")
            outcome.results.append(self._execute_one(spec))
        return outcome

    def _execute_one(self, spec: RunSpec) -> RunResult:
        run_dir = self._layout.run_dir(self._plan.round, spec.id)
        (run_dir / "spec.json").write_text(
            json.dumps(spec.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        (run_dir / "command.txt").write_text(spec.command + "\n", encoding="utf-8", newline="\n")
        log_path = run_dir / "stdout.log"
        timeout_s = spec.timeout_s or self._timeout_s or DEFAULT_TIMEOUT_S
        started_at = _now()
        started = time.perf_counter()
        env_extra = {"LABLOOP_RUN_ID": spec.id, "LABLOOP_ROUND": str(self._plan.round)}
        try:
            exit_code, timed_out = self._spawner(
                spec.command,
                cwd=self._cwd,
                timeout_s=timeout_s,
                env_extra=env_extra,
                log_path=log_path,
            )
        except KeyboardInterrupt:
            self._append_log(log_path, "[用户中断]\n")
            self._write_result(
                run_dir,
                RunResult(
                    run_id=spec.id,
                    params=spec.params,
                    status=RunStatus.INTERRUPTED,
                    exit_code=None,
                    started_at=started_at,
                    ended_at=_now(),
                    duration_s=time.perf_counter() - started,
                    error="用户中断（Ctrl+C）",
                ),
            )
            raise
        duration_s = time.perf_counter() - started
        status = RunStatus.OK if exit_code == 0 else (RunStatus.TIMEOUT if timed_out else RunStatus.FAILED)
        metrics: dict[str, float] = {}
        warnings: list[Note] = []
        if status == RunStatus.OK:
            template_vars = {
                **self._study.fixed,
                **spec.params,
                "run_id": spec.id,
                "round": str(self._plan.round),
                "slug": self._study.slug,
            }
            parsed, parse_warnings = parse_metrics(
                self._recipe.metrics,
                stdout_path=log_path,
                cwd=self._cwd,
                template_vars=template_vars,
            )
            metrics = parsed
            warnings = [Note(code="METRIC_UNPARSEABLE", message=message) for message in parse_warnings]
        error: str | None = None
        if status == RunStatus.TIMEOUT:
            error = f"超时（{timeout_s:.0f}s）"
        elif status == RunStatus.FAILED:
            error = f"退出码 {exit_code}"
        result = RunResult(
            run_id=spec.id,
            params=spec.params,
            status=status,
            exit_code=exit_code,
            started_at=started_at,
            ended_at=_now(),
            duration_s=duration_s,
            metrics=metrics,
            log_path="stdout.log",
            env=self._env_collector(),  # type: ignore[arg-type]
            error=error,
            warnings=warnings,
            fixed=dict(self._study.fixed),
            recipe_id=self._recipe.id,
        )
        self._write_result(run_dir, result)
        return result

    def _append_log(self, log_path: Path, text: str) -> None:
        payload = text if text.endswith("\n") else text + "\n"
        with log_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)

    def _write_result(self, run_dir: Path, result: RunResult) -> None:
        (run_dir / "result.json").write_text(
            json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
