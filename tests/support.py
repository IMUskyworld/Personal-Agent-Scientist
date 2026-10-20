"""Shared builders for tests (importable thanks to pytest pythonpath=["tests"])."""

from __future__ import annotations

from datetime import datetime

from labloop.domain.enums import Backend, Goal, RunStatus, Source
from labloop.domain.models import EnvRecord, GpuSummary, Hypothesis, Parameter, RunResult, Study

CWD = "C:/fake/project"


def make_study(
    *,
    slug: str = "demo",
    question: str = "batch size 对精度的影响",
    goal: Goal = Goal.MAXIMIZE,
    primary_metric: str = "accuracy",
    secondary_metrics: list[str] | None = None,
    param: str = "batch",
    values: list[str] | None = None,
    backend: Backend = Backend.OFFLINE,
    recipe_id: str = "generic",
) -> Study:
    return Study(
        slug=slug,
        question=question,
        created_at=datetime(2026, 9, 22, 22, 0, 0).isoformat(),
        recipe_id=recipe_id,
        backend=backend,
        goal=goal,
        primary_metric=primary_metric,
        secondary_metrics=secondary_metrics or ["duration_s"],
        parameter=Parameter(name=param, values=values or ["8", "16", "32", "64"]),
        fixed={"seed": "42"},
        hypothesis=Hypothesis(
            statement="测试假设",
            variable=param,
            expected_effect="unknown",
            rationale="fixture",
            source=Source.OFFLINE,
        ),
        cwd=CWD,
        runs_planned=4,
    )


def make_env(*, packages_hash: str = "abc123", gpu: tuple[str, str] | None = None) -> EnvRecord:
    return EnvRecord(
        platform="Windows-11",
        python="3.11.9",
        packages_hash=packages_hash,
        gpu=GpuSummary(name=gpu[0], driver=gpu[1]) if gpu else None,
    )


def make_result(
    value: str,
    metric: float | None,
    *,
    status: RunStatus = RunStatus.OK,
    duration_s: float = 10.0,
    param: str = "batch",
    primary_metric: str = "accuracy",
    run_id: str | None = None,
    env: EnvRecord | None = None,
) -> RunResult:
    metrics = {primary_metric: metric} if metric is not None else {}
    return RunResult(
        run_id=run_id or f"r001-{param}-{value}",
        params={param: value},
        status=status,
        exit_code=0 if status == RunStatus.OK else 1,
        started_at="2026-09-22T22:00:00+08:00",
        ended_at="2026-09-22T22:00:10+08:00",
        duration_s=duration_s,
        metrics=metrics,
        env=env if env is not None else make_env(),
    )
