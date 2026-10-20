"""LLM backend (DEVELOPMENT.md §10): strict JSON, validation, heuristics fallback."""

from __future__ import annotations

from typing import Any

from labloop.design.backend import AnalysisOutcome, DesignBackend, DesignHints, DesignOutcome
from labloop.domain.enums import Backend, Confidence, Source
from labloop.domain.models import Finding, Hypothesis, NextSuggestion, Parameter, RunResult, Study
from labloop.errors import LLM_OUTPUT_INVALID, DesignError, LlmError
from labloop.llm import LlmClient, redact
from labloop.recipes.model import Recipe

DESIGN_SYSTEM = (
    "你是实验设计助手。给定研究问题与实验配方，输出严格 JSON（不要任何额外文字）：\n"
    '{"hypothesis": {"statement": str, "variable": str, "expected_effect": str, "rationale": str}, '
    '"parameter": {"name": str, "values": [str]}, "notes": [str]}\n'
    "约束：parameter.values 数量不超过 max_runs；如果用户已经给出变量的取值列表，必须沿用这些取值。"
)

ANALYZE_SYSTEM = (
    "你是实验结果分析助手。给定本轮参数-指标表格，输出严格 JSON（不要任何额外文字）：\n"
    '{"summary": str, "findings": [{"kind": str, "detail": str}], '
    '"best": {"param": str, "value": str, "metric_value": number} | null, '
    '"boundary": bool, "next": {"param": str, "values": [str], "rationale": str} | null, '
    '"confidence": "high"|"medium"|"low"}\n'
    "约束：next.values 最多 4 个；必须是数据显示出的合理外推或收窄；没有规律就 next=null。"
)

MAX_NEXT_VALUES = 4


def _parse_design(payload: dict[str, Any], hints: DesignHints) -> tuple[Hypothesis, Parameter, list[str]]:
    try:
        raw_hypothesis = payload["hypothesis"]
        raw_parameter = payload["parameter"]
        hypothesis = Hypothesis(
            statement=str(raw_hypothesis["statement"]),
            variable=str(raw_hypothesis["variable"]),
            expected_effect=str(raw_hypothesis.get("expected_effect", "unknown")),
            rationale=str(raw_hypothesis.get("rationale", "")),
            source=Source.LLM,
        )
        name = str(raw_parameter["name"])
        values = [str(item) for item in raw_parameter["values"]]
    except (KeyError, TypeError) as exc:
        raise LlmError(f"LLM 设计输出结构不符：{exc}", code=LLM_OUTPUT_INVALID) from exc
    if not values:
        raise LlmError("LLM 设计输出没有给出任何取值", code=LLM_OUTPUT_INVALID)
    notes = [str(item) for item in payload.get("notes") or []]
    if hints.var_values:
        user_name, user_values = next(iter(hints.var_values.items()))
        if name != user_name or values != list(user_values):
            notes.append(
                f"用户 --var 优先：{user_name}={','.join(user_values)}（LLM 建议 {name}={','.join(values)} 被覆盖）"
            )
            name, values = user_name, list(user_values)
        hypothesis.variable = name
    if len(values) > hints.max_runs:
        raise LlmError(f"LLM 给出 {len(values)} 个取值，超过 --max-runs={hints.max_runs}", code=LLM_OUTPUT_INVALID)
    return hypothesis, Parameter(name=name, values=values), notes


def _parse_analysis(payload: dict[str, Any], study: Study) -> AnalysisOutcome:
    summary = str(payload.get("summary") or "").strip()
    if not summary:
        raise LlmError("LLM 分析输出缺少 summary", code=LLM_OUTPUT_INVALID)
    findings: list[Finding] = []
    for item in payload.get("findings") or []:
        if isinstance(item, dict) and item.get("detail"):
            findings.append(Finding(kind=str(item.get("kind", "note")), detail=str(item["detail"])))
    best_value: str | None = None
    best_metric: float | None = None
    raw_best = payload.get("best")
    if isinstance(raw_best, dict) and raw_best.get("value") not in (None, ""):
        best_value = str(raw_best["value"])
        try:
            best_metric = float(raw_best["metric_value"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LlmError("LLM 分析输出 best.metric_value 不是数值", code=LLM_OUTPUT_INVALID) from exc
    suggestion: NextSuggestion | None = None
    raw_next = payload.get("next")
    if isinstance(raw_next, dict):
        values = [str(item) for item in raw_next.get("values") or []]
        if len(values) > MAX_NEXT_VALUES:
            findings.append(
                Finding(kind="note", detail=f"LLM 建议 {len(values)} 个取值，已截断为 {MAX_NEXT_VALUES} 个")
            )
            values = values[:MAX_NEXT_VALUES]
        if values:
            suggestion = NextSuggestion(
                param=str(raw_next.get("param") or study.parameter.name),
                values=values,
                rationale=str(raw_next.get("rationale", "")),
            )
    confidence = Confidence.MEDIUM
    raw_confidence = str(payload.get("confidence", "medium")).lower()
    if raw_confidence in {item.value for item in Confidence}:
        confidence = Confidence(raw_confidence)
    return AnalysisOutcome(
        summary=summary,
        findings=findings,
        best_value=best_value,
        best_metric=best_metric,
        boundary=bool(payload.get("boundary", False)),
        next=suggestion,
        confidence=confidence,
    )


class LlmBackend:
    backend_id = Backend.LLM

    def __init__(self, client: LlmClient, *, fallback: DesignBackend | None = None) -> None:
        self._client = client
        self._fallback = fallback

    def _chat(self, *, system: str, user: str) -> dict[str, Any]:
        """One request, one retry (§10: transient failures get a second chance)."""
        try:
            return self._client.chat_json(system=system, user=user)
        except LlmError as first:
            try:
                return self._client.chat_json(system=system, user=user)
            except LlmError as second:
                raise second from first

    def design(self, *, question: str, recipe: Recipe, hints: DesignHints) -> DesignOutcome:
        try:
            payload = self._chat(
                system=DESIGN_SYSTEM,
                user=self._design_prompt(question, recipe, hints),
            )
            hypothesis, parameter, notes = _parse_design(payload, hints)
            return DesignOutcome(hypothesis=hypothesis, parameter=parameter, notes=notes)
        except LlmError as exc:
            return self._fallback_design(question, recipe, hints, exc)

    def _fallback_design(
        self,
        question: str,
        recipe: Recipe,
        hints: DesignHints,
        exc: LlmError,
    ) -> DesignOutcome:
        if self._fallback is None:
            raise LlmError(
                f"{exc.message}（显式 LLM 后端不降级：改用 --backend auto 可在失败时自动降级到离线）",
                code=exc.code,
                details=exc.details,
            ) from exc
        try:
            outcome = self._fallback.design(question=question, recipe=recipe, hints=hints)
        except DesignError as design_exc:
            raise DesignError(
                f"LLM 失败（{exc.message}）且离线降级失败（{design_exc.message}）",
                code=design_exc.code,
            ) from exc
        outcome.notes.append(f"LLM 降级：{exc.message}")
        return outcome

    def analyze(self, *, study: Study, results: list[RunResult], round_no: int) -> AnalysisOutcome:
        try:
            payload = self._chat(
                system=ANALYZE_SYSTEM,
                user=self._analyze_prompt(study, results, round_no),
            )
            return _parse_analysis(payload, study)
        except LlmError as exc:
            if self._fallback is None:
                raise LlmError(
                    f"{exc.message}（显式 LLM 后端不降级：改用 --backend offline 走离线规则，"
                    "或 --backend auto 自动降级）",
                    code=exc.code,
                    details=exc.details,
                ) from exc
            outcome = self._fallback.analyze(study=study, results=results, round_no=round_no)
            outcome.fallback_reason = f"LLM 降级：{exc.message}"
            return outcome

    @staticmethod
    def _design_prompt(question: str, recipe: Recipe, hints: DesignHints) -> str:
        lines = [
            f"研究问题：{redact(question)}",
            f"配方：{recipe.id}（{recipe.description}）",
            f"命令模板：{recipe.command}",
        ]
        if hints.var_values:
            joined = "; ".join(f"{name}={','.join(values)}" for name, values in hints.var_values.items())
            lines.append(f"用户已给出变量取值（必须沿用）：{joined}")
        else:
            lines.append("用户未指定变量取值：请根据问题提出不超过 4 个取值。")
        lines.append(f"单轮 run 数上限：{hints.max_runs}；主指标优化方向：{hints.goal.value}")
        return "\n".join(lines)

    @staticmethod
    def _analyze_prompt(study: Study, results: list[RunResult], round_no: int) -> str:
        lines = [
            f"研究问题：{redact(study.question)}",
            f"研究变量：{study.parameter.name}；主指标：{study.primary_metric}（{study.goal.value}）",
            f"第 {round_no} 轮结果（param_value | metric | duration_s | status）：",
        ]
        for result in results:
            metric = result.metrics.get(study.primary_metric)
            metric_text = f"{metric:g}" if metric is not None else "-"
            param_value = result.params.get(study.parameter.name, "?")
            lines.append(f"{param_value} | {metric_text} | {result.duration_s:.1f} | {result.status.value}")
        return "\n".join(lines)
