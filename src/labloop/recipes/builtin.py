"""Built-in recipes: ``yolo`` (data file) and ``generic`` (built from CLI flags)."""

from __future__ import annotations

import json
import re

from labloop.domain.enums import MetricKind
from labloop.errors import RECIPE_INVALID, RECIPE_NOT_FOUND, RecipeError
from labloop.recipes.model import DATA_DIR, MetricRule, Recipe, RecipeParameter, recipe_from_mapping, validate_recipe

BUILTIN_IDS = ("yolo", "generic")
_METRIC_FLAG_RE = re.compile(r"^(?P<name>[^=]+)=(?P<pattern>.+)$")


def load_builtin(recipe_id: str) -> Recipe:
    if recipe_id == "generic":
        raise RecipeError(
            "generic 配方由 --command / --metric 动态构造，请直接提供这两个参数",
            code=RECIPE_NOT_FOUND,
            details={"recipe": recipe_id},
        )
    path = DATA_DIR / f"{recipe_id}.json"
    if not path.is_file():
        raise RecipeError(
            f"未知配方：{recipe_id}（内置：{', '.join(BUILTIN_IDS)}；或使用 --recipe-file）",
            code=RECIPE_NOT_FOUND,
            details={"recipe": recipe_id},
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    return recipe_from_mapping(payload, origin=str(path))


def build_generic(
    *,
    command: str,
    metric_flags: list[str],
    var_values: dict[str, list[str]] | None = None,
) -> Recipe:
    if not command.strip():
        raise RecipeError("generic 配方需要 --command", code=RECIPE_INVALID)
    metrics: list[MetricRule] = []
    for flag in metric_flags:
        match = _METRIC_FLAG_RE.match(flag.strip())
        if not match:
            raise RecipeError(
                f"--metric 需要 name=正则 形式，收到：{flag}",
                code=RECIPE_INVALID,
                details={"flag": flag},
            )
        name = match.group("name").strip()
        pattern = match.group("pattern").strip()
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise RecipeError(
                f"--metric 正则无效（{name}）：{exc}",
                code=RECIPE_INVALID,
                details={"flag": flag},
            ) from exc
        if compiled.groups != 1:
            raise RecipeError(
                f"--metric 正则必须恰好含 1 个捕获组（{name} 有 {compiled.groups} 个）",
                code=RECIPE_INVALID,
                details={"flag": flag},
            )
        metrics.append(MetricRule(name=name, kind=MetricKind.STDOUT_REGEX, pattern=pattern))
    if not metrics:
        raise RecipeError("generic 配方需要至少一个 --metric name=正则", code=RECIPE_INVALID)
    parameters: dict[str, RecipeParameter] = {}
    for name, values in (var_values or {}).items():
        parameters[name] = RecipeParameter(description=f"来自 --var {name}", values=list(values))
    recipe = Recipe(
        id="generic",
        description="用户命令模板",
        command=command,
        parameters=parameters,
        metrics=metrics,
    )
    return validate_recipe(recipe)
