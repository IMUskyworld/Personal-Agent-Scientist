"""Recipe model (DEVELOPMENT.md §9): parameter template, command rendering, metric rules."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from labloop.domain.enums import MetricKind
from labloop.errors import RECIPE_INVALID, RECIPE_NOT_FOUND, TEMPLATE_MISSING_VALUE, RecipeError

DATA_DIR = Path(__file__).resolve().parent / "data"

PLACEHOLDER_KEYS = {"run_id", "round", "slug"}


class RecipeParameter(BaseModel):
    description: str = ""
    values: list[str] = Field(default_factory=list)


class MetricRule(BaseModel):
    name: str
    kind: MetricKind
    pattern: str | None = None
    file: str | None = None
    column: str | None = None
    point: str | None = None


class Recipe(BaseModel):
    id: str
    description: str = ""
    command: str
    parameters: dict[str, RecipeParameter] = Field(default_factory=dict)
    fixed: dict[str, str] = Field(default_factory=dict)
    metrics: list[MetricRule] = Field(default_factory=list)
    timeout_s: float | None = None


def validate_recipe(recipe: Recipe) -> Recipe:
    errors: list[str] = []
    if not recipe.command.strip():
        errors.append("command 为空")
    for name, rule in ((rule.name, rule) for rule in recipe.metrics):
        if rule.kind == MetricKind.STDOUT_REGEX and not rule.pattern:
            errors.append(f"指标 {name}: stdout_regex 需要 pattern")
        if rule.kind == MetricKind.CSV_LAST and (not rule.file or not rule.column):
            errors.append(f"指标 {name}: csv_last 需要 file 与 column")
        if rule.kind == MetricKind.JSON_FILE and (not rule.file or not rule.point):
            errors.append(f"指标 {name}: json_file 需要 file 与 point")
    if errors:
        raise RecipeError("配方无效：" + "；".join(errors), code=RECIPE_INVALID, details={"recipe": recipe.id})
    return recipe


def render_command(recipe: Recipe, params: dict[str, str], *, run_id: str, round_no: int, slug: str) -> str:
    """``{param}`` / ``{run_id}`` / ``{round}`` / ``{slug}`` placeholders → final command."""
    merged: dict[str, str] = {}
    merged.update(recipe.fixed)
    merged.update(params)
    merged.update({"run_id": run_id, "round": str(round_no), "slug": slug})
    try:
        return recipe.command.format(**merged)
    except KeyError as exc:
        missing = sorted(str(key).strip("'") for key in exc.args)
        raise RecipeError(
            f"命令模板缺少取值：{', '.join(missing)}（用 --fixed 或 --var 提供）；"
            "如果这是字面量花括号，需要写成 {{ 与 }} 的转义形式",
            code=TEMPLATE_MISSING_VALUE,
            details={"recipe": recipe.id, "missing": missing},
        ) from exc
    except (IndexError, ValueError) as exc:
        raise RecipeError(
            f"命令模板渲染失败：{type(exc).__name__}: {exc}（字面量花括号需写成 {{{{ }}}}）",
            code=RECIPE_INVALID,
            details={"recipe": recipe.id},
        ) from exc


def recipe_from_mapping(payload: dict[str, Any], *, origin: str) -> Recipe:
    try:
        recipe = Recipe.model_validate(payload)
    except ValidationError as exc:
        raise RecipeError(
            f"配方解析失败：{origin}",
            code=RECIPE_INVALID,
            details={"origin": origin, "reason": exc.errors()[:5]},
        ) from exc
    return validate_recipe(recipe)


def load_recipe_file(path: Path) -> Recipe:
    import json

    if not path.is_file():
        raise RecipeError(f"配方文件不存在：{path}", code=RECIPE_NOT_FOUND, details={"path": str(path)})
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecipeError(
            f"配方文件读取失败：{path}",
            code=RECIPE_INVALID,
            details={"path": str(path), "reason": f"{type(exc).__name__}: {exc}"},
        ) from exc
    return recipe_from_mapping(payload, origin=str(path))
