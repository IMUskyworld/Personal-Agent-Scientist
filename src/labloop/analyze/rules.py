"""Deterministic analysis rules (DEVELOPMENT.md §12.3): series, best point, next-round suggestion."""

from __future__ import annotations

from dataclasses import dataclass

from labloop.domain.enums import Goal, RunStatus
from labloop.domain.models import NextSuggestion, RunResult


@dataclass
class SeriesPoint:
    value: str
    numeric: float | None
    metric: float


def to_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def format_number(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(round(value))
    return f"{value:g}"


def build_series(results: list[RunResult], *, param: str, primary_metric: str) -> list[SeriesPoint]:
    """Collect (param value → primary metric) from ok runs only."""
    points: list[SeriesPoint] = []
    for result in results:
        if result.status != RunStatus.OK:
            continue
        if param not in result.params or primary_metric not in result.metrics:
            continue
        raw = result.params[param]
        points.append(SeriesPoint(value=raw, numeric=to_float(raw), metric=result.metrics[primary_metric]))
    return points


def pick_best(points: list[SeriesPoint], goal: Goal) -> SeriesPoint | None:
    if not points:
        return None
    if goal == Goal.MAXIMIZE:
        return max(points, key=lambda point: point.metric)
    return min(points, key=lambda point: point.metric)


def order_points(points: list[SeriesPoint]) -> list[SeriesPoint]:
    """Numeric ascending when every value parses, otherwise the original order (stable + intuitive ties)."""
    if points and all(point.numeric is not None for point in points):
        return sorted(points, key=lambda point: point.numeric or 0.0)
    return list(points)


def suggest_next(
    points: list[SeriesPoint],
    *,
    param: str,
    goal: Goal,
) -> tuple[NextSuggestion | None, bool, str]:
    """Returns (suggestion, best-is-boundary, note). Implements §12.3."""
    if len(points) < 2:
        return None, False, "有效数据点不足（<2），无法给出下一轮建议"
    if any(point.numeric is None for point in points):
        return None, False, "参数取值不是数值，无法自动外推/收窄"
    ordered = sorted(points, key=lambda point: point.numeric or 0.0)
    numeric_values: list[float] = []
    for point in ordered:
        assert point.numeric is not None  # guarded by the all-numeric check above
        numeric_values.append(point.numeric)
    metrics = [point.metric for point in ordered]
    if max(metrics) - min(metrics) < 1e-12:
        return None, False, "所有取值的指标相同，没有梯度信息可依据"
    best = pick_best(ordered, goal)
    assert best is not None
    index = ordered.index(best)
    boundary = index in (0, len(ordered) - 1)
    best_numeric = numeric_values[index]
    if boundary:
        step = min(numeric_values[i + 1] - numeric_values[i] for i in range(len(numeric_values) - 1))
        new_value = best_numeric + step if index == len(numeric_values) - 1 else best_numeric - step
        if new_value <= 0:
            return None, True, f"外推值 {format_number(new_value)} ≤ 0，停止外推"
        suggestion = NextSuggestion(
            param=param,
            values=[format_number(new_value)],
            rationale=f"最佳点在边界（{best.value}），按最小步长 {format_number(step)} 向外扩展一个点",
        )
        return suggestion, True, ""
    left = numeric_values[index - 1]
    right = numeric_values[index + 1]
    existing = {format_number(value) for value in numeric_values}
    candidates = [format_number((left + best_numeric) / 2), format_number((best_numeric + right) / 2)]
    values = [value for value in dict.fromkeys(candidates) if value not in existing]
    if not values:
        return None, False, "收窄得到的取值与已有取值重复，停止"
    suggestion = NextSuggestion(
        param=param,
        values=values,
        rationale=f"最佳点在内部（{best.value}），向两侧相邻取值收窄",
    )
    return suggestion, False, ""


def describe_trend(points: list[SeriesPoint], primary_metric: str) -> str | None:
    """Monotone / peak description for the numeric series (findings material)."""
    numeric = [point for point in points if point.numeric is not None]
    if len(numeric) < 3:
        return None
    ordered = sorted(numeric, key=lambda point: point.numeric or 0.0)
    deltas = [ordered[i + 1].metric - ordered[i].metric for i in range(len(ordered) - 1)]
    if all(abs(delta) < 1e-12 for delta in deltas):
        return None
    if all(delta > 0 for delta in deltas):
        return f"{primary_metric} 随参数单调上升（{ordered[0].value} → {ordered[-1].value}）"
    if all(delta < 0 for delta in deltas):
        return f"{primary_metric} 随参数单调下降（{ordered[0].value} → {ordered[-1].value}）"
    return f"{primary_metric} 呈非单调变化（先升后降或先降后升），峰值需在最佳点附近确认"
