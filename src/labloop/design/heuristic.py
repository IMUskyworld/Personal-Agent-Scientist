"""Offline heuristic backend (DEVELOPMENT.md §10): template hypothesis + grid matrix + §12.3 rules."""

from __future__ import annotations

from labloop.analyze.rules import build_series, describe_trend, order_points, pick_best, suggest_next
from labloop.design.backend import AnalysisOutcome, DesignHints, DesignOutcome
from labloop.domain.enums import Backend, Confidence, RunStatus, Source
from labloop.domain.models import Finding, Hypothesis, Parameter, RunResult, Study
from labloop.errors import MATRIX_TOO_LARGE, TOO_MANY_VARIABLES, VAR_REQUIRED_OFFLINE, DesignError
from labloop.recipes.model import Recipe


class HeuristicBackend:
    """Deterministic, dependency-free backend; also the LLM fallback target."""

    backend_id = Backend.OFFLINE

    def design(self, *, question: str, recipe: Recipe, hints: DesignHints) -> DesignOutcome:
        if len(hints.var_values) > 1:
            raise DesignError(
                f"MVP 仅支持一个研究变量，收到 {len(hints.var_values)} 个：{', '.join(hints.var_values)}",
                code=TOO_MANY_VARIABLES,
                details={"variables": sorted(hints.var_values)},
            )
        if not hints.var_values:
            raise DesignError(
                "离线后端需要 --var name=v1,v2,… 提供实验矩阵（或配置 LLM 后端：--backend llm）",
                code=VAR_REQUIRED_OFFLINE,
            )
        name, values = next(iter(hints.var_values.items()))
        if len(values) > hints.max_runs:
            raise DesignError(
                f"矩阵规模 {len(values)} 超过上限 --max-runs={hints.max_runs}",
                code=MATRIX_TOO_LARGE,
                details={"values": len(values), "max_runs": hints.max_runs},
            )
        del question, recipe
        hypothesis = Hypothesis(
            statement=f"探索 {name} ∈ {', '.join(values)} 对主指标的影响",
            variable=name,
            expected_effect="unknown",
            rationale="离线后端：网格取值 + 主指标对比（无 LLM）",
            source=Source.OFFLINE,
        )
        return DesignOutcome(
            hypothesis=hypothesis,
            parameter=Parameter(name=name, values=list(values)),
            notes=[f"离线矩阵：{len(values)} 个取值，单变量"],
        )

    def analyze(self, *, study: Study, results: list[RunResult], round_no: int) -> AnalysisOutcome:
        del round_no
        param = study.parameter.name
        ok_runs = [result for result in results if result.status == RunStatus.OK]
        points = build_series(results, param=param, primary_metric=study.primary_metric)
        if not points:
            return AnalysisOutcome(
                summary="本轮没有可用的结果（无 ok run 或主指标未解析成功）",
                findings=[Finding(kind="gap", detail=f"{len(results)} 个 run 中没有可比较的 {study.primary_metric}")],
                confidence=Confidence.LOW,
            )
        ordered = order_points(points)
        best = pick_best(ordered, study.goal)
        assert best is not None
        suggestion, boundary, note = suggest_next(ordered, param=param, goal=study.goal)
        findings: list[Finding] = [
            Finding(
                kind="best",
                detail=f"{param}={best.value} → {study.primary_metric}={best.metric:g}（{study.goal.value}）",
            )
        ]
        trend = describe_trend(points, study.primary_metric)
        if trend:
            findings.append(Finding(kind="trend", detail=trend))
        durations = [(result.params.get(param, "?"), result.duration_s) for result in ok_runs]
        if len(durations) >= 2:
            fastest = min(durations, key=lambda item: item[1])
            findings.append(Finding(kind="duration", detail=f"最快：{param}={fastest[0]}（{fastest[1]:.1f}s）"))
        summary = f"{param}={best.value} 的 {study.primary_metric} 最佳（{best.metric:g}）"
        summary += "；最佳点在边界" if boundary else "；最佳点在内部区间"
        if suggestion:
            summary += f"；建议下一轮 {param} ∈ {', '.join(suggestion.values)}"
        elif note:
            summary += f"；无下一轮建议（{note}）"
        confidence = Confidence.LOW
        if len(points) >= 4:
            confidence = Confidence.HIGH
        elif len(points) >= 3:
            confidence = Confidence.MEDIUM
        return AnalysisOutcome(
            summary=summary,
            findings=findings,
            best_value=best.value,
            best_metric=best.metric,
            boundary=boundary,
            next=suggestion,
            confidence=confidence,
        )
