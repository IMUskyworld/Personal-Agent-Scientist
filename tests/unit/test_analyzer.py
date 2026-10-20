from __future__ import annotations

import json

import httpx

from labloop.analyze.analyzer import analyze_round, check_comparability
from labloop.config import LabloopConfig
from labloop.design.heuristic import HeuristicBackend
from labloop.design.llm import LlmBackend
from labloop.domain.enums import Backend, Confidence, RunStatus
from labloop.llm import LlmClient
from support import make_env, make_result, make_study


def test_comparability_ok_with_identical_envs() -> None:
    results = [make_result(value, 0.8) for value in ["8", "16"]]
    comparability = check_comparability(results, primary_metric="accuracy")
    assert comparability.ok is True
    assert comparability.issues == []


def test_comparability_flags_env_drift() -> None:
    results = [
        make_result("8", 0.8, env=make_env(packages_hash="aaa111")),
        make_result("16", 0.82, env=make_env(packages_hash="bbb222")),
    ]
    comparability = check_comparability(results, primary_metric="accuracy")
    assert comparability.ok is False
    assert any("包指纹不一致" in issue for issue in comparability.issues)


def test_comparability_flags_gpu_drift_and_too_few_runs() -> None:
    results = [
        make_result("8", 0.8, env=make_env(gpu=("RTX 3090", "551.61"))),
        make_result("16", 0.81, env=make_env(gpu=("RTX 4090", "551.61"))),
    ]
    comparability = check_comparability(results, primary_metric="accuracy")
    assert any("GPU 不一致" in issue for issue in comparability.issues)

    single = check_comparability([make_result("8", 0.8)], primary_metric="accuracy")
    assert any("不足" in issue for issue in single.issues)


def test_comparability_flags_missing_primary_metric() -> None:
    """两个 ok run，但只有一个解析出主指标 → 不可比较（§12.2）。"""
    results = [make_result("8", 0.8), make_result("16", None)]
    comparability = check_comparability(results, primary_metric="accuracy")
    assert comparability.ok is False
    assert any("主指标" in issue for issue in comparability.issues)


def test_comparability_accepts_two_parsed_runs() -> None:
    results = [make_result("8", 0.8), make_result("16", None), make_result("32", 0.9)]
    comparability = check_comparability(results, primary_metric="accuracy")
    assert comparability.ok is True


def test_comparability_flags_fixed_context_drift() -> None:
    """固定上下文（fixed/配方）不一致 → 不可比较（§12.2 第一条）。"""
    first = make_result("8", 0.8)
    second = make_result("16", 0.82)
    first.fixed = {"seed": "42"}
    second.fixed = {"seed": "7"}
    first.recipe_id = "generic"
    second.recipe_id = "generic"
    comparability = check_comparability([first, second], primary_metric="accuracy")
    assert comparability.ok is False
    assert any("固定上下文" in issue for issue in comparability.issues)

    same = [make_result("8", 0.8), make_result("16", 0.82)]
    for result in same:
        result.fixed = {"seed": "42"}
        result.recipe_id = "generic"
    assert check_comparability(same, primary_metric="accuracy").ok is True


def test_comparability_flags_recipe_drift() -> None:
    first = make_result("8", 0.8)
    second = make_result("16", 0.82)
    first.fixed = {"seed": "42"}
    second.fixed = {"seed": "42"}
    first.recipe_id = "generic"
    second.recipe_id = "yolo"
    comparability = check_comparability([first, second], primary_metric="accuracy")
    assert comparability.ok is False
    assert any("配方不一致" in issue for issue in comparability.issues)


def test_analyze_round_heuristic_and_confidence_downgrade() -> None:
    study = make_study()
    results = [
        make_result(value, metric) for value, metric in [("8", 0.831), ("16", 0.84), ("32", 0.844), ("64", 0.838)]
    ]
    analysis = analyze_round(study=study, results=results, round_no=1, backend=HeuristicBackend())
    assert analysis.generated_by == Backend.OFFLINE
    assert analysis.best is not None and analysis.best.value == "32"
    assert analysis.confidence == Confidence.HIGH
    assert analysis.comparability.ok is True
    assert analysis.next is not None and analysis.next.values == ["24", "48"]

    drifted = [
        make_result("8", 0.831, env=make_env(packages_hash="aaa111")),
        make_result("16", 0.84, env=make_env(packages_hash="bbb222")),
        make_result("32", 0.844, env=make_env(packages_hash="bbb222")),
        make_result("64", 0.838, env=make_env(packages_hash="bbb222")),
    ]
    analysis_drifted = analyze_round(study=study, results=drifted, round_no=1, backend=HeuristicBackend())
    assert analysis_drifted.comparability.ok is False
    assert analysis_drifted.confidence == Confidence.MEDIUM  # high → medium


def test_analyze_round_llm_fallback_reason_propagates(monkeypatch) -> None:
    monkeypatch.setenv("LABLOOP_LLM_API_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"unexpected": True})}}]})

    config = LabloopConfig()
    config.llm.enabled = True
    config.llm.base_url = "https://llm.example/v1"
    config.llm.model = "qwen-test"
    backend = LlmBackend(LlmClient(config, transport=httpx.MockTransport(handler)), fallback=HeuristicBackend())
    study = make_study()
    results = [
        make_result(value, metric) for value, metric in [("8", 0.83), ("16", 0.84), ("32", 0.844), ("64", 0.838)]
    ]
    analysis = analyze_round(study=study, results=results, round_no=1, backend=backend)
    assert analysis.generated_by == Backend.LLM
    assert analysis.fallback_reason is not None
    assert "LLM 降级" in analysis.fallback_reason
    assert analysis.best is not None and analysis.best.value == "32"


def test_llm_best_point_is_reconciled_with_the_data(monkeypatch) -> None:
    """LLM 说的最佳点与实测不符 → 按实测修正，并留下 finding。"""
    monkeypatch.setenv("LABLOOP_LLM_API_KEY", "test-key")
    payload = {
        "summary": "batch=64 最佳",
        "best": {"param": "batch", "value": "64", "metric_value": 0.99},
        "next": {"param": "batch", "values": ["128"], "rationale": "外推"},
        "confidence": "high",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        content = json.dumps(payload, ensure_ascii=False)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    config = LabloopConfig()
    config.llm.enabled = True
    config.llm.base_url = "https://llm.example/v1"
    config.llm.model = "qwen-test"
    backend = LlmBackend(LlmClient(config, transport=httpx.MockTransport(handler)), fallback=HeuristicBackend())
    study = make_study()
    results = [
        make_result(value, metric) for value, metric in [("8", 0.83), ("16", 0.84), ("32", 0.844), ("64", 0.838)]
    ]
    analysis = analyze_round(study=study, results=results, round_no=1, backend=backend)
    assert analysis.best is not None
    assert analysis.best.value == "32"  # 实测最佳，不是 LLM 说的 64
    assert analysis.best.metric_value == 0.844
    assert any("修正" in finding.detail for finding in analysis.findings)


def test_analysis_without_usable_data_has_no_best(tmp_path) -> None:
    study = make_study()
    results = [make_result("8", None, status=RunStatus.FAILED)]
    analysis = analyze_round(study=study, results=results, round_no=1, backend=HeuristicBackend())
    assert analysis.best is None
    assert analysis.boundary is False


def test_llm_cannot_switch_the_research_variable(monkeypatch) -> None:
    """LLM 建议了另一个变量 → 忽略并回退离线规则（§1：不许装懂）。"""
    monkeypatch.setenv("LABLOOP_LLM_API_KEY", "test-key")
    payload = {
        "summary": "batch=32 最佳",
        "best": {"param": "batch", "value": "32", "metric_value": 0.844},
        "next": {"param": "lr", "values": ["0.01"], "rationale": "换个变量试试"},
        "confidence": "high",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        content = json.dumps(payload, ensure_ascii=False)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    config = LabloopConfig()
    config.llm.enabled = True
    config.llm.base_url = "https://llm.example/v1"
    config.llm.model = "qwen-test"
    backend = LlmBackend(LlmClient(config, transport=httpx.MockTransport(handler)), fallback=HeuristicBackend())
    study = make_study()
    results = [
        make_result(value, metric) for value, metric in [("8", 0.83), ("16", 0.84), ("32", 0.844), ("64", 0.838)]
    ]
    analysis = analyze_round(study=study, results=results, round_no=1, backend=backend)
    assert analysis.next is not None
    assert analysis.next.param == "batch"  # 研究变量没被换掉
    assert analysis.next.values == ["24", "48"]  # 离线规则接管
    assert any("不一致" in finding.detail for finding in analysis.findings)
