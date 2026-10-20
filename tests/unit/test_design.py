from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from labloop.config import LabloopConfig
from labloop.design import build_backend
from labloop.design.backend import DesignHints
from labloop.design.heuristic import HeuristicBackend
from labloop.design.llm import LlmBackend
from labloop.domain.enums import Backend, Confidence, RunStatus, Source
from labloop.errors import DesignError, LlmError
from labloop.llm import LlmClient, extract_json, redact
from labloop.recipes.builtin import build_generic
from support import make_result, make_study


def _recipe():
    return build_generic(
        command="python train.py --batch {batch} --seed 42",
        metric_flags=["accuracy=acc:\\s*([0-9.]+)"],
        var_values={"batch": ["8", "16"]},
    )


def _client(handler, *, enabled: bool = True) -> LlmClient:
    config = LabloopConfig()
    config.llm.enabled = enabled
    config.llm.base_url = "https://llm.example/v1"
    config.llm.model = "qwen-test"
    return LlmClient(config, transport=httpx.MockTransport(handler))


def _ok_handler(payload: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-key"
        content = json.dumps(payload, ensure_ascii=False)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return handler


def _content_handler(content: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return handler


DESIGN_PAYLOAD = {
    "hypothesis": {
        "statement": "增大 batch 可提升吞吐但可能损失精度",
        "variable": "batch",
        "expected_effect": "non_monotonic",
        "rationale": "受显存与梯度噪声影响",
    },
    "parameter": {"name": "batch", "values": ["8", "16", "32"]},
    "notes": ["建议 4 个取值以内"],
}


# --- heuristic design ---------------------------------------------------------


def test_heuristic_design_requires_var() -> None:
    with pytest.raises(DesignError) as excinfo:
        HeuristicBackend().design(question="q", recipe=_recipe(), hints=DesignHints())
    assert excinfo.value.code == "VAR_REQUIRED_OFFLINE"


def test_heuristic_design_rejects_two_variables() -> None:
    hints = DesignHints(var_values={"batch": ["8"], "lr": ["0.1"]})
    with pytest.raises(DesignError) as excinfo:
        HeuristicBackend().design(question="q", recipe=_recipe(), hints=hints)
    assert excinfo.value.code == "TOO_MANY_VARIABLES"


def test_heuristic_design_rejects_oversized_matrix() -> None:
    hints = DesignHints(var_values={"batch": [str(index) for index in range(20)]}, max_runs=16)
    with pytest.raises(DesignError) as excinfo:
        HeuristicBackend().design(question="q", recipe=_recipe(), hints=hints)
    assert excinfo.value.code == "MATRIX_TOO_LARGE"


def test_heuristic_design_ok() -> None:
    hints = DesignHints(var_values={"batch": ["8", "16", "32", "64"]})
    outcome = HeuristicBackend().design(question="q", recipe=_recipe(), hints=hints)
    assert outcome.parameter.name == "batch"
    assert outcome.parameter.values == ["8", "16", "32", "64"]
    assert outcome.hypothesis.variable == "batch"
    assert outcome.hypothesis.source == Source.OFFLINE
    assert outcome.notes


# --- heuristic analyze --------------------------------------------------------


def test_heuristic_analyze_interior() -> None:
    study = make_study()
    results = [
        make_result(value, metric) for value, metric in [("8", 0.831), ("16", 0.840), ("32", 0.844), ("64", 0.838)]
    ]
    outcome = HeuristicBackend().analyze(study=study, results=results, round_no=1)
    assert outcome.best_value == "32"
    assert outcome.boundary is False
    assert outcome.next is not None
    assert outcome.next.values == ["24", "48"]
    assert outcome.confidence == Confidence.HIGH
    assert any(finding.kind == "duration" for finding in outcome.findings)
    assert "24, 48" in outcome.summary


def test_heuristic_analyze_without_usable_points() -> None:
    study = make_study()
    results = [make_result("8", None), make_result("16", 0.8, status=RunStatus.FAILED)]
    outcome = HeuristicBackend().analyze(study=study, results=results, round_no=1)
    assert outcome.best_value is None
    assert outcome.next is None
    assert outcome.confidence == Confidence.LOW


# --- LLM design ---------------------------------------------------------------


def test_llm_design_parses_payload(monkeypatch) -> None:
    monkeypatch.setenv("LABLOOP_LLM_API_KEY", "test-key")
    backend = LlmBackend(_client(_ok_handler(DESIGN_PAYLOAD)))
    outcome = backend.design(question="q", recipe=_recipe(), hints=DesignHints(max_runs=8))
    assert outcome.parameter.values == ["8", "16", "32"]
    assert outcome.hypothesis.source == Source.LLM
    assert "非单调" in outcome.hypothesis.statement or outcome.hypothesis.statement


def test_llm_design_user_var_wins(monkeypatch) -> None:
    monkeypatch.setenv("LABLOOP_LLM_API_KEY", "test-key")
    backend = LlmBackend(_client(_ok_handler(DESIGN_PAYLOAD)))
    hints = DesignHints(var_values={"batch": ["8", "16"]}, max_runs=8)
    outcome = backend.design(question="q", recipe=_recipe(), hints=hints)
    assert outcome.parameter.values == ["8", "16"]
    assert any("覆盖" in note for note in outcome.notes)


def test_llm_design_fenced_json(monkeypatch) -> None:
    monkeypatch.setenv("LABLOOP_LLM_API_KEY", "test-key")
    content = "```json\n" + json.dumps(DESIGN_PAYLOAD) + "\n```"
    backend = LlmBackend(_client(_content_handler(content)))
    outcome = backend.design(question="q", recipe=_recipe(), hints=DesignHints(max_runs=8))
    assert outcome.parameter.values == ["8", "16", "32"]


def test_llm_design_garbage_falls_back_to_heuristic(monkeypatch) -> None:
    monkeypatch.setenv("LABLOOP_LLM_API_KEY", "test-key")
    backend = LlmBackend(_client(_content_handler("这不是 JSON")), fallback=HeuristicBackend())
    hints = DesignHints(var_values={"batch": ["8", "16"]}, max_runs=8)
    outcome = backend.design(question="q", recipe=_recipe(), hints=hints)
    assert outcome.hypothesis.source == Source.OFFLINE
    assert any("LLM 降级" in note for note in outcome.notes)


def test_llm_design_oversized_output_without_fallback_raises(monkeypatch) -> None:
    monkeypatch.setenv("LABLOOP_LLM_API_KEY", "test-key")
    payload = json.loads(json.dumps(DESIGN_PAYLOAD))
    payload["parameter"]["values"] = [str(index) for index in range(20)]
    backend = LlmBackend(_client(_ok_handler(payload)))
    with pytest.raises(LlmError):
        backend.design(question="q", recipe=_recipe(), hints=DesignHints(max_runs=16))


def test_llm_design_fallback_failure_reports_both(monkeypatch) -> None:
    monkeypatch.setenv("LABLOOP_LLM_API_KEY", "test-key")
    backend = LlmBackend(_client(_content_handler("garbage")), fallback=HeuristicBackend())
    with pytest.raises(DesignError) as excinfo:
        backend.design(question="q", recipe=_recipe(), hints=DesignHints())  # 无 --var 可用
    assert "LLM 失败" in excinfo.value.message
    assert "离线降级失败" in excinfo.value.message


# --- LLM analyze --------------------------------------------------------------

ANALYZE_PAYLOAD = {
    "summary": "batch=32 最佳",
    "findings": [{"kind": "best", "detail": "batch=32 → 0.844"}],
    "best": {"param": "batch", "value": "32", "metric_value": 0.844},
    "boundary": False,
    "next": {"param": "batch", "values": ["24", "48"], "rationale": "向两侧收窄"},
    "confidence": "high",
}


def test_llm_analyze_ok(monkeypatch) -> None:
    monkeypatch.setenv("LABLOOP_LLM_API_KEY", "test-key")
    study = make_study()
    results = [make_result("32", 0.844)]
    outcome = LlmBackend(_client(_ok_handler(ANALYZE_PAYLOAD))).analyze(study=study, results=results, round_no=1)
    assert outcome.best_value == "32"
    assert outcome.next is not None and outcome.next.values == ["24", "48"]
    assert outcome.confidence == Confidence.HIGH
    assert outcome.fallback_reason is None


def test_llm_analyze_truncates_long_next(monkeypatch) -> None:
    monkeypatch.setenv("LABLOOP_LLM_API_KEY", "test-key")
    payload = json.loads(json.dumps(ANALYZE_PAYLOAD))
    payload["next"]["values"] = ["1", "2", "3", "4", "5", "6"]
    study = make_study()
    outcome = LlmBackend(_client(_ok_handler(payload))).analyze(
        study=study, results=[make_result("32", 0.8)], round_no=1
    )
    assert outcome.next is not None
    assert outcome.next.values == ["1", "2", "3", "4"]
    assert any("截断" in finding.detail for finding in outcome.findings)


def test_llm_analyze_http_error_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("LABLOOP_LLM_API_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    study = make_study()
    results = [
        make_result(value, metric) for value, metric in [("8", 0.83), ("16", 0.84), ("32", 0.844), ("64", 0.838)]
    ]
    outcome = LlmBackend(_client(handler), fallback=HeuristicBackend()).analyze(
        study=study, results=results, round_no=1
    )
    assert outcome.best_value == "32"  # 离线规则兜底
    assert outcome.fallback_reason is not None
    assert "LLM 降级" in outcome.fallback_reason


def test_llm_analyze_without_key_raises_disabled(monkeypatch) -> None:
    monkeypatch.delenv("LABLOOP_LLM_API_KEY", raising=False)
    study = make_study()
    with pytest.raises(LlmError) as excinfo:
        LlmBackend(_client(_ok_handler(ANALYZE_PAYLOAD))).analyze(
            study=study, results=[make_result("32", 0.8)], round_no=1
        )
    assert excinfo.value.code == "LLM_DISABLED"


# --- factory ------------------------------------------------------------------


def test_llm_retries_once_before_giving_up(monkeypatch) -> None:
    """瞬时失败重试 1 次（§10）：第一次失败、第二次成功。"""
    monkeypatch.setenv("LABLOOP_LLM_API_KEY", "test-key")
    calls: list[int] = []

    def flaky_handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, text="boom")
        content = json.dumps(DESIGN_PAYLOAD, ensure_ascii=False)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    backend = LlmBackend(_client(flaky_handler))
    outcome = backend.design(question="q", recipe=_recipe(), hints=DesignHints(max_runs=8))
    assert len(calls) == 2
    assert outcome.parameter.values == ["8", "16", "32"]


def test_build_backend_offline_auto_and_llm(monkeypatch) -> None:
    monkeypatch.delenv("LABLOOP_LLM_API_KEY", raising=False)
    config = LabloopConfig()
    config.llm.base_url = "https://llm.example/v1"
    config.llm.model = "qwen-test"
    client = LlmClient(config)

    backend, note = build_backend("offline", client=client)
    assert backend.backend_id == Backend.OFFLINE and note is None

    backend, note = build_backend("auto", client=client)
    assert backend.backend_id == Backend.OFFLINE
    assert note is not None and "降级" in note

    with pytest.raises(DesignError) as excinfo:
        build_backend("llm", client=client)
    assert excinfo.value.code == "LLM_DISABLED"

    monkeypatch.setenv("LABLOOP_LLM_API_KEY", "test-key")
    backend, note = build_backend("auto", client=client)
    assert backend.backend_id == Backend.LLM and note is None
    backend, note = build_backend("llm", client=client)
    assert backend.backend_id == Backend.LLM and note is None


def test_explicit_llm_backend_never_degrades(monkeypatch) -> None:
    """显式 --backend llm 失败即报错（ADR 0001 #4），不静默换成离线结果。"""
    monkeypatch.setenv("LABLOOP_LLM_API_KEY", "test-key")
    config = LabloopConfig()
    config.llm.enabled = True
    config.llm.base_url = "https://llm.example/v1"
    config.llm.model = "qwen-test"
    client = LlmClient(config, transport=httpx.MockTransport(_content_handler("这不是 JSON")))

    backend, _note = build_backend("llm", client=client)
    with pytest.raises(LlmError) as excinfo:
        backend.design(question="q", recipe=_recipe(), hints=DesignHints(var_values={"batch": ["8"]}, max_runs=8))
    assert "不降级" in excinfo.value.message

    with pytest.raises(LlmError):
        backend.analyze(study=make_study(), results=[make_result("8", 0.8)], round_no=1)


def test_auto_backend_degrades_when_llm_breaks(monkeypatch) -> None:
    """auto 模式允许降级：LLM 挂了也能出结果，但原因会被记录。"""
    monkeypatch.setenv("LABLOOP_LLM_API_KEY", "test-key")
    config = LabloopConfig()
    config.llm.enabled = True
    config.llm.base_url = "https://llm.example/v1"
    config.llm.model = "qwen-test"
    client = LlmClient(config, transport=httpx.MockTransport(_content_handler("这不是 JSON")))

    backend, _note = build_backend("auto", client=client)
    outcome = backend.design(question="q", recipe=_recipe(), hints=DesignHints(var_values={"batch": ["8"]}, max_runs=8))
    assert outcome.hypothesis.source == Source.OFFLINE
    assert any("LLM 降级" in note for note in outcome.notes)


# --- llm helpers --------------------------------------------------------------


def test_extract_json_variants() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('前置说明 {"a": 1} 后置说明') == {"a": 1}
    with pytest.raises(LlmError):
        extract_json("完全没有 JSON")
    with pytest.raises(LlmError):
        extract_json("[1, 2, 3]")


def test_redact(monkeypatch) -> None:
    monkeypatch.setenv("USERNAME", "secretuser")
    home = str(Path.home())
    text = f"路径 {home}\\proj 用户 secretuser"
    out = redact(text, user="secretuser", host="nohost")
    assert "<home>" in out and home not in out
    assert "secretuser" not in out and "<user>" in out
