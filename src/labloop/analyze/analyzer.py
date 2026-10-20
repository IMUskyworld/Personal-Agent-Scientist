"""Round analysis assembly (DEVELOPMENT.md §12): comparability guard + backend delegation."""

from __future__ import annotations

from labloop.analyze.rules import build_series, order_points, pick_best, suggest_next
from labloop.design.backend import AnalysisOutcome, DesignBackend
from labloop.domain.enums import Confidence, RunStatus
from labloop.domain.models import BestPoint, Comparability, Finding, NextSuggestion, RoundAnalysis, RunResult, Study

_CONFIDENCE_ORDER = [Confidence.LOW, Confidence.MEDIUM, Confidence.HIGH]


def check_comparability(results: list[RunResult], *, primary_metric: str) -> Comparability:
    """Runs are only comparable when the environment did not drift between them (§12.2)."""
    issues: list[str] = []
    ok_runs = [result for result in results if result.status == RunStatus.OK]
    envs = [result.env for result in ok_runs if result.env is not None]
    if envs:
        platforms = {env.platform for env in envs}
        if len(platforms) > 1:
            issues.append(f"平台不一致：{', '.join(sorted(platforms))}")
        pythons = {env.python for env in envs}
        if len(pythons) > 1:
            issues.append(f"Python 版本不一致：{', '.join(sorted(pythons))}")
        hashes = {env.packages_hash for env in envs}
        if len(hashes) > 1:
            issues.append(f"环境包指纹不一致（{len(hashes)} 种）：{', '.join(sorted(hashes))}")
        gpus = {env.gpu.name if env.gpu else "无" for env in envs}
        if len(gpus) > 1:
            issues.append(f"GPU 不一致：{', '.join(sorted(gpus))}")
    # 固定上下文：fixed 参数 + 配方 id 必须一致（快照缺失的老结果不参与比较）
    snapshots = [(result.fixed, result.recipe_id) for result in ok_runs if result.fixed or result.recipe_id]
    if len(snapshots) >= 2:
        fixed_sets = {tuple(sorted(item[0].items())) for item in snapshots}
        if len(fixed_sets) > 1:
            issues.append("固定上下文不一致（各 run 的 fixed 参数不同）")
        recipe_ids = {item[1] for item in snapshots if item[1]}
        if len(recipe_ids) > 1:
            issues.append(f"配方不一致：{', '.join(sorted(recipe_ids))}")
    if len(ok_runs) < 2:
        issues.append(f"可比较的 ok run 不足（{len(ok_runs)} 个）")
    parsed = [result for result in ok_runs if primary_metric in result.metrics]
    if len(parsed) < 2:
        issues.append(f"主指标 {primary_metric} 解析成功的 run 不足（{len(parsed)} 个）")
    return Comparability(ok=not issues, issues=issues)


def _downgrade(confidence: Confidence) -> Confidence:
    index = _CONFIDENCE_ORDER.index(confidence)
    return _CONFIDENCE_ORDER[max(index - 1, 0)]


def _offline_next(study: Study, results: list[RunResult]) -> NextSuggestion | None:
    """Deterministic next-round suggestion, always bound to the study's variable."""
    points = build_series(results, param=study.parameter.name, primary_metric=study.primary_metric)
    suggestion, _boundary, _note = suggest_next(points, param=study.parameter.name, goal=study.goal)
    return suggestion


def _reconcile_with_data(
    study: Study,
    results: list[RunResult],
    outcome: AnalysisOutcome,
) -> tuple[BestPoint | None, bool, list[Finding]]:
    """Check the backend's best point against the measurements (§1: never fake it).

    The best point and the boundary flag are always derived from the runs that
    actually happened; a backend claim that disagrees with the data is corrected
    and reported as a finding.
    """
    findings: list[Finding] = []
    points = build_series(results, param=study.parameter.name, primary_metric=study.primary_metric)
    if not points:
        if outcome.best_value is not None:
            findings.append(
                Finding(kind="note", detail=f"后端给出的最佳点「{outcome.best_value}」没有实测数据支撑，已忽略")
            )
        return None, False, findings
    ordered = order_points(points)
    derived = pick_best(ordered, study.goal)
    assert derived is not None
    boundary = len(ordered) >= 2 and ordered.index(derived) in (0, len(ordered) - 1)
    if outcome.best_value is not None:
        match = next((point for point in points if point.value == outcome.best_value), None)
        if match is None:
            findings.append(
                Finding(
                    kind="note",
                    detail=f"后端给出的最佳点「{outcome.best_value}」不在实测取值中，已按数据修正为 {derived.value}",
                )
            )
        elif outcome.best_metric is not None and abs(match.metric - outcome.best_metric) > 1e-9:
            findings.append(
                Finding(
                    kind="note",
                    detail=(
                        f"后端给出的最佳指标值（{outcome.best_metric:g}）与实测（{match.metric:g}）不符，已按数据修正"
                    ),
                )
            )
    if outcome.boundary != boundary:
        findings.append(Finding(kind="note", detail=f"边界判定已按实测数据修正（{boundary}）"))
    best = BestPoint(param=study.parameter.name, value=derived.value, metric_value=derived.metric)
    return best, boundary, findings


def analyze_round(
    *,
    study: Study,
    results: list[RunResult],
    round_no: int,
    backend: DesignBackend,
) -> RoundAnalysis:
    outcome = backend.analyze(study=study, results=results, round_no=round_no)
    comparability = check_comparability(results, primary_metric=study.primary_metric)
    confidence = _downgrade(outcome.confidence) if not comparability.ok else outcome.confidence
    best, boundary, data_findings = _reconcile_with_data(study, results, outcome)
    findings = list(outcome.findings) + data_findings
    suggestion = outcome.next
    if suggestion is not None and suggestion.param != study.parameter.name:
        # The LLM must not switch the research variable: the next plan would then
        # measure something else than the study declares (§1: never fake a conclusion).
        findings.append(
            Finding(
                kind="note",
                detail=(
                    f"后端建议的变量「{suggestion.param}」与研究变量「{study.parameter.name}」不一致，"
                    "已忽略该建议并改用离线规则"
                ),
            )
        )
        suggestion = _offline_next(study, results)
    return RoundAnalysis(
        round=round_no,
        generated_by=backend.backend_id,
        summary=outcome.summary,
        findings=findings,
        best=best,
        boundary=boundary,
        next=suggestion,
        confidence=confidence,
        comparability=comparability,
        fallback_reason=outcome.fallback_reason,
    )
