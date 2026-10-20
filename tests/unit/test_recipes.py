from __future__ import annotations

import json
from pathlib import Path

import pytest

from labloop.domain.enums import MetricKind
from labloop.errors import RecipeError
from labloop.recipes.builtin import build_generic, load_builtin
from labloop.recipes.model import render_command


def test_load_builtin_yolo() -> None:
    recipe = load_builtin("yolo")
    assert recipe.id == "yolo"
    assert recipe.parameters["batch"].values == ["8", "16", "32", "64"]
    assert recipe.timeout_s == 3600
    kinds = {rule.kind for rule in recipe.metrics}
    assert kinds == {MetricKind.CSV_LAST}


def test_load_builtin_unknown(tmp_path: Path) -> None:
    with pytest.raises(RecipeError) as excinfo:
        load_builtin("nope")
    assert excinfo.value.code == "RECIPE_NOT_FOUND"


def test_load_builtin_generic_requires_flags() -> None:
    with pytest.raises(RecipeError) as excinfo:
        load_builtin("generic")
    assert excinfo.value.code == "RECIPE_NOT_FOUND"


def test_render_yolo_command_uses_run_id() -> None:
    recipe = load_builtin("yolo")
    command = render_command(recipe, {"batch": "8"}, run_id="r001-batch-8", round_no=1, slug="demo")
    assert "batch=8" in command
    assert "name=labloop-r001-batch-8" in command
    assert "{run_id}" not in command
    assert "epochs=50" in command


def test_render_missing_value_is_structured() -> None:
    recipe = load_builtin("yolo")
    recipe.fixed.pop("data")
    with pytest.raises(RecipeError) as excinfo:
        render_command(recipe, {"batch": "8"}, run_id="x", round_no=1, slug="demo")
    assert excinfo.value.code == "TEMPLATE_MISSING_VALUE"
    assert "data" in excinfo.value.details["missing"]


def test_render_literal_braces_gets_escape_hint() -> None:
    recipe = build_generic(
        command="python -c \"print({'a': 1})\" --batch {batch}",
        metric_flags=["acc=acc:([0-9.]+)"],
        var_values={"batch": ["8"]},
    )
    with pytest.raises(RecipeError) as excinfo:
        render_command(recipe, {"batch": "8"}, run_id="x", round_no=1, slug="demo")
    assert excinfo.value.code == "TEMPLATE_MISSING_VALUE"
    assert "花括号" in excinfo.value.message


def test_render_invalid_format_spec() -> None:
    recipe = build_generic(
        command="python train.py --batch {batch:z}",
        metric_flags=["acc=acc:([0-9.]+)"],
        var_values={"batch": ["8"]},
    )
    with pytest.raises(RecipeError) as excinfo:
        render_command(recipe, {"batch": "8"}, run_id="x", round_no=1, slug="demo")
    assert excinfo.value.code == "RECIPE_INVALID"


def test_build_generic_valid() -> None:
    recipe = build_generic(
        command="python train.py --batch {batch} --seed {seed}",
        metric_flags=["accuracy=acc:\\s*([0-9.]+)", "loss=loss:\\s*([0-9.]+)"],
        var_values={"batch": ["8", "16"]},
    )
    assert recipe.id == "generic"
    assert recipe.parameters["batch"].values == ["8", "16"]
    assert [rule.name for rule in recipe.metrics] == ["accuracy", "loss"]
    command = render_command(recipe, {"batch": "16", "seed": "42"}, run_id="r", round_no=1, slug="s")
    assert command == "python train.py --batch 16 --seed 42"


def test_build_generic_rejects_bad_metric_flags() -> None:
    with pytest.raises(RecipeError) as no_group:
        build_generic(command="x {batch}", metric_flags=["acc=[0-9.]+"])
    assert no_group.value.code == "RECIPE_INVALID"
    assert "捕获组" in no_group.value.message

    with pytest.raises(RecipeError) as two_groups:
        build_generic(command="x {batch}", metric_flags=["acc=(a)(b)"])
    assert two_groups.value.code == "RECIPE_INVALID"

    with pytest.raises(RecipeError) as malformed:
        build_generic(command="x {batch}", metric_flags=["no-equals-sign"])
    assert malformed.value.code == "RECIPE_INVALID"

    with pytest.raises(RecipeError) as no_metrics:
        build_generic(command="x {batch}", metric_flags=[])
    assert no_metrics.value.code == "RECIPE_INVALID"


def test_load_recipe_file(tmp_path: Path) -> None:
    from labloop.recipes.model import MetricRule, Recipe, load_recipe_file, validate_recipe

    payload = {
        "id": "custom",
        "command": "python train.py --lr {lr}",
        "parameters": {"lr": {"values": ["0.1", "0.01"]}},
        "metrics": [{"name": "acc", "kind": "json_file", "file": "out.json", "point": "final.acc"}],
    }
    path = tmp_path / "recipe.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    recipe = load_recipe_file(path)
    assert recipe.id == "custom"
    assert recipe.metrics[0].kind == MetricKind.JSON_FILE

    with pytest.raises(RecipeError) as excinfo:
        load_recipe_file(tmp_path / "missing.json")
    assert excinfo.value.code == "RECIPE_NOT_FOUND"

    bad = Recipe(id="bad", command="x", metrics=[MetricRule(name="acc", kind=MetricKind.CSV_LAST)])
    with pytest.raises(RecipeError) as excinfo:
        validate_recipe(bad)
    assert excinfo.value.code == "RECIPE_INVALID"
