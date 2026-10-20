"""CLI wiring (DEVELOPMENT.md §4): commands, confirmation gate, budgets, interrupts."""

from __future__ import annotations

import re
import sys
import time
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console

from labloop import __version__
from labloop.analyze.analyzer import analyze_round
from labloop.config import load_config
from labloop.design import build_backend
from labloop.design.backend import DesignHints
from labloop.domain.enums import Backend, Goal, RunStatus, Source
from labloop.domain.models import ExperimentPlan, NextSuggestion, RoundAnalysis, RunResult, RunSpec, Study
from labloop.errors import (
    INVALID_ARGUMENT,
    NO_SUGGESTION,
    PLAN_NOT_FOUND,
    STUDY_EXISTS,
    STUDY_NOT_FOUND,
    AmbiguousStudyError,
    LabloopError,
    StudyError,
    describe,
)
from labloop.execute.runner import Runner
from labloop.llm import LlmClient
from labloop.paths import StudyLayout, WorkLayout, resolve_workdir, slugify, validate_study_name
from labloop.recipes.builtin import build_generic, load_builtin
from labloop.recipes.model import Recipe, load_recipe_file, render_command
from labloop.report.console import render_analysis, render_plan, render_report, render_results, render_study_brief
from labloop.report.writer import build_report, write_html_report, write_json_report
from labloop.store import (
    clear_analysis,
    load_analysis,
    load_plan,
    load_recipe,
    load_results,
    load_study,
    save_analysis,
    save_plan,
    save_recipe,
    save_study,
)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="labloop — 把研究问题变成自动执行的实验循环（设计 → 执行 → 分析 → 下一轮）。",
)
console = Console()
err_console = Console(stderr=True)

EXIT_OK = 0
EXIT_RUN_FAILED = 1
EXIT_NOT_PERFORMED = 2
EXIT_INTERNAL = 3
EXIT_INTERRUPTED = 130

_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9._-]+")


@app.callback()
def _bootstrap() -> None:
    # Runs for every entry point: cp936 consoles cannot encode the Chinese output (§22).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def _fail(exc: BaseException) -> typer.Exit:
    payload = describe(exc)
    err_console.print(f"[red][{payload['code']}][/] {payload['message']}")
    if payload.get("details"):
        err_console.print(f"[dim]{payload['details']}[/]")
    return typer.Exit(EXIT_INTERNAL)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _parse_vars(pairs: list[str] | None) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for pair in pairs or []:
        name, separator, values_text = pair.partition("=")
        values = [item.strip() for item in values_text.split(",") if item.strip()]
        if not separator or not name.strip() or not values:
            raise LabloopError(
                f"--var 需要 name=v1,v2,… 形式，收到：{pair}",
                code=INVALID_ARGUMENT,
                details={"pair": pair},
            )
        key = name.strip()
        if key in result:
            raise LabloopError(
                f"--var {key} 重复给出（后者会静默覆盖前者）",
                code=INVALID_ARGUMENT,
                details={"variable": key},
            )
        result[key] = values
    return result


def _parse_fixed(pairs: list[str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for pair in pairs or []:
        name, separator, value = pair.partition("=")
        if not separator or not name.strip() or not value.strip():
            raise LabloopError(
                f"--fixed 需要 name=value 形式，收到：{pair}",
                code=INVALID_ARGUMENT,
                details={"pair": pair},
            )
        result[name.strip()] = value.strip()
    return result


def _parse_goal(value: str) -> Goal:
    try:
        return Goal(value)
    except ValueError as exc:
        raise LabloopError(
            f"--goal 只能是 maximize 或 minimize，收到：{value!r}",
            code=INVALID_ARGUMENT,
            details={"goal": value},
        ) from exc


def _positive_int(value: int, name: str) -> int:
    if value < 1:
        raise LabloopError(f"{name} 必须 ≥ 1，收到：{value}", code=INVALID_ARGUMENT, details={name: value})
    return value


def _positive_float(value: float, name: str) -> float:
    if value <= 0:
        raise LabloopError(f"{name} 必须 > 0，收到：{value}", code=INVALID_ARGUMENT, details={name: value})
    return value


def _resolve_study(workdir: str | None, name: str) -> tuple[WorkLayout, StudyLayout, Study]:
    work = WorkLayout(resolve_workdir(workdir))
    validate_study_name(name)
    try:
        layout = work.find_study(name)
    except AmbiguousStudyError as exc:
        raise StudyError(exc.message, code=exc.code, details=exc.details) from exc
    if layout is None:
        raise StudyError(f"study 不存在：{name}", code=STUDY_NOT_FOUND, details={"name": name})
    return work, layout, load_study(layout)


def _latest_round(layout: StudyLayout, explicit: int | None) -> int:
    if explicit is not None:
        return explicit
    rounds = layout.round_numbers()
    if not rounds:
        raise StudyError(f"study 还没有任何轮次：{layout.slug}", code=PLAN_NOT_FOUND, details={"slug": layout.slug})
    return rounds[-1]


def _latest_analyzed_round(layout: StudyLayout) -> int | None:
    """The newest round that has an analysis — the basis for the next plan."""
    for number in reversed(layout.round_numbers()):
        if load_analysis(layout, number) is not None:
            return number
    return None


def _sanitize(value: str) -> str:
    cleaned = _SANITIZE_RE.sub("-", value).strip("-")
    return cleaned or "value"


def _make_runs(study: Study, *, param: str, values: list[str], round_no: int, recipe: Recipe) -> list[RunSpec]:
    runs: list[RunSpec] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:  # duplicate values would collide on the same run id
            continue
        seen.add(value)
        run_id = f"r{round_no:03d}-{_sanitize(param)}-{_sanitize(value)}"
        specs = {**study.fixed, param: value, "run_id": run_id, "round": str(round_no), "slug": study.slug}
        command = render_command(recipe, specs, run_id=run_id, round_no=round_no, slug=study.slug)
        runs.append(RunSpec(id=run_id, params={param: value}, command=command, timeout_s=recipe.timeout_s))
    return runs


def _cleanup_partial_study(layout: StudyLayout) -> None:
    """Remove the artifacts of a study that failed to be created completely."""
    rounds = layout.root / "rounds"
    for path in (layout.study_json, layout.recipe_json, rounds / "round-001" / "plan.json"):
        try:
            path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - best effort cleanup
            pass
    for path in (rounds / "round-001", rounds, layout.root):
        try:
            path.rmdir()  # only removes empty directories
        except OSError:
            pass


def _pending_specs(layout: StudyLayout, plan: ExperimentPlan) -> list[RunSpec]:
    done = {result.run_id for result in load_results(layout, plan.round) if result.status == RunStatus.OK}
    return [spec for spec in plan.runs if spec.id not in done]


def _confirm_gate(study: Study, plan: ExperimentPlan, pending: list[RunSpec], yes: bool) -> tuple[bool, str]:
    console.print()
    console.print(f"[bold]即将执行第 {plan.round} 轮：{len(pending)} 个 run[/]")
    console.print(f"  实验目录  {study.cwd}")
    timeout_note = pending[0].timeout_s if pending else None
    console.print(f"  单 run 超时 {timeout_note:.0f}s" if timeout_note else "  单 run 超时 默认 1800s")
    console.print(f"  命令示例  [dim]{pending[0].command if pending else '—'}[/]")
    if yes:
        return True, ""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False, "非交互终端且未提供 --yes，已跳过执行（补上 --yes 或在本机终端重跑）"
    console.print("[yellow]⚠ 实验命令将以你的权限执行；预算/超时护栏已生效，Ctrl+C 可安全中断。[/]")
    answer = typer.prompt("继续？[y/N]", default="n", show_default=False)
    if answer.strip().lower() not in {"y", "yes"}:
        return False, "已按你的选择跳过执行"
    return True, ""


def _runner(
    layout: StudyLayout,
    study: Study,
    plan: ExperimentPlan,
    recipe: Recipe,
    *,
    timeout_s: float | None,
    redo: bool,
) -> Runner:
    return Runner(
        layout=layout,
        study=study,
        plan=plan,
        recipe=recipe,
        timeout_s=timeout_s,
        redo=redo,
        echo=console.print,
    )


def _collect_rounds(
    layout: StudyLayout,
    study: Study,
) -> list[tuple[int, ExperimentPlan | None, list[RunResult], RoundAnalysis | None]]:
    del study
    rounds: list[tuple[int, ExperimentPlan | None, list[RunResult], RoundAnalysis | None]] = []
    for round_no in layout.round_numbers():
        results = load_results(layout, round_no)
        if not results:
            continue
        try:
            plan_obj = load_plan(layout, round_no)
        except LabloopError:
            plan_obj = None
        rounds.append((round_no, plan_obj, results, load_analysis(layout, round_no)))
    return rounds


# --- commands ------------------------------------------------------------------


@app.command()
def version() -> None:
    """打印版本信息。"""
    console.print(f"labloop {__version__}")


@app.command()
def design(
    question: str = typer.Argument(..., help="研究问题（自然语言）"),
    study: str | None = typer.Option(None, "--study", help="study 名称（默认由问题生成 slug）"),
    recipe: str = typer.Option("generic", "--recipe", help="配方：yolo / generic"),
    recipe_file: Path | None = typer.Option(None, "--recipe-file", help="自定义配方 JSON"),
    command: str | None = typer.Option(None, "--command", help="generic 配方：命令模板"),
    metric: list[str] | None = typer.Option(None, "--metric", help="generic 配方：name=正则（1 个捕获组），可重复"),
    var: list[str] | None = typer.Option(None, "--var", help="研究变量 name=v1,v2,… ，可重复"),
    fixed: list[str] | None = typer.Option(None, "--fixed", help="固定上下文 name=value，可重复"),
    goal: str = typer.Option("maximize", "--goal", help="主指标方向：maximize | minimize"),
    primary_metric: str | None = typer.Option(None, "--primary-metric", help="主指标名（默认第一个）"),
    backend: str = typer.Option("auto", "--backend", help="auto | llm | offline"),
    max_runs: int | None = typer.Option(None, "--max-runs", help="单轮 run 数上限"),
    workdir: str | None = typer.Option(None, "--workdir", help="运行目录根（默认 ./.labloop）"),
    config_path: Path | None = typer.Option(None, "--config", help="配置文件路径"),
) -> None:
    """生成假设 + 第一轮实验矩阵，创建 study。"""
    try:
        config = load_config(str(config_path) if config_path else None)
        work = WorkLayout(resolve_workdir(workdir))
        var_values = _parse_vars(var)
        fixed_values = _parse_fixed(fixed)

        if recipe_file is not None:
            resolved_recipe = load_recipe_file(recipe_file)
        elif recipe == "generic":
            resolved_recipe = build_generic(
                command=command or "",
                metric_flags=list(metric or []),
                var_values=var_values,
            )
        else:
            resolved_recipe = load_builtin(recipe)
        if fixed_values:
            resolved_recipe = resolved_recipe.model_copy(
                update={"fixed": {**resolved_recipe.fixed, **fixed_values}},
            )

        hints_vars = dict(var_values)
        if not hints_vars and len(resolved_recipe.parameters) == 1:
            param_name, param_spec = next(iter(resolved_recipe.parameters.items()))
            if param_spec.values:
                hints_vars = {param_name: list(param_spec.values)}
                console.print(f"[dim]使用配方内置参数：{param_name} ∈ {', '.join(param_spec.values)}[/]")

        client = LlmClient(config)
        backend_obj, backend_note = build_backend(backend, client=client)
        if backend_note:
            console.print(f"[yellow]{backend_note}[/]")

        run_limit = config.defaults.max_runs if max_runs is None else _positive_int(max_runs, "--max-runs")
        goal_value = _parse_goal(goal)
        hints = DesignHints(var_values=hints_vars, max_runs=run_limit, goal=goal_value)
        outcome = backend_obj.design(question=question, recipe=resolved_recipe, hints=hints)

        if not resolved_recipe.metrics:
            raise LabloopError("配方没有任何指标规则，无法分析", code=INVALID_ARGUMENT)
        primary = primary_metric or resolved_recipe.metrics[0].name
        metric_names = [rule.name for rule in resolved_recipe.metrics]
        if primary not in set(metric_names):
            raise LabloopError(
                f"--primary-metric「{primary}」不在配方指标里：{', '.join(metric_names)}",
                code=INVALID_ARGUMENT,
            )
        secondary = [rule.name for rule in resolved_recipe.metrics if rule.name != primary] + ["duration_s"]

        slug = study or slugify(question, taken=work.existing_slugs())
        validate_study_name(slug)
        layout = work.study(slug)
        if layout.study_json.is_file():
            raise StudyError(f"study 已存在：{slug}（换 --study 名称或删除后重来）", code=STUDY_EXISTS)
        study_obj = Study(
            slug=slug,
            question=question,
            created_at=_now(),
            recipe_id=resolved_recipe.id,
            # 记录「真正产出这个 study 的后端」：LLM 设计失败并降级时，假设来自离线
            # 规则，study 就该记 offline，后续 analyze 默认沿用同一套方法。
            backend=Backend.LLM if outcome.hypothesis.source == Source.LLM else Backend.OFFLINE,
            goal=goal_value,
            primary_metric=primary,
            secondary_metrics=secondary,
            parameter=outcome.parameter,
            fixed=dict(resolved_recipe.fixed),
            hypothesis=outcome.hypothesis,
            cwd=str(Path.cwd()),
            runs_planned=len(outcome.parameter.values),
        )
        # Render the whole plan *before* touching the disk: a broken command template
        # must not leave a half-written study behind (§7 run directories are the only
        # thing that is allowed to stay around).
        try:
            plan_obj = ExperimentPlan(
                round=1,
                created_at=_now(),
                source=backend_obj.backend_id,
                runs=_make_runs(
                    study_obj,
                    param=outcome.parameter.name,
                    values=outcome.parameter.values,
                    round_no=1,
                    recipe=resolved_recipe,
                ),
                notes=list(outcome.notes),
            )
            save_study(layout, study_obj)
            save_recipe(layout, resolved_recipe)
            save_plan(layout, plan_obj)
        except (LabloopError, OSError):
            _cleanup_partial_study(layout)
            raise

        console.print(f"[bold green]study 已创建[/] {slug}")
        render_study_brief(console, study_obj)
        render_plan(console, plan_obj)
        console.print(f"[dim]下一步：labloop plan {slug} 预览 · labloop auto {slug} --yes 执行[/]")
    except (LabloopError, OSError) as exc:
        raise _fail(exc) from None
    raise typer.Exit(EXIT_OK)


@app.command()
def plan(
    study: str = typer.Argument(..., help="study 名称（支持唯一前缀）"),
    round_no: int | None = typer.Option(None, "--round", help="轮次（默认最新一轮）"),
    workdir: str | None = typer.Option(None, "--workdir"),
) -> None:
    """dry-run：渲染本轮命令（不执行任何东西）。"""
    try:
        _work, layout, study_obj = _resolve_study(workdir, study)
        number = _latest_round(layout, round_no)
        plan_obj = load_plan(layout, number)
        render_study_brief(console, study_obj)
        render_plan(console, plan_obj)
        pending = _pending_specs(layout, plan_obj)
        console.print(f"[dim]待执行 {len(pending)}/{len(plan_obj.runs)} 个 run（dry-run，未执行任何命令）[/]")
        console.print(f"[dim]执行：labloop run {study_obj.slug} [--yes][/]")
    except LabloopError as exc:
        raise _fail(exc) from None
    raise typer.Exit(EXIT_OK)


@app.command()
def run(
    study: str = typer.Argument(...),
    round_no: int | None = typer.Option(None, "--round"),
    yes: bool = typer.Option(False, "--yes", help="跳过交互确认"),
    max_runs: int | None = typer.Option(None, "--max-runs"),
    timeout_s: float | None = typer.Option(None, "--timeout", help="单 run 超时（秒）"),
    redo: bool = typer.Option(False, "--redo", help="重跑已完成的 run"),
    workdir: str | None = typer.Option(None, "--workdir"),
    config_path: Path | None = typer.Option(None, "--config"),
) -> None:
    """执行本轮实验（默认需要确认）。"""
    try:
        config = load_config(str(config_path) if config_path else None)
        _work, layout, study_obj = _resolve_study(workdir, study)
        number = _latest_round(layout, round_no)
        plan_obj = load_plan(layout, number)
        recipe = load_recipe(layout)
        pending = _pending_specs(layout, plan_obj)
        if not pending and not redo:
            console.print("[dim]本轮所有 run 都已完成（resume 无待执行）[/]")
            raise typer.Exit(EXIT_NOT_PERFORMED)
        # --redo 会重跑全部 run，确认门要如实显示将要执行的 run 数
        to_run = plan_obj.runs if redo else pending
        proceed, note = _confirm_gate(study_obj, plan_obj, to_run, yes)
        if not proceed:
            console.print(f"[yellow]{note}[/]")
            raise typer.Exit(EXIT_NOT_PERFORMED)
        if timeout_s is not None:
            timeout_s = _positive_float(timeout_s, "--timeout")
        if max_runs is not None:
            max_runs = _positive_int(max_runs, "--max-runs")
        effective_timeout = timeout_s if timeout_s is not None else float(config.defaults.timeout_s)
        runner = _runner(
            layout,
            study_obj,
            plan_obj,
            recipe,
            timeout_s=effective_timeout,
            redo=redo,
        )
        outcome = runner.run_round(max_runs=max_runs)
        results = load_results(layout, number)
        render_results(console, study_obj, results)
        if outcome.results and clear_analysis(layout, number):
            console.print(f"[yellow]本轮结果已更新，第 {number} 轮的旧分析作废；请重新运行 labloop analyze。[/]")
        if outcome.stopped == "max_runs":
            remaining = len(pending) - len(outcome.results)
            console.print(f"[yellow]本轮因 --max-runs 截断，{remaining} 个 run 未执行（重跑 labloop run 会续跑）。[/]")
        failed = [result for result in outcome.results if result.status != RunStatus.OK]
        if failed:
            console.print(f"[red]{len(failed)} 个 run 未成功[/]")
            raise typer.Exit(EXIT_RUN_FAILED)
        console.print(f"[green]✓ {len(outcome.results)} 个 run 完成[/]")
    except KeyboardInterrupt:
        err_console.print("[yellow]已中断（Ctrl+C）：子进程已终止，已完成结果保留，重跑将自动续跑。[/]")
        raise typer.Exit(EXIT_INTERRUPTED) from None
    except LabloopError as exc:
        raise _fail(exc) from None
    raise typer.Exit(EXIT_OK)


@app.command(name="analyze")
def analyze_command(
    study: str = typer.Argument(...),
    round_no: int | None = typer.Option(None, "--round"),
    backend: str | None = typer.Option(None, "--backend", help="覆盖 study 的后端：auto | llm | offline"),
    workdir: str | None = typer.Option(None, "--workdir"),
    config_path: Path | None = typer.Option(None, "--config"),
) -> None:
    """轮次分析 + 下一轮建议。"""
    try:
        config = load_config(str(config_path) if config_path else None)
        _work, layout, study_obj = _resolve_study(workdir, study)
        number = _latest_round(layout, round_no)
        results = load_results(layout, number)
        if not results:
            console.print(f"[yellow]第 {number} 轮还没有结果文件；先执行 labloop run {study_obj.slug}[/]")
            raise typer.Exit(EXIT_NOT_PERFORMED)
        client = LlmClient(config)
        backend_obj, note = build_backend(backend or study_obj.backend.value, client=client)
        if note:
            console.print(f"[yellow]{note}[/]")
        analysis = analyze_round(study=study_obj, results=results, round_no=number, backend=backend_obj)
        save_analysis(layout, analysis)
        render_analysis(console, analysis)
    except LabloopError as exc:
        raise _fail(exc) from None
    raise typer.Exit(EXIT_OK)


@app.command(name="next")
def next_command(
    study: str = typer.Argument(...),
    force: bool = typer.Option(False, "--force", help="覆盖已存在的下一轮计划"),
    workdir: str | None = typer.Option(None, "--workdir"),
) -> None:
    """采纳分析建议 → 生成下一轮计划。"""
    try:
        config = load_config()
        _work, layout, study_obj = _resolve_study(workdir, study)
        number = _latest_analyzed_round(layout)
        if number is None:
            console.print(f"[yellow]还没有任何轮次的分析；先跑 labloop analyze {study_obj.slug}[/]")
            raise typer.Exit(EXIT_NOT_PERFORMED)
        analysis = load_analysis(layout, number)
        if analysis is None:  # pragma: no cover - defensive
            console.print(f"[yellow]第 {number} 轮还没有分析；先跑 labloop analyze {study_obj.slug}[/]")
            raise typer.Exit(EXIT_NOT_PERFORMED)
        if analysis.next is None:
            console.print(f"[yellow]没有下一轮建议：{analysis.summary}[/]")
            raise typer.Exit(EXIT_NOT_PERFORMED)
        next_plan = _plan_from_suggestion(
            layout,
            study_obj,
            analysis.next,
            number + 1,
            max_runs=config.defaults.max_runs,
            force=force,
        )
        render_plan(console, next_plan)
        console.print(f"[dim]执行：labloop run {study_obj.slug} --yes（将在第 {next_plan.round} 轮运行）[/]")
    except LabloopError as exc:
        raise _fail(exc) from None
    raise typer.Exit(EXIT_OK)


def _plan_from_suggestion(
    layout: StudyLayout,
    study: Study,
    suggestion: NextSuggestion,
    round_no: int,
    *,
    max_runs: int | None = None,
    force: bool = False,
    reuse_existing: bool = False,
) -> ExperimentPlan:
    """Turn a next-round suggestion into a plan (never silently overwriting one)."""
    if suggestion.param != study.parameter.name:
        raise LabloopError(
            f"建议的变量「{suggestion.param}」与研究变量「{study.parameter.name}」不一致，拒绝生成下一轮计划",
            code=INVALID_ARGUMENT,
            details={"suggested": suggestion.param, "study": study.parameter.name},
        )
    values = list(dict.fromkeys(suggestion.values))
    if max_runs is not None and len(values) > max_runs:
        values = values[:max_runs]
    if not values:
        raise LabloopError("下一轮建议没有可用取值", code=NO_SUGGESTION, details={"round": round_no})
    plan_path = layout.plan_path_for(round_no)
    if plan_path.is_file():
        if reuse_existing:
            return load_plan(layout, round_no)
        if not force:
            raise LabloopError(
                f"第 {round_no} 轮计划已存在：{plan_path}（用 --force 覆盖，或先删除该文件）",
                code=INVALID_ARGUMENT,
                details={"round": round_no, "path": str(plan_path)},
            )
    recipe = load_recipe(layout)
    plan_obj = ExperimentPlan(
        round=round_no,
        created_at=_now(),
        source=study.backend,
        runs=_make_runs(study, param=suggestion.param, values=values, round_no=round_no, recipe=recipe),
        notes=[suggestion.rationale],
    )
    save_plan(layout, plan_obj)
    return plan_obj


@app.command()
def auto(
    study: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", help="跳过交互确认"),
    max_rounds: int | None = typer.Option(None, "--max-rounds", help="最大轮数（默认配置 3）"),
    budget_min: float | None = typer.Option(None, "--budget-min", help="总墙钟预算（分钟）"),
    max_runs: int | None = typer.Option(None, "--max-runs", help="单轮 run 数上限"),
    timeout_s: float | None = typer.Option(None, "--timeout", help="单 run 超时（秒）"),
    no_report: bool = typer.Option(False, "--no-report", help="结束时不生成报告"),
    backend: str | None = typer.Option(None, "--backend", help="覆盖 study 的后端：auto | llm | offline"),
    workdir: str | None = typer.Option(None, "--workdir"),
    config_path: Path | None = typer.Option(None, "--config"),
) -> None:
    """全自动循环：执行 → 分析 → 下一轮 → … → 报告。"""
    try:
        config = load_config(str(config_path) if config_path else None)
        _work, layout, study_obj = _resolve_study(workdir, study)
        recipe = load_recipe(layout)
        client = LlmClient(config)
        # 显式指定后端 → 失败即报错；沿用 study 的后端 → 允许降级（循环不该因为
        # LLM 掉线而丢掉已经跑完的实验，降级原因会写进 analysis 与报告）。
        backend_obj, note = build_backend(
            backend or study_obj.backend.value,
            client=client,
            allow_fallback=backend is None,
        )
        if note:
            console.print(f"[yellow]{note}[/]")
        deadline = time.monotonic() + budget_min * 60 if budget_min else None
        round_limit = config.defaults.max_rounds
        if max_rounds is not None:
            round_limit = _positive_int(max_rounds, "--max-rounds")
        if max_runs is not None:
            max_runs = _positive_int(max_runs, "--max-runs")
        if timeout_s is not None:
            timeout_s = _positive_float(timeout_s, "--timeout")
        if budget_min is not None:
            budget_min = _positive_float(budget_min, "--budget-min")
        current = _latest_round(layout, None)
        gate_done = False
        stopped_reason: str | None = None
        any_failed = False
        rounds_done = 0
        while True:
            plan_obj = load_plan(layout, current)
            pending = _pending_specs(layout, plan_obj)
            if pending and not gate_done:
                proceed, gate_note = _confirm_gate(study_obj, plan_obj, pending, yes)
                if not proceed:
                    console.print(f"[yellow]{gate_note}[/]")
                    raise typer.Exit(EXIT_NOT_PERFORMED)
                gate_done = True
            runner = _runner(layout, study_obj, plan_obj, recipe, timeout_s=timeout_s, redo=False)
            outcome = runner.run_round(max_runs=max_runs, deadline=deadline)
            any_failed = any_failed or any(result.status != RunStatus.OK for result in outcome.results)
            if outcome.stopped == "max_runs":
                # A truncated round must not produce a conclusion: the remaining runs
                # still belong to this round, and `next` would otherwise plan a new
                # round on top of half the data.
                remaining = len(pending) - len(outcome.results)
                stopped_reason = "max_runs"
                console.print(
                    f"[yellow]本轮因 --max-runs={max_runs} 截断，{remaining} 个 run 未执行；自动循环已停止。[/]"
                )
                console.print(f"[dim]提高 --max-runs，或重新运行 labloop auto {study_obj.slug} 续跑剩余实验。[/]")
                break
            results = load_results(layout, current)
            analysis = analyze_round(study=study_obj, results=results, round_no=current, backend=backend_obj)
            save_analysis(layout, analysis)
            render_analysis(console, analysis)
            rounds_done += 1
            if outcome.stopped == "budget" or (deadline is not None and time.monotonic() >= deadline):
                stopped_reason = "budget"
                break
            if analysis.next is None:
                stopped_reason = "no_suggestion"
                break
            if rounds_done >= round_limit:
                stopped_reason = "max_rounds"
                break
            current += 1
            _plan_from_suggestion(
                layout,
                study_obj,
                analysis.next,
                current,
                max_runs=config.defaults.max_runs,
                reuse_existing=True,
            )
        if not no_report:
            rounds = _collect_rounds(layout, study_obj)
            report_obj = build_report(study=study_obj, rounds=rounds, stopped_reason=stopped_reason)
            write_json_report(report_obj, layout.report_json_path)
            write_html_report(report_obj, study_obj, layout.report_html_path)
            render_report(
                console,
                report_obj,
                artifacts={"报告": str(layout.report_html_path), "JSON": str(layout.report_json_path)},
            )
        else:
            console.print(f"[dim]停止原因：{stopped_reason or '正常结束'}（--no-report）[/]")
        if any_failed:
            raise typer.Exit(EXIT_RUN_FAILED)
    except KeyboardInterrupt:
        err_console.print("[yellow]已中断（Ctrl+C）：子进程已终止，已完成结果保留，重跑将自动续跑。[/]")
        raise typer.Exit(EXIT_INTERRUPTED) from None
    except LabloopError as exc:
        raise _fail(exc) from None
    raise typer.Exit(EXIT_OK)


@app.command()
def report(
    study: str = typer.Argument(...),
    html_out: Path | None = typer.Option(None, "--html", help="另存 HTML 到指定路径"),
    json_out: Path | None = typer.Option(None, "--json", help="另存 JSON 到指定路径"),
    workdir: str | None = typer.Option(None, "--workdir"),
) -> None:
    """生成/刷新报告（console + report.json + report.html）。"""
    try:
        _work, layout, study_obj = _resolve_study(workdir, study)
        rounds = _collect_rounds(layout, study_obj)
        if not rounds:
            console.print("[yellow]study 还没有任何结果，无法生成报告[/]")
            raise typer.Exit(EXIT_NOT_PERFORMED)
        report_obj = build_report(study=study_obj, rounds=rounds, stopped_reason=None)
        write_json_report(report_obj, layout.report_json_path)
        write_html_report(report_obj, study_obj, layout.report_html_path)
        if html_out:
            write_html_report(report_obj, study_obj, html_out)
        if json_out:
            write_json_report(report_obj, json_out)
        render_report(
            console,
            report_obj,
            artifacts={"报告": str(layout.report_html_path), "JSON": str(layout.report_json_path)},
        )
    except LabloopError as exc:
        raise _fail(exc) from None
    raise typer.Exit(EXIT_OK)


@app.command()
def status(
    study: str = typer.Argument(...),
    workdir: str | None = typer.Option(None, "--workdir"),
) -> None:
    """查看 study 状态。"""
    try:
        _work, layout, study_obj = _resolve_study(workdir, study)
        render_study_brief(console, study_obj)
        rounds = layout.round_numbers()
        if not rounds:
            console.print("[dim]还没有运行记录[/]")
        for round_no in rounds:
            results = load_results(layout, round_no)
            ok_count = sum(1 for result in results if result.status == RunStatus.OK)
            analysis = load_analysis(layout, round_no)
            best_text = ""
            if analysis is not None and analysis.best is not None:
                best_text = f" · 最佳 {analysis.best.value}（{analysis.best.metric_value:.4g}）"
            console.print(f"  第 {round_no} 轮：{ok_count}/{len(results)} ok{best_text}")
        console.print(f"[dim]目录[/] {layout.root}")
    except LabloopError as exc:
        raise _fail(exc) from None
    raise typer.Exit(EXIT_OK)


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:  # pragma: no cover - interactive path
        err_console.print("[yellow]已中断（Ctrl+C）[/]")
        raise SystemExit(EXIT_INTERRUPTED) from None


if __name__ == "__main__":  # pragma: no cover
    main()
