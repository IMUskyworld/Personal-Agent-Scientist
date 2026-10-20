"""Study artifact persistence (DEVELOPMENT.md §7)."""

from __future__ import annotations

import json
from pathlib import Path

from labloop.domain.models import ExperimentPlan, Report, RoundAnalysis, RunResult, Study
from labloop.errors import PLAN_NOT_FOUND, STUDY_NOT_FOUND, StudyError
from labloop.paths import StudyLayout


def dump_json(payload: object, path: Path) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def load_model(path: Path, model_type):
    return model_type.model_validate(json.loads(path.read_text(encoding="utf-8")))


def save_study(layout: StudyLayout, study: Study) -> None:
    dump_json(study.model_dump(mode="json"), layout.study_json)


def load_study(layout: StudyLayout) -> Study:
    if not layout.study_json.is_file():
        raise StudyError(f"study 不存在：{layout.slug}", code=STUDY_NOT_FOUND, details={"slug": layout.slug})
    return load_model(layout.study_json, Study)


def save_recipe(layout: StudyLayout, recipe) -> None:
    dump_json(recipe.model_dump(mode="json"), layout.recipe_json)


def load_recipe(layout: StudyLayout):
    from labloop.errors import RECIPE_NOT_FOUND, RecipeError
    from labloop.recipes.model import Recipe

    if not layout.recipe_json.is_file():
        raise RecipeError(
            f"study 缺少配方快照：{layout.slug}/recipe.json",
            code=RECIPE_NOT_FOUND,
            details={"slug": layout.slug},
        )
    return load_model(layout.recipe_json, Recipe)


def save_plan(layout: StudyLayout, plan: ExperimentPlan) -> None:
    dump_json(plan.model_dump(mode="json"), layout.plan_path(plan.round))


def load_plan(layout: StudyLayout, round_no: int) -> ExperimentPlan:
    path = layout.plan_path(round_no)
    if not path.is_file():
        raise StudyError(
            f"第 {round_no} 轮计划不存在：{layout.slug}",
            code=PLAN_NOT_FOUND,
            details={"slug": layout.slug, "round": round_no},
        )
    return load_model(path, ExperimentPlan)


def load_results(layout: StudyLayout, round_no: int) -> list[RunResult]:
    runs_dir = layout.runs_dir(round_no)
    results: list[RunResult] = []
    for path in sorted(runs_dir.glob("*/result.json")):
        try:
            results.append(load_model(path, RunResult))
        except (OSError, ValueError):
            continue  # 写到一半的文件视作不存在；resume 时会重跑
    return results


def save_analysis(layout: StudyLayout, analysis: RoundAnalysis) -> None:
    dump_json(analysis.model_dump(mode="json"), layout.analysis_path(analysis.round))


def load_analysis(layout: StudyLayout, round_no: int) -> RoundAnalysis | None:
    path = layout.analysis_path(round_no)
    if not path.is_file():
        return None
    return load_model(path, RoundAnalysis)


def clear_analysis(layout: StudyLayout, round_no: int) -> bool:
    """Drop a round's analysis — the measurements it described changed.

    Returns True when an analysis was actually removed.
    """
    path = layout.analysis_path(round_no)
    if not path.is_file():
        return False
    try:
        path.unlink()
    except OSError:  # pragma: no cover - best effort
        return False
    return True


def load_report(layout: StudyLayout) -> Report | None:
    if not layout.report_json_path.is_file():
        return None
    return load_model(layout.report_json_path, Report)
