from __future__ import annotations

from labloop.analyze.rules import build_series, describe_trend, format_number, pick_best, suggest_next, to_float
from labloop.domain.enums import Goal, RunStatus
from support import make_result


def _series(values_metrics: list[tuple[str, float]]):
    results = [make_result(value, metric) for value, metric in values_metrics]
    return build_series(results, param="batch", primary_metric="accuracy")


def test_build_series_skips_non_ok_and_missing() -> None:
    results = [
        make_result("8", 0.80),
        make_result("16", 0.84),
        make_result("32", None),
        make_result("64", 0.83, status=RunStatus.FAILED),
    ]
    points = build_series(results, param="batch", primary_metric="accuracy")
    assert [point.value for point in points] == ["8", "16"]


def test_pick_best_both_goals() -> None:
    points = _series([("8", 0.83), ("16", 0.84), ("32", 0.844), ("64", 0.838)])
    assert pick_best(points, Goal.MAXIMIZE).value == "32"
    assert pick_best(points, Goal.MINIMIZE).value == "8"
    assert pick_best([], Goal.MAXIMIZE) is None


def test_suggest_next_interior_takes_midpoints() -> None:
    points = _series([("8", 0.831), ("16", 0.840), ("32", 0.844), ("64", 0.838)])
    suggestion, boundary, note = suggest_next(points, param="batch", goal=Goal.MAXIMIZE)
    assert note == ""
    assert boundary is False
    assert suggestion is not None
    assert suggestion.values == ["24", "48"]
    assert suggestion.param == "batch"


def test_suggest_next_boundary_extends() -> None:
    points = _series([("8", 0.80), ("16", 0.83), ("32", 0.844)])
    suggestion, boundary, note = suggest_next(points, param="batch", goal=Goal.MAXIMIZE)
    assert boundary is True
    assert note == ""
    assert suggestion is not None
    assert suggestion.values == ["40"]

    points_low = _series([("24", 0.844), ("32", 0.83), ("64", 0.80)])
    suggestion_low, boundary_low, _ = suggest_next(points_low, param="batch", goal=Goal.MAXIMIZE)
    assert boundary_low is True
    assert suggestion_low is not None
    assert suggestion_low.values == ["16"]


def test_suggest_next_boundary_extrapolates_to_non_positive() -> None:
    points = _series([("8", 0.844), ("16", 0.83), ("32", 0.80)])
    suggestion, boundary, note = suggest_next(points, param="batch", goal=Goal.MAXIMIZE)
    assert boundary is True
    assert suggestion is None
    assert "≤ 0" in note


def test_suggest_next_non_numeric_and_single_and_flat() -> None:
    text_points = _series([("small", 0.8), ("large", 0.84)])
    suggestion, _, note = suggest_next(text_points, param="batch", goal=Goal.MAXIMIZE)
    assert suggestion is None
    assert "不是数值" in note

    single = _series([("8", 0.8)])
    suggestion, _, note = suggest_next(single, param="batch", goal=Goal.MAXIMIZE)
    assert suggestion is None
    assert "不足" in note

    flat = _series([("8", 0.8), ("16", 0.8), ("32", 0.8)])
    suggestion, _, note = suggest_next(flat, param="batch", goal=Goal.MAXIMIZE)
    assert suggestion is None
    assert "相同" in note


def test_describe_trend() -> None:
    rising = _series([("8", 0.8), ("16", 0.82), ("32", 0.84)])
    assert "单调上升" in (describe_trend(rising, "accuracy") or "")

    falling = _series([("8", 0.84), ("16", 0.82), ("32", 0.80)])
    assert "单调下降" in (describe_trend(falling, "accuracy") or "")

    peaked = _series([("8", 0.80), ("16", 0.84), ("32", 0.83)])
    assert "非单调" in (describe_trend(peaked, "accuracy") or "")

    assert describe_trend(_series([("8", 0.8)]), "accuracy") is None


def test_format_number_and_to_float() -> None:
    assert format_number(24.0) == "24"
    assert format_number(24.5) == "24.5"
    assert format_number(0.0001) == "0.0001"
    assert to_float("16") == 16.0
    assert to_float("abc") is None
    assert to_float(None) is None  # type: ignore[arg-type]
