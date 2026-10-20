"""Report assembly + serialization (DEVELOPMENT.md §13.2-13.3)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from labloop import __version__
from labloop.analyze.rules import to_float
from labloop.domain.enums import Goal, RunStatus
from labloop.domain.models import ExperimentPlan, Report, ReportRound, RoundAnalysis, RunResult, Study, ToolInfo
from labloop.report.svg import line_chart

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


def _ordered_results(plan: ExperimentPlan | None, results: list[RunResult], param: str) -> list[RunResult]:
    by_id = {result.run_id: result for result in results}
    ordered: list[RunResult] = []
    if plan is not None:
        ordered = [by_id[spec.id] for spec in plan.runs if spec.id in by_id]
    seen = {result.run_id for result in ordered}
    ordered.extend(result for result in results if result.run_id not in seen)
    numeric = all(to_float(result.params.get(param, "")) is not None for result in ordered if result.params)
    if numeric and ordered:
        ordered.sort(key=lambda result: to_float(result.params.get(param, "0")) or 0.0)
    return ordered


def build_report(
    *,
    study: Study,
    rounds: list[tuple[int, ExperimentPlan | None, list[RunResult], RoundAnalysis | None]],
    stopped_reason: str | None = None,
) -> Report:
    report_rounds: list[ReportRound] = []
    best_candidates = []
    for round_no, plan, results, analysis in rounds:
        report_rounds.append(
            ReportRound(round=round_no, analysis=analysis, runs=_ordered_results(plan, results, study.parameter.name))
        )
        if analysis is not None and analysis.best is not None:
            best_candidates.append(analysis.best)
    best_overall = None
    if best_candidates:
        if study.goal == Goal.MAXIMIZE:
            best_overall = max(best_candidates, key=lambda point: point.metric_value)
        else:
            best_overall = min(best_candidates, key=lambda point: point.metric_value)
    return Report(
        tool=ToolInfo(version=__version__),
        study_slug=study.slug,
        question=study.question,
        generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        rounds=report_rounds,
        best_overall=best_overall,
        stopped_reason=stopped_reason,
    )


def build_charts(study: Study, results: list[RunResult]) -> list[dict[str, str]]:
    """One chart per (primary metric, duration) for a round's ok runs."""
    param = study.parameter.name
    ok_runs = [result for result in results if result.status == RunStatus.OK]
    if not ok_runs:
        return []
    primary_points = [
        (result.params.get(param, "?"), result.metrics[study.primary_metric])
        for result in ok_runs
        if study.primary_metric in result.metrics
    ]
    best_label = None
    if primary_points:
        if study.goal == Goal.MAXIMIZE:
            best_label = max(primary_points, key=lambda item: item[1])[0]
        else:
            best_label = min(primary_points, key=lambda item: item[1])[0]
    charts: list[dict[str, str]] = []
    if primary_points:
        charts.append(
            {
                "title": f"{param} → {study.primary_metric}",
                "svg": line_chart(primary_points, title=f"{param} → {study.primary_metric}", highlight=best_label),
            }
        )
    duration_points = [(result.params.get(param, "?"), result.duration_s) for result in ok_runs]
    charts.append(
        {
            "title": f"{param} → 耗时（秒）",
            "svg": line_chart(duration_points, title=f"{param} → 耗时（秒）"),
        }
    )
    return charts


def dump_json(payload: object, path: Path) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def write_json_report(report: Report, path: Path) -> None:
    dump_json(report.model_dump(mode="json"), path)


def write_html_report(report: Report, study: Study, path: Path) -> None:
    environment = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = environment.get_template("report.html.j2")
    charts_by_round: dict[int, list[dict[str, str]]] = {
        round_data.round: build_charts(study, round_data.runs) for round_data in report.rounds
    }
    html = template.render(report=report, study=study, charts_by_round=charts_by_round)
    path.write_text(html, encoding="utf-8", newline="\n")
