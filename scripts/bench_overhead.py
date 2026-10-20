"""Measure design / plan / analyze / report overhead against DEVELOPMENT.md §17.

Usage:  python scripts/bench_overhead.py
Exit 1 when a measurement exceeds its target (2x grace on CI runners).
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from labloop.analyze.analyzer import analyze_round  # noqa: E402
from labloop.design.backend import DesignHints  # noqa: E402
from labloop.design.heuristic import HeuristicBackend  # noqa: E402
from labloop.domain.enums import Backend, Goal, RunStatus, Source  # noqa: E402
from labloop.domain.models import (  # noqa: E402
    EnvRecord,
    ExperimentPlan,
    Hypothesis,
    Parameter,
    RunResult,
    RunSpec,
    Study,
)
from labloop.paths import StudyLayout  # noqa: E402
from labloop.recipes.builtin import build_generic  # noqa: E402
from labloop.recipes.model import render_command  # noqa: E402
from labloop.report.writer import build_report, write_html_report, write_json_report  # noqa: E402

TIMESTAMP = "2026-09-22T22:00:00+08:00"


def make_study(values: list[str]) -> Study:
    return Study(
        slug="bench",
        question="bench：batch 对精度的影响",
        created_at=TIMESTAMP,
        recipe_id="generic",
        backend=Backend.OFFLINE,
        goal=Goal.MAXIMIZE,
        primary_metric="accuracy",
        secondary_metrics=["duration_s"],
        parameter=Parameter(name="batch", values=list(values)),
        fixed={"seed": "42"},
        hypothesis=Hypothesis(statement="bench", variable="batch", rationale="", source=Source.OFFLINE),
        cwd=str(Path.cwd()),
        runs_planned=len(values),
    )


def make_result(value: str, metric: float, *, duration_s: float, run_id: str) -> RunResult:
    return RunResult(
        run_id=run_id,
        params={"batch": value},
        status=RunStatus.OK,
        exit_code=0,
        started_at=TIMESTAMP,
        ended_at=TIMESTAMP,
        duration_s=duration_s,
        metrics={"accuracy": metric},
        env=EnvRecord(platform="bench", python="3.11", packages_hash="bench"),
    )


TARGETS = {
    "design (offline)": 1.0,
    "plan render (16 runs)": 0.5,
    "analyze (16 runs)": 0.5,
    "report (3 rounds)": 1.0,
}

REPEATS = 3


def best_of(action, repeats: int = REPEATS) -> tuple[float, object]:
    best = float("inf")
    result = None
    for _ in range(repeats):
        started = time.perf_counter()
        result = action()
        best = min(best, time.perf_counter() - started)
    return best, result


def main() -> int:
    grace = 2.0 if os.environ.get("CI") else 1.0
    tmp = Path(tempfile.mkdtemp(prefix="labloop-bench-"))
    study = make_study(values=[str(index) for index in range(16)])
    recipe = build_generic(
        command="python train.py --batch {batch} --seed {seed}",
        metric_flags=["accuracy=acc:\\s*([0-9.]+)"],
        var_values={"batch": study.parameter.values},
    )
    backend = HeuristicBackend()
    hints = DesignHints(var_values={"batch": list(study.parameter.values)}, max_runs=32)

    design_seconds, _outcome = best_of(lambda: backend.design(question=study.question, recipe=recipe, hints=hints))

    runs = [
        RunSpec(
            id=f"r001-batch-{value}",
            params={"batch": value},
            command=render_command(
                recipe, {"batch": value, "seed": "42"}, run_id=f"r001-batch-{value}", round_no=1, slug=study.slug
            ),
        )
        for value in study.parameter.values
    ]
    plan = ExperimentPlan(round=1, created_at="2026-09-22T22:00:00+08:00", runs=runs)

    def render_plan() -> None:
        for spec in plan.runs[:16]:
            render_command(
                recipe, {"batch": spec.params["batch"], "seed": "42"}, run_id=spec.id, round_no=1, slug=study.slug
            )

    plan_seconds, _ = best_of(render_plan)

    results = [
        make_result(value, 0.80 + index * 0.005, duration_s=10.0 - index * 0.2, run_id=f"r001-batch-{value}")
        for index, value in enumerate(study.parameter.values)
    ]
    analyze_seconds, _analysis = best_of(
        lambda: analyze_round(study=study, results=results, round_no=1, backend=backend)
    )

    layout = StudyLayout(tmp / "studies" / "demo")

    def render_report() -> None:
        rounds = [
            (round_no, plan, results, analyze_round(study=study, results=results, round_no=round_no, backend=backend))
            for round_no in (1, 2, 3)
        ]
        report = build_report(study=study, rounds=rounds)
        write_json_report(report, layout.report_json_path)
        write_html_report(report, study, layout.report_html_path)

    report_seconds, _ = best_of(render_report)

    measurements = {
        "design (offline)": design_seconds,
        "plan render (16 runs)": plan_seconds,
        "analyze (16 runs)": analyze_seconds,
        "report (3 rounds)": report_seconds,
    }
    print(f"{'operation':<24} {'actual':>8} {'target':>8}  verdict")
    failures = 0
    for name, actual in measurements.items():
        target = TARGETS[name]
        ok = actual <= target * grace
        failures += 0 if ok else 1
        print(f"{name:<24} {actual:>7.3f}s {target:>7.1f}s  {'PASS' if ok else 'FAIL'}")
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)
    if failures:
        print(f"\n{failures} 项超过目标（grace x{grace:g}）")
        return 1
    print("\nall measurements within targets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
