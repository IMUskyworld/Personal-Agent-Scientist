"""Console rendering (DEVELOPMENT.md §13.1)."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from labloop.analyze.rules import to_float
from labloop.domain.enums import RunStatus
from labloop.domain.models import ExperimentPlan, Report, RoundAnalysis, RunResult, Study

STATUS_DISPLAY: dict[RunStatus, tuple[str, str]] = {
    RunStatus.OK: ("✓", "green"),
    RunStatus.FAILED: ("✗", "red"),
    RunStatus.TIMEOUT: ("~", "yellow"),
    RunStatus.INTERRUPTED: ("!", "yellow"),
}

STATUS_ASCII: dict[RunStatus, str] = {
    RunStatus.OK: "OK",
    RunStatus.FAILED: "FAIL",
    RunStatus.TIMEOUT: "TIME",
    RunStatus.INTERRUPTED: "INTR",
}


def _unicode_ok(console: Console) -> bool:
    encoding = getattr(getattr(console, "file", None), "encoding", None) or "utf-8"
    try:
        "✓✗→∈".encode(encoding)
        return True
    except (LookupError, UnicodeEncodeError):
        return False


def render_study_brief(console: Console, study: Study) -> None:
    console.print(f"[bold]研究问题[/] {study.question}")
    console.print(
        f"[dim]study[/] {study.slug} · [dim]配方[/] {study.recipe_id} · [dim]后端[/] {study.backend.value} · "
        f"[dim]目标[/] {study.goal.value} {study.primary_metric}"
    )
    console.print(f"[dim]变量[/] {study.parameter.name} ∈ {', '.join(study.parameter.values)}")
    console.print(f"[dim]实验目录[/] {study.cwd}")
    console.print(f"[dim]假设[/] {study.hypothesis.statement} [dim]（{study.hypothesis.source.value}）[/]")


def render_plan(console: Console, plan: ExperimentPlan) -> None:
    table = Table(title=f"第 {plan.round} 轮计划（{len(plan.runs)} 个 run）", title_justify="left", show_edge=False)
    table.add_column("run", no_wrap=True)
    table.add_column("参数", no_wrap=True)
    table.add_column("命令")
    for spec in plan.runs:
        params = ", ".join(f"{key}={value}" for key, value in spec.params.items())
        table.add_row(spec.id, params, f"[dim]{spec.command}[/]")
    console.print(table)
    for note in plan.notes:
        console.print(f"[dim]· {note}[/]")


def _ordered_by_param(study: Study, results: list[RunResult]) -> list[RunResult]:
    """Sort runs by the parameter value (numeric when possible) for readable tables."""
    param = study.parameter.name
    values = [to_float(result.params.get(param, "")) for result in results]
    if results and all(value is not None for value in values):
        return [result for _value, result in sorted(zip(values, results, strict=True), key=lambda pair: pair[0] or 0.0)]
    return list(results)


def render_results(console: Console, study: Study, results: list[RunResult]) -> None:
    unicode_ok = _unicode_ok(console)
    table = Table(title="执行结果", title_justify="left", show_edge=False)
    table.add_column("run", no_wrap=True)
    table.add_column(study.parameter.name, no_wrap=True)
    table.add_column(study.primary_metric, justify="right", no_wrap=True)
    table.add_column("耗时", justify="right", no_wrap=True)
    table.add_column("状态", no_wrap=True)
    table.add_column("指标", overflow="fold")
    for result in _ordered_by_param(study, results):
        glyph, style = STATUS_DISPLAY[result.status]
        if not unicode_ok:
            glyph = STATUS_ASCII[result.status]
        metric = result.metrics.get(study.primary_metric)
        metric_text = f"{metric:.4g}" if metric is not None else "—"
        others = "、".join(
            f"{name}={value:.4g}" for name, value in result.metrics.items() if name != study.primary_metric
        )
        table.add_row(
            result.run_id,
            result.params.get(study.parameter.name, "?"),
            metric_text,
            f"{result.duration_s:.1f}s",
            f"[{style}]{glyph}[/]",
            f"[dim]{others or '—'}[/]",
        )
    console.print(table)


def render_analysis(console: Console, analysis: RoundAnalysis) -> None:
    console.print(
        f"[bold]第 {analysis.round} 轮分析[/]（{analysis.generated_by.value}，置信度 {analysis.confidence.value}）"
    )
    console.print(f"  {analysis.summary}")
    for finding in analysis.findings:
        console.print(f"  [dim]·[/] [dim][{finding.kind}][/] {finding.detail}")
    if analysis.next is not None:
        values_text = ", ".join(analysis.next.values)
        console.print(f"  [green]下一轮建议[/]：{analysis.next.param} ∈ {values_text} —— {analysis.next.rationale}")
    if not analysis.comparability.ok:
        console.print(f"  [yellow]可比性警告：{'；'.join(analysis.comparability.issues)}[/]")
    if analysis.fallback_reason:
        console.print(f"  [yellow]{analysis.fallback_reason}[/]")


def render_report(console: Console, report: Report, *, artifacts: dict[str, str] | None = None) -> None:
    console.print()
    console.print(f"[bold]labloop 报告[/] · {report.study_slug} · [dim]{report.question}[/]")
    if report.best_overall is not None:
        console.print(
            f"[bold green]最佳[/]：{report.best_overall.param}={report.best_overall.value} → "
            f"{report.best_overall.metric_value:.4g}"
        )
    if report.stopped_reason:
        console.print(f"[dim]停止原因：{report.stopped_reason}[/]")
    for round_data in report.rounds:
        ok_count = sum(1 for result in round_data.runs if result.status == RunStatus.OK)
        summary = round_data.analysis.summary if round_data.analysis else "（未分析）"
        console.print(f"  [dim]第 {round_data.round} 轮[/] {ok_count}/{len(round_data.runs)} ok · {summary}")
    if artifacts:
        for name, value in artifacts.items():
            console.print(f"[dim]{name}[/] {value}")
