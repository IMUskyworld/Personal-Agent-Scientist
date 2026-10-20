from __future__ import annotations

import json
import re
from pathlib import Path

from labloop.analyze.analyzer import analyze_round
from labloop.design.heuristic import HeuristicBackend
from labloop.domain.models import ExperimentPlan, RunSpec
from labloop.report.svg import line_chart
from labloop.report.writer import build_charts, build_report, write_html_report, write_json_report
from support import make_result, make_study

EXTERNAL_REF_RE = re.compile(r'(?:src|href)\s*=\s*"https?://')


def _round_data():
    study = make_study()
    plan = ExperimentPlan(
        round=1,
        created_at="2026-09-22T22:00:00+08:00",
        runs=[
            RunSpec(id=f"r001-batch-{value}", params={"batch": value}, command=f"echo {value}")
            for value in ["8", "16", "32", "64"]
        ],
    )
    durations = {"8": 12.0, "16": 10.0, "32": 8.5, "64": 7.0}
    results = [
        make_result(value, metric, duration_s=durations[value], run_id=f"r001-batch-{value}")
        for value, metric in [("8", 0.831), ("16", 0.840), ("32", 0.844), ("64", 0.838)]
    ]
    analysis = analyze_round(study=study, results=results, round_no=1, backend=HeuristicBackend())
    return study, plan, results, analysis


def test_line_chart_basic_and_highlight() -> None:
    svg = line_chart([("8", 0.83), ("16", 0.84)], title="batch → accuracy", highlight="16")
    assert "<polyline" in svg
    assert 'class="chart-best"' in svg
    assert 'class="chart-dot"' in svg
    assert "batch → accuracy" in svg

    empty = line_chart([], title="空图")
    assert "无数据" in empty

    escaped = line_chart([("<b>bad</b>", 1.0)], title="t")
    assert "<b>bad</b>" not in escaped
    assert "&lt;b&gt;" in escaped


def test_build_report_sorts_runs_and_picks_best_overall() -> None:
    study, plan, results, analysis = _round_data()
    report = build_report(study=study, rounds=[(1, plan, results, analysis)], stopped_reason="no_suggestion")
    assert report.best_overall is not None
    assert report.best_overall.value == "32"
    assert report.stopped_reason == "no_suggestion"
    assert [run.params["batch"] for run in report.rounds[0].runs] == ["8", "16", "32", "64"]


def test_build_charts_highlights_best() -> None:
    study, _plan, results, _analysis = _round_data()
    charts = build_charts(study, results)
    assert len(charts) == 2
    primary_svg = charts[0]["svg"]
    assert 'class="chart-best"' in primary_svg
    assert ">32<" in primary_svg  # 最佳点标注
    assert "耗时" in charts[1]["title"]


def test_write_json_report_roundtrip(tmp_path: Path) -> None:
    study, plan, results, analysis = _round_data()
    report = build_report(study=study, rounds=[(1, plan, results, analysis)])
    path = tmp_path / "report.json"
    write_json_report(report, path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "1.0"
    assert payload["tool"]["name"] == "labloop"
    assert payload["rounds"][0]["analysis"]["best"]["value"] == "32"
    raw = path.read_text(encoding="utf-8")
    assert "batch" in raw  # ensure_ascii=False 且内容可读


def test_html_report_self_contained_and_rendered(tmp_path: Path) -> None:
    study, plan, results, analysis = _round_data()
    report = build_report(study=study, rounds=[(1, plan, results, analysis)])
    path = tmp_path / "report.html"
    write_html_report(report, study, path)
    html = path.read_text(encoding="utf-8")
    assert html.startswith("<!DOCTYPE html>")
    assert EXTERNAL_REF_RE.search(html) is None, "HTML 必须自包含"
    assert "<svg" in html
    assert "第 1 轮" in html
    assert "下一轮建议" in html
    assert "0.844" in html


def test_html_report_escapes_hostile_content(tmp_path: Path) -> None:
    study, plan, results, analysis = _round_data()
    study.question = "<script>alert(1)</script>"
    report = build_report(study=study, rounds=[(1, plan, results, analysis)])
    path = tmp_path / "report.html"
    write_html_report(report, study, path)
    html = path.read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
