"""LLM paths end-to-end against a local fake OpenAI-compatible server (no external network)."""

from __future__ import annotations

import http.server
import json
import threading
from pathlib import Path

import pytest
from test_cli import design, inject_results, run_cli

DESIGN_PAYLOAD = {
    "hypothesis": {
        "statement": "增大 batch 提升吞吐但可能损失精度（来自假服务器）",
        "variable": "batch",
        "expected_effect": "non_monotonic",
        "rationale": "梯度噪声与显存权衡",
    },
    "parameter": {"name": "batch", "values": ["8", "16"]},
    "notes": ["fake server"],
}

ANALYZE_PAYLOAD = {
    "summary": "假服务器分析：batch=16 最佳",
    "findings": [{"kind": "best", "detail": "batch=16 → 0.84"}],
    "best": {"param": "batch", "value": "16", "metric_value": 0.84},
    "boundary": False,
    "next": {"param": "batch", "values": ["12", "24"], "rationale": "向两侧收窄"},
    "confidence": "high",
}


class _Handler(http.server.BaseHTTPRequestHandler):
    mode = "ok"

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        text = json.dumps(body, ensure_ascii=False)
        if "实验设计助手" in text:
            content = json.dumps(DESIGN_PAYLOAD, ensure_ascii=False)
        elif "实验结果分析助手" in text:
            content = json.dumps(ANALYZE_PAYLOAD, ensure_ascii=False)
        else:
            content = "{}"
        if type(self).mode == "garbage":
            content = "这不是 JSON"
        data = json.dumps({"choices": [{"message": {"content": content}}]}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:  # silence the access log
        pass


@pytest.fixture
def fake_llm():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    _Handler.mode = "ok"
    yield base_url
    server.shutdown()
    server.server_close()


def _llm_env(base_url: str) -> dict[str, str]:
    return {
        "LABLOOP_LLM_BASE_URL": base_url,
        "LABLOOP_LLM_MODEL": "fake-qwen",
        "LABLOOP_LLM_API_KEY": "test-key",
    }


def test_design_with_llm_backend(fake_llm, tmp_path: Path) -> None:
    code, output = design(tmp_path, "--backend", "llm", env_extra=_llm_env(fake_llm))
    assert code == 0, output
    study = json.loads((tmp_path / ".labloop" / "studies" / "batch" / "study.json").read_text(encoding="utf-8"))
    assert study["hypothesis"]["source"] == "llm"
    assert study["backend"] == "llm"


def test_design_without_var_uses_llm_values(fake_llm, tmp_path: Path) -> None:
    code, output = run_cli(
        "design",
        "batch 对精度的影响",
        "--command",
        "echo acc: 0.9 batch={batch}",
        "--metric",
        "accuracy=acc:\\s*([0-9.]+)",
        "--backend",
        "llm",
        "--workdir",
        str(tmp_path / ".labloop"),
        cwd=tmp_path,
        env_extra=_llm_env(fake_llm),
    )
    assert code == 0, output
    study = json.loads((tmp_path / ".labloop" / "studies" / "batch" / "study.json").read_text(encoding="utf-8"))
    assert study["parameter"]["values"] == ["8", "16"]


def test_design_explicit_llm_garbage_fails_loudly(fake_llm, tmp_path: Path) -> None:
    """显式 --backend llm：LLM 输出非法就报错，不静默降级（ADR 0001 #4）。"""
    _Handler.mode = "garbage"
    code, output = design(tmp_path, "--backend", "llm", env_extra=_llm_env(fake_llm))
    assert code == 3
    assert "不降级" in output
    # 失败不留半成品 study
    assert not (tmp_path / ".labloop" / "studies" / "batch" / "study.json").exists()


def test_design_auto_backend_degrades_to_offline(fake_llm, tmp_path: Path) -> None:
    """auto 模式：LLM 挂了自动降级到离线，并记录降级原因。"""
    _Handler.mode = "garbage"
    code, output = design(tmp_path, "--backend", "auto", env_extra=_llm_env(fake_llm))
    assert code == 0, output
    study_dir = tmp_path / ".labloop" / "studies" / "batch"
    study = json.loads((study_dir / "study.json").read_text(encoding="utf-8"))
    assert study["hypothesis"]["source"] == "offline"
    # study 记录的是「真正产出假设的后端」：降级后就是 offline
    assert study["backend"] == "offline"
    plan = json.loads((study_dir / "rounds" / "round-001" / "plan.json").read_text(encoding="utf-8"))
    assert any("LLM 降级" in note for note in plan["notes"])
    # 降级原因要让人看得见
    assert "LLM 降级" in output


def test_analyze_with_llm_backend(fake_llm, tmp_path: Path) -> None:
    design(tmp_path, "--backend", "offline", env_extra=_llm_env(fake_llm))
    inject_results(tmp_path, "batch", 1, [("8", 0.83), ("16", 0.84), ("32", 0.838)])
    code, output = run_cli(
        "analyze",
        "batch",
        "--backend",
        "llm",
        "--workdir",
        str(tmp_path / ".labloop"),
        cwd=tmp_path,
        env_extra=_llm_env(fake_llm),
    )
    assert code == 0, output
    analysis = json.loads(
        (tmp_path / ".labloop" / "studies" / "batch" / "rounds" / "round-001" / "analysis.json").read_text(
            encoding="utf-8"
        )
    )
    assert analysis["generated_by"] == "llm"
    assert analysis["next"]["values"] == ["12", "24"]
    assert analysis["fallback_reason"] is None


def test_analyze_explicit_llm_garbage_fails_loudly(fake_llm, tmp_path: Path) -> None:
    """显式 --backend llm：分析失败即报错，并给出可操作的替代方案。"""
    design(tmp_path, "--backend", "offline", env_extra=_llm_env(fake_llm))
    inject_results(tmp_path, "batch", 1, [("8", 0.83), ("16", 0.84), ("32", 0.838)])
    _Handler.mode = "garbage"
    code, output = run_cli(
        "analyze",
        "batch",
        "--backend",
        "llm",
        "--workdir",
        str(tmp_path / ".labloop"),
        cwd=tmp_path,
        env_extra=_llm_env(fake_llm),
    )
    assert code == 3
    assert "不降级" in output
    assert not (tmp_path / ".labloop" / "studies" / "batch" / "rounds" / "round-001" / "analysis.json").exists()


def test_auto_loop_degrades_when_llm_breaks(fake_llm, tmp_path: Path) -> None:
    """auto 循环沿用 study 的 llm 后端：LLM 掉线时降级，但不丢已跑完的实验。"""
    code, output = design(tmp_path, "--backend", "llm", env_extra=_llm_env(fake_llm))
    assert code == 0, output
    _Handler.mode = "garbage"
    code, output = run_cli(
        "auto",
        "batch",
        "--yes",
        "--max-rounds",
        "1",
        "--workdir",
        str(tmp_path / ".labloop"),
        cwd=tmp_path,
        env_extra=_llm_env(fake_llm),
    )
    assert code == 0, output
    study_dir = tmp_path / ".labloop" / "studies" / "batch"
    analysis = json.loads((study_dir / "rounds" / "round-001" / "analysis.json").read_text(encoding="utf-8"))
    assert analysis["fallback_reason"] is not None
    assert "LLM 降级" in analysis["fallback_reason"]
    report = json.loads((study_dir / "report.json").read_text(encoding="utf-8"))
    assert len(report["rounds"]) == 1  # 报告照常生成


def test_analyze_auto_backend_degrades_with_reason(fake_llm, tmp_path: Path) -> None:
    """auto 模式：LLM 失败降级到离线规则，原因写进 analysis。"""
    design(tmp_path, "--backend", "offline", env_extra=_llm_env(fake_llm))
    inject_results(tmp_path, "batch", 1, [("8", 0.83), ("16", 0.84), ("32", 0.838)])
    _Handler.mode = "garbage"
    code, output = run_cli(
        "analyze",
        "batch",
        "--backend",
        "auto",
        "--workdir",
        str(tmp_path / ".labloop"),
        cwd=tmp_path,
        env_extra=_llm_env(fake_llm),
    )
    assert code == 0, output
    analysis = json.loads(
        (tmp_path / ".labloop" / "studies" / "batch" / "rounds" / "round-001" / "analysis.json").read_text(
            encoding="utf-8"
        )
    )
    assert analysis["fallback_reason"] is not None
    assert "LLM 降级" in analysis["fallback_reason"]
    assert analysis["best"]["value"] == "16"  # 离线规则兜底
