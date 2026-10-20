from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from labloop.domain.enums import MetricKind, RunStatus
from labloop.domain.models import ExperimentPlan, RunSpec
from labloop.errors import LabloopError
from labloop.execute.envrecord import collect_env, packages_hash
from labloop.execute.metrics import parse_metrics
from labloop.execute.runner import Runner, build_child_env
from labloop.paths import StudyLayout
from labloop.recipes.builtin import build_generic
from labloop.recipes.model import MetricRule
from support import make_env, make_study

# --- metrics ------------------------------------------------------------------


def _recipe():
    return build_generic(
        command="python train.py --batch {batch}",
        metric_flags=["accuracy=acc:\\s*([0-9.]+)"],
        var_values={"batch": ["8"]},
    )


def test_parse_metrics_stdout_regex_takes_last_match(tmp_path: Path) -> None:
    recipe = _recipe()
    metrics, warnings = parse_metrics(
        recipe.metrics,
        stdout_text="acc: 0.700\n...\nacc: 0.844\n",
        cwd=tmp_path,
        template_vars={},
    )
    assert metrics == {"accuracy": 0.844}
    assert warnings == []


def test_parse_metrics_missing_match_warns(tmp_path: Path) -> None:
    recipe = _recipe()
    metrics, warnings = parse_metrics(recipe.metrics, stdout_text="no numbers here", cwd=tmp_path, template_vars={})
    assert metrics == {}
    assert warnings and "未在输出中找到" in warnings[0]


def test_parse_metrics_scans_a_large_log_in_chunks(tmp_path: Path) -> None:
    """大日志分块扫描：最后一条指标仍能被找到（内存里不放整份日志）。"""
    log = tmp_path / "stdout.log"
    log.write_text("noise line\n" * 50_000 + "acc: 0.42\n", encoding="utf-8")
    metrics, warnings = parse_metrics(
        _recipe().metrics,
        stdout_path=log,
        cwd=tmp_path,
        template_vars={},
    )
    assert metrics == {"accuracy": 0.42}
    assert warnings == []


def test_parse_metrics_rejects_non_finite_values(tmp_path: Path) -> None:
    log = tmp_path / "stdout.log"
    log.write_text("acc: nan\n", encoding="utf-8")
    rules = [MetricRule(name="accuracy", kind=MetricKind.STDOUT_REGEX, pattern=r"acc:\s*(\S+)")]
    metrics, warnings = parse_metrics(rules, stdout_path=log, cwd=tmp_path, template_vars={})
    assert metrics == {}
    assert warnings and "有限数值" in warnings[0]


def test_parse_metrics_csv_handles_quoted_commas(tmp_path: Path) -> None:
    csv_path = tmp_path / "results.csv"
    csv_path.write_text('epoch,"metrics/mAP50(B)",note\n0,0.5,"a, b"\n1,0.844,"c, d"\n', encoding="utf-8")
    rules = [
        MetricRule(
            name="mAP50",
            kind=MetricKind.CSV_LAST,
            file="results.csv",
            column="metrics/mAP50(B)",
        )
    ]
    metrics, warnings = parse_metrics(rules, cwd=tmp_path, template_vars={})
    assert metrics == {"mAP50": 0.844}
    assert warnings == []


def test_parse_metrics_csv_last_and_json_file(tmp_path: Path) -> None:
    csv_path = tmp_path / "runs" / "labloop-r1" / "results.csv"
    csv_path.parent.mkdir(parents=True)
    csv_path.write_text("epoch,metrics/mAP50(B),time\n0,0.5,10\n1,0.844,20\n", encoding="utf-8")
    rules = [
        MetricRule(
            name="mAP50",
            kind=MetricKind.CSV_LAST,
            file="{project}/labloop-{run_id}/results.csv",
            column="metrics/mAP50(B)",
        ),
        MetricRule(
            name="bad", kind=MetricKind.CSV_LAST, file="{project}/labloop-{run_id}/results.csv", column="does-not-exist"
        ),
    ]
    metrics, warnings = parse_metrics(
        rules,
        stdout_text="",
        cwd=tmp_path,
        template_vars={"project": "runs", "run_id": "r1"},
    )
    assert metrics == {"mAP50": 0.844}
    assert any("缺少列" in warning for warning in warnings)

    json_path = tmp_path / "out.json"
    json_path.write_text(json.dumps({"final": {"acc": 0.91}}), encoding="utf-8")
    json_rules = [MetricRule(name="acc", kind=MetricKind.JSON_FILE, file="out.json", point="final.acc")]
    metrics_json, _ = parse_metrics(json_rules, stdout_text="", cwd=tmp_path, template_vars={})
    assert metrics_json == {"acc": 0.91}

    missing_rules = [MetricRule(name="acc", kind=MetricKind.JSON_FILE, file="nope.json", point="final.acc")]
    metrics_missing, warnings_missing = parse_metrics(missing_rules, stdout_text="", cwd=tmp_path, template_vars={})
    assert metrics_missing == {}
    assert any("不存在" in warning for warning in warnings_missing)


# --- envrecord ----------------------------------------------------------------


def test_build_child_env_drops_labloop_secrets(monkeypatch) -> None:
    monkeypatch.setenv("LABLOOP_LLM_API_KEY", "super-secret")
    monkeypatch.setenv("TRAINING_DATA", "/data/coco")
    env = build_child_env({"LABLOOP_RUN_ID": "r001-batch-8", "LABLOOP_ROUND": "1"})
    assert "LABLOOP_LLM_API_KEY" not in env
    assert env["LABLOOP_RUN_ID"] == "r001-batch-8"
    assert env["LABLOOP_ROUND"] == "1"
    # 训练脚本需要的普通环境变量照常继承
    assert env["TRAINING_DATA"] == "/data/coco"


def test_build_child_env_cannot_be_reintroduced_by_env_extra(monkeypatch) -> None:
    monkeypatch.setenv("LABLOOP_LLM_API_KEY", "super-secret")
    env = build_child_env({"LABLOOP_LLM_API_KEY": "leaked-again"})
    assert "LABLOOP_LLM_API_KEY" not in env


def test_packages_hash_is_stable() -> None:
    first = packages_hash()
    assert first == packages_hash()
    assert len(first) == 12


def test_collect_env_shape() -> None:
    env = collect_env()
    assert env.platform
    assert env.python
    assert len(env.packages_hash) == 12


# --- runner -------------------------------------------------------------------


class FakeSpawner:
    def __init__(
        self,
        *,
        exit_code: int = 0,
        output: str = "done acc: 0.9",
        timed_out: bool = False,
        interrupt_command: str | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.env_extras: list[dict[str, str]] = []
        self.exit_code = exit_code
        self.output = output
        self.timed_out = timed_out
        self.interrupt_command = interrupt_command

    def __call__(self, command, *, cwd, timeout_s, env_extra, log_path):
        self.calls.append(command)
        self.env_extras.append(dict(env_extra))
        if self.interrupt_command is not None and command == self.interrupt_command:
            raise KeyboardInterrupt
        Path(log_path).write_text(self.output, encoding="utf-8")
        return self.exit_code, self.timed_out


def _plan(values: list[str]) -> ExperimentPlan:
    runs = [
        RunSpec(
            id=f"r001-batch-{value}", params={"batch": value}, command=f"python train.py --batch {value}", timeout_s=5
        )
        for value in values
    ]
    return ExperimentPlan(round=1, created_at="2026-09-22T22:00:00+08:00", runs=runs)


def _runner(tmp_path: Path, values: list[str], spawner: FakeSpawner, *, redo: bool = False) -> Runner:
    study = make_study(values=values)
    study.cwd = str(tmp_path)
    layout = StudyLayout(tmp_path / "studies" / "demo")
    return Runner(
        layout=layout,
        study=study,
        plan=_plan(values),
        recipe=_recipe(),
        redo=redo,
        spawner=spawner,
        env_collector=lambda: make_env(),
    )


def test_runner_happy_path_writes_artifacts(tmp_path: Path) -> None:
    spawner = FakeSpawner()
    runner = _runner(tmp_path, ["8", "16"], spawner)
    outcome = runner.run_round()
    assert [result.status for result in outcome.results] == [RunStatus.OK, RunStatus.OK]
    assert all(result.metrics == {"accuracy": 0.9} for result in outcome.results)
    assert all(result.env is not None for result in outcome.results)
    run_dir = tmp_path / "studies" / "demo" / "rounds" / "round-001" / "runs" / "r001-batch-8"
    for name in ("spec.json", "command.txt", "stdout.log", "result.json"):
        assert (run_dir / name).is_file()
    assert spawner.env_extras[0]["LABLOOP_RUN_ID"] == "r001-batch-8"
    assert spawner.env_extras[0]["LABLOOP_ROUND"] == "1"


def test_runner_resume_skips_done_and_redo_reruns(tmp_path: Path) -> None:
    spawner = FakeSpawner()
    runner = _runner(tmp_path, ["8", "16"], spawner)
    runner.run_round()
    assert len(spawner.calls) == 2

    again = runner.run_round()
    assert again.results == []
    assert len(spawner.calls) == 2  # 未重复执行

    redo_runner = _runner(tmp_path, ["8", "16"], spawner, redo=True)
    redo_runner.run_round()
    assert len(spawner.calls) == 4


def test_runner_failed_and_timeout_statuses(tmp_path: Path) -> None:
    failed = _runner(tmp_path, ["8"], FakeSpawner(exit_code=1, output="boom"))
    result = failed.run_round().results[0]
    assert result.status == RunStatus.FAILED
    assert result.metrics == {}
    assert result.error and "退出码 1" in result.error

    timed = _runner(tmp_path, ["16"], FakeSpawner(timed_out=True, exit_code=None))
    result_timed = timed.run_round().results[0]
    assert result_timed.status == RunStatus.TIMEOUT
    assert result_timed.error and "超时" in result_timed.error


def test_runner_max_runs_truncates(tmp_path: Path) -> None:
    spawner = FakeSpawner()
    runner = _runner(tmp_path, ["8", "16", "32", "64"], spawner)
    outcome = runner.run_round(max_runs=2)
    assert outcome.stopped == "max_runs"
    assert len(outcome.results) == 2
    assert len(spawner.calls) == 2


def test_runner_budget_stops_before_start(tmp_path: Path) -> None:
    spawner = FakeSpawner()
    runner = _runner(tmp_path, ["8", "16"], spawner)
    outcome = runner.run_round(deadline=time.monotonic() - 1)
    assert outcome.stopped == "budget"
    assert outcome.results == []
    assert spawner.calls == []


def test_runner_interrupt_records_and_resumes(tmp_path: Path) -> None:
    spawner = FakeSpawner(interrupt_command="python train.py --batch 8")
    runner = _runner(tmp_path, ["8"], spawner)
    with pytest.raises(KeyboardInterrupt):
        runner.run_round()
    result_path = tmp_path / "studies" / "demo" / "rounds" / "round-001" / "runs" / "r001-batch-8" / "result.json"
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["status"] == "interrupted"

    # interrupted ≠ done：重入会重试
    resume_spawner = FakeSpawner()
    resume_runner = _runner(tmp_path, ["8"], resume_spawner)
    outcome = resume_runner.run_round()
    assert [result.status for result in outcome.results] == [RunStatus.OK]


def test_runner_missing_cwd_is_structured(tmp_path: Path) -> None:
    study = make_study()
    study.cwd = str(tmp_path / "does-not-exist")
    layout = StudyLayout(tmp_path / "studies" / "demo")
    runner = Runner(layout=layout, study=study, plan=_plan(["8"]), recipe=_recipe(), spawner=FakeSpawner())
    with pytest.raises(LabloopError) as excinfo:
        runner.run_round()
    assert excinfo.value.code == "RUN_FAILED"
