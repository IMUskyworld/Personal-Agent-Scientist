"""MVP acceptance audit for DEVELOPMENT.md §20.

Each check produces executable evidence. The only local real-subprocess check is
explicitly labelled plumbing (not a science experiment); real experiments belong
on the 3090 (see §24.3 in DEVELOPMENT.md).

Usage:
    python scripts/verify_mvp.py                # includes pytest (slow but complete)
    python scripts/verify_mvp.py --skip-tests   # when the caller already ran pytest

Exit code 0 only when nothing failed.
"""

from __future__ import annotations

import contextlib
import http.server
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# cp936 consoles cannot encode the Chinese output (§22); this also keeps the whole
# audit log UTF-8 even though check 10 runs the CLI in-process.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from labloop.analyze.analyzer import analyze_round  # noqa: E402
from labloop.config import LabloopConfig  # noqa: E402
from labloop.design.heuristic import HeuristicBackend  # noqa: E402
from labloop.design.llm import LlmBackend  # noqa: E402
from labloop.domain.enums import Backend, Confidence, Goal, RunStatus, Source  # noqa: E402
from labloop.domain.models import (  # noqa: E402
    EnvRecord,
    ExperimentPlan,
    RunResult,
    RunSpec,
    Study,
)
from labloop.execute.runner import Runner  # noqa: E402
from labloop.llm import LlmClient  # noqa: E402
from labloop.paths import StudyLayout  # noqa: E402
from labloop.recipes.builtin import build_generic  # noqa: E402
from labloop.report.writer import build_report, write_html_report, write_json_report  # noqa: E402

DEV = ROOT / "scripts" / "labloop_dev.py"
EXTERNAL_REF_RE = re.compile(r'(?:src|href)\s*=\s*"https?://')
ECHO_COMMAND = "echo acc: 0.9 batch={batch}"
ACC_METRIC = "accuracy=acc:\\s*([0-9.]+)"

RESULTS: list[tuple[str, str, str]] = []

FAKE_DESIGN = {
    "hypothesis": {"statement": "假服务器假设", "variable": "batch", "expected_effect": "unknown", "rationale": "fake"},
    "parameter": {"name": "batch", "values": ["8", "16"]},
    "notes": ["fake"],
}
FAKE_ANALYZE = {
    "summary": "假服务器分析",
    "findings": [{"kind": "best", "detail": "batch=16"}],
    "best": {"param": "batch", "value": "16", "metric_value": 0.84},
    "boundary": False,
    "next": {"param": "batch", "values": ["12", "24"], "rationale": "收窄"},
    "confidence": "high",
}


def record(status: str, name: str, detail: str) -> None:
    RESULTS.append((status, name, detail))
    print(f"{status:<4} {name:<34} {detail}")


def run_cli(*args: str, cwd: Path, env_extra: dict[str, str] | None = None) -> tuple[int, str]:
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.update(env_extra or {})
    proc = subprocess.run(
        [sys.executable, str(DEV), *args],
        cwd=str(cwd),
        capture_output=True,
        env=env,
        stdin=subprocess.DEVNULL,
    )
    output = proc.stdout.decode("utf-8", errors="replace") + proc.stderr.decode("utf-8", errors="replace")
    return proc.returncode, output


def design(cwd: Path, *extra: str, env_extra: dict[str, str] | None = None) -> tuple[int, str]:
    return run_cli(
        "design",
        "batch 对精度的影响",
        "--command",
        ECHO_COMMAND,
        "--metric",
        ACC_METRIC,
        "--var",
        "batch=8,16,32",
        "--workdir",
        str(cwd / ".labloop"),
        *extra,
        cwd=cwd,
        env_extra=env_extra,
    )


def inject_results(cwd: Path, slug: str, round_no: int, pairs: list[tuple[str, float]]) -> None:
    runs_dir = cwd / ".labloop" / "studies" / slug / "rounds" / f"round-{round_no:03d}" / "runs"
    for value, metric in pairs:
        run_id = f"r{round_no:03d}-batch-{value}"
        run_dir = runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        result = RunResult(
            run_id=run_id,
            params={"batch": value},
            status=RunStatus.OK,
            exit_code=0,
            started_at="2026-09-22T22:00:00+08:00",
            ended_at="2026-09-22T22:00:10+08:00",
            duration_s=10.0,
            metrics={"accuracy": metric},
            env=EnvRecord(platform="Windows-11", python="3.11.9", packages_hash="abc123"),
        )
        (run_dir / "result.json").write_text(
            json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class _FakeLlmHandler(http.server.BaseHTTPRequestHandler):
    mode = "ok"

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        text = json.dumps(body, ensure_ascii=False)
        if "实验设计助手" in text:
            content = json.dumps(FAKE_DESIGN, ensure_ascii=False)
        elif "实验结果分析助手" in text:
            content = json.dumps(FAKE_ANALYZE, ensure_ascii=False)
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

    def log_message(self, *args) -> None:
        pass


@contextlib.contextmanager
def fake_llm_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeLlmHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()


def llm_env(base_url: str) -> dict[str, str]:
    return {
        "LABLOOP_LLM_BASE_URL": base_url,
        "LABLOOP_LLM_MODEL": "fake-qwen",
        "LABLOOP_LLM_API_KEY": "test-key",
    }


def _synth_study(values: list[str]) -> Study:
    from labloop.domain.models import Hypothesis, Parameter

    return Study(
        slug="audit",
        question="audit",
        created_at="2026-09-22T22:00:00+08:00",
        recipe_id="generic",
        backend=Backend.OFFLINE,
        goal=Goal.MAXIMIZE,
        primary_metric="accuracy",
        secondary_metrics=["duration_s"],
        parameter=Parameter(name="batch", values=list(values)),
        fixed={"seed": "42"},
        hypothesis=Hypothesis(statement="audit", variable="batch", rationale="", source=Source.OFFLINE),
        cwd=str(Path.cwd()),
        runs_planned=len(values),
    )


def _result(
    value: str, metric: float | None, *, env: EnvRecord | None = None, status: RunStatus = RunStatus.OK
) -> RunResult:
    return RunResult(
        run_id=f"r001-batch-{value}",
        params={"batch": value},
        status=status,
        exit_code=0 if status == RunStatus.OK else 1,
        started_at="2026-09-22T22:00:00+08:00",
        ended_at="2026-09-22T22:00:10+08:00",
        duration_s=10.0,
        metrics={"accuracy": metric} if metric is not None else {},
        env=env or EnvRecord(platform="Windows-11", python="3.11.9", packages_hash="abc123"),
    )


# --- checks -------------------------------------------------------------------


def check_20_01_design_offline(tmp: Path) -> tuple[str, str]:
    code, output = design(tmp)
    if code != 0:
        return "FAIL", f"exit={code}；{output.strip()[-160:]}"
    study_dir = tmp / ".labloop" / "studies" / "batch"
    study = read_json(study_dir / "study.json")
    if study["hypothesis"]["source"] != "offline":
        return "FAIL", f"假设来源应为 offline：{study['hypothesis']['source']}"
    if study["parameter"]["values"] != ["8", "16", "32"]:
        return "FAIL", f"矩阵取值不符：{study['parameter']['values']}"
    plan = read_json(study_dir / "rounds" / "round-001" / "plan.json")
    if len(plan["runs"]) != 3:
        return "FAIL", f"矩阵 run 数不符：{len(plan['runs'])}"
    if not (study_dir / "recipe.json").is_file():
        return "FAIL", "缺少 recipe.json 快照"
    return "PASS", "假设（offline）+ 矩阵 3 取值 + 配方快照齐备"


def check_20_02_design_llm_and_fallback(tmp: Path) -> tuple[str, str]:
    with fake_llm_server() as base_url:
        code, output = design(tmp, "--backend", "llm", env_extra=llm_env(base_url))
        if code != 0:
            return "FAIL", f"LLM 设计失败 exit={code}；{output.strip()[-160:]}"
        study = read_json(tmp / ".labloop" / "studies" / "batch" / "study.json")
        if study["hypothesis"]["source"] != "llm":
            return "FAIL", f"假设来源应为 llm：{study['hypothesis']['source']}"

        # 显式 --backend llm：LLM 输出非法 → 报错，不静默降级（ADR 0001 #4）
        _FakeLlmHandler.mode = "garbage"
        try:
            code, output = design(tmp, "--backend", "llm", env_extra=llm_env(base_url))
        finally:
            _FakeLlmHandler.mode = "ok"
        if code == 0:
            return "FAIL", "显式 llm 遇到垃圾输出应报错，而不是静默降级"
        if (tmp / ".labloop" / "studies" / "batch-2" / "study.json").exists():
            return "FAIL", "失败的显式 llm 设计留下了 study"

        # auto：允许降级，但必须记录原因
        _FakeLlmHandler.mode = "garbage"
        try:
            code, output = design(tmp, "--backend", "auto", env_extra=llm_env(base_url))
        finally:
            _FakeLlmHandler.mode = "ok"
        if code != 0:
            return "FAIL", f"auto 模式应降级成功 exit={code}；{output.strip()[-160:]}"
        study2_dir = tmp / ".labloop" / "studies" / "batch-2"
        study2 = read_json(study2_dir / "study.json")
        plan2 = read_json(study2_dir / "rounds" / "round-001" / "plan.json")
        if study2["hypothesis"]["source"] != "offline":
            return "FAIL", "auto 降级后的假设来源应为 offline"
        if study2["backend"] != "offline":
            return "FAIL", f"降级后 study.backend 应记录 offline：{study2['backend']}"
        if not any("LLM 降级" in note for note in plan2["notes"]):
            return "FAIL", "降级原因未记录在计划 notes"
    return "PASS", "真 httpx 路径命中假服务器；显式 llm 垃圾输出 → 报错不降级；auto → 离线降级 + 原因记录"


def check_20_03_plan_dry_run(tmp: Path) -> tuple[str, str]:
    design(tmp)
    code, output = run_cli("plan", "batch", "--workdir", str(tmp / ".labloop"), cwd=tmp)
    if code != 0:
        return "FAIL", f"exit={code}"
    if "dry-run" not in output:
        return "FAIL", "plan 未标注 dry-run"
    study_dir = tmp / ".labloop" / "studies" / "batch"
    leftovers = list(study_dir.rglob("result.json"))
    if leftovers:
        return "FAIL", f"dry-run 产生了执行结果：{leftovers[0]}"
    if "batch=8" not in output:
        return "FAIL", "未渲染出具体命令"
    return "PASS", "渲染 3 条命令、零执行（无 result.json）"


def check_20_04_resume_skips_done(tmp: Path) -> tuple[str, str]:
    design(tmp)
    inject_results(tmp, "batch", 1, [("8", 0.9), ("16", 0.9), ("32", 0.9)])
    code, output = run_cli("run", "batch", "--yes", "--workdir", str(tmp / ".labloop"), cwd=tmp)
    if code != 2 or "都已完成" not in output:
        return "FAIL", f"已完成 run 应触发 resume 跳过（exit 2）：exit={code}；{output.strip()[-120:]}"
    return "PASS", "注入合成结果后重入 → 跳过全部已完成（--redo 路径由单元测试覆盖）"


def check_20_05_plumbing_real_subprocess(tmp: Path) -> tuple[str, str]:
    code, output = design(tmp)
    if code != 0:
        return "FAIL", f"design 失败：{output.strip()[-120:]}"
    code, output = run_cli("run", "batch", "--yes", "--workdir", str(tmp / ".labloop"), cwd=tmp)
    if code != 0:
        return "FAIL", f"真实子进程执行失败 exit={code}；{output.strip()[-160:]}"
    runs_dir = tmp / ".labloop" / "studies" / "batch" / "rounds" / "round-001" / "runs"
    run_dirs = sorted(path for path in runs_dir.iterdir() if path.is_dir())
    if len(run_dirs) != 3:
        return "FAIL", f"run 目录数不符：{len(run_dirs)}"
    for run_dir in run_dirs:
        result = read_json(run_dir / "result.json")
        if result["status"] != "ok" or result["metrics"].get("accuracy") != 0.9:
            return "FAIL", f"{run_dir.name} 结果不符：{result['status']} {result['metrics']}"
        if result["duration_s"] <= 0:
            return "FAIL", "duration_s 未记录"
        if "acc: 0.9" not in (run_dir / "stdout.log").read_text(encoding="utf-8"):
            return "FAIL", "stdout.log 未捕获输出"
    return "PASS", "真实子进程 ×3：日志/耗时/指标解析全通（管线验证，非科学实验）"


def check_20_06_analyze_offline_rules(tmp: Path) -> tuple[str, str]:
    del tmp
    backend = HeuristicBackend()

    interior = _synth_study(["8", "16", "32", "64"])
    analysis = analyze_round(
        study=interior,
        results=[_result(v, m) for v, m in [("8", 0.831), ("16", 0.840), ("32", 0.844), ("64", 0.838)]],
        round_no=1,
        backend=backend,
    )
    if analysis.next is None or analysis.next.values != ["24", "48"] or analysis.boundary:
        return "FAIL", f"内部收窄不符：{analysis.next}"

    boundary = _synth_study(["8", "16", "32"])
    analysis_b = analyze_round(
        study=boundary,
        results=[_result(v, m) for v, m in [("8", 0.80), ("16", 0.83), ("32", 0.844)]],
        round_no=1,
        backend=backend,
    )
    if analysis_b.next is None or analysis_b.next.values != ["40"] or not analysis_b.boundary:
        return "FAIL", f"边界外推不符：{analysis_b.next}"

    non_numeric = _synth_study(["small", "large"])
    analysis_n = analyze_round(
        study=non_numeric,
        results=[_result("small", 0.8), _result("large", 0.84)],
        round_no=1,
        backend=backend,
    )
    if analysis_n.next is not None or "不是数值" not in analysis_n.summary:
        return "FAIL", f"非数值应无建议：{analysis_n.next} / {analysis_n.summary}"

    single = _synth_study(["8"])
    analysis_s = analyze_round(study=single, results=[_result("8", 0.8)], round_no=1, backend=backend)
    if analysis_s.next is not None or "不足" not in analysis_s.summary:
        return "FAIL", f"单点应无建议：{analysis_s.next}"
    return "PASS", "内部收窄 [24,48] / 边界外推 [40] / 非数值与单点均无建议"


def check_20_07_analyze_llm(tmp: Path) -> tuple[str, str]:
    import httpx

    config = LabloopConfig()
    config.llm.enabled = True
    config.llm.base_url = "https://llm.example/v1"
    config.llm.model = "fake"
    os.environ["LABLOOP_LLM_API_KEY"] = "test-key"
    try:
        study = _synth_study(["8", "16", "32"])
        results = [_result(v, m) for v, m in [("8", 0.83), ("16", 0.84), ("32", 0.838)]]

        def ok_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(FAKE_ANALYZE)}}]})

        backend_ok = LlmBackend(LlmClient(config, transport=httpx.MockTransport(ok_handler)))
        analysis = analyze_round(study=study, results=results, round_no=1, backend=backend_ok)
        if analysis.generated_by != Backend.LLM or analysis.next is None or analysis.next.values != ["12", "24"]:
            return "FAIL", f"LLM 分析未生效：{analysis.generated_by} / {analysis.next}"

        def garbage_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": [{"message": {"content": "垃圾输出"}}]})

        backend_bad = LlmBackend(
            LlmClient(config, transport=httpx.MockTransport(garbage_handler)), fallback=HeuristicBackend()
        )
        analysis_bad = analyze_round(study=study, results=results, round_no=1, backend=backend_bad)
        if analysis_bad.fallback_reason is None or "LLM 降级" not in analysis_bad.fallback_reason:
            return "FAIL", "垃圾输出未触发降级"
        if analysis_bad.best is None or analysis_bad.best.value != "16":
            return "FAIL", "降级后未用离线规则兜底"

        # 显式 --backend llm 的 CLI 路径：失败必须报错，不静默降级
        with fake_llm_server() as base_url:
            _FakeLlmHandler.mode = "garbage"
            try:
                design(tmp, "--backend", "offline", env_extra=llm_env(base_url))
                inject_results(tmp, "batch", 1, [("8", 0.83), ("16", 0.84), ("32", 0.838)])
                code, _output = run_cli(
                    "analyze",
                    "batch",
                    "--backend",
                    "llm",
                    "--workdir",
                    str(tmp / ".labloop"),
                    cwd=tmp,
                    env_extra=llm_env(base_url),
                )
            finally:
                _FakeLlmHandler.mode = "ok"
            if code == 0:
                return "FAIL", "显式 llm 分析遇到垃圾输出应报错而非降级"
    finally:
        os.environ.pop("LABLOOP_LLM_API_KEY", None)
    return "PASS", "LLM 分析生效；auto 降级（fallback_reason）+ 离线兜底；显式 llm 失败即报错"


def check_20_08_comparability_guard(tmp: Path) -> tuple[str, str]:
    del tmp
    study = _synth_study(["8", "16", "32", "64"])
    results = [
        _result("8", 0.831, env=EnvRecord(platform="Windows-11", python="3.11.9", packages_hash="aaa111")),
        _result("16", 0.840, env=EnvRecord(platform="Windows-11", python="3.11.9", packages_hash="bbb222")),
        _result("32", 0.844, env=EnvRecord(platform="Windows-11", python="3.11.9", packages_hash="bbb222")),
        _result("64", 0.838, env=EnvRecord(platform="Windows-11", python="3.11.9", packages_hash="bbb222")),
    ]
    analysis = analyze_round(study=study, results=results, round_no=1, backend=HeuristicBackend())
    if analysis.comparability.ok:
        return "FAIL", "环境指纹漂移未被标记"
    if analysis.confidence != Confidence.MEDIUM:
        return "FAIL", f"置信度未降级：{analysis.confidence}"
    if not any("指纹" in issue for issue in analysis.comparability.issues):
        return "FAIL", "问题描述缺少指纹信息"
    return "PASS", f"指纹漂移 → ok=false + 置信度降级（{analysis.confidence.value}）"


def check_20_09_report(tmp: Path) -> tuple[str, str]:
    del tmp
    study = _synth_study(["8", "16", "32", "64"])
    results = [_result(v, m) for v, m in [("8", 0.831), ("16", 0.840), ("32", 0.844), ("64", 0.838)]]
    analysis = analyze_round(study=study, results=results, round_no=1, backend=HeuristicBackend())
    plan = ExperimentPlan(
        round=1,
        created_at="2026-09-22T22:00:00+08:00",
        runs=[RunSpec(id=result.run_id, params=result.params, command="echo") for result in results],
    )
    report = build_report(study=study, rounds=[(1, plan, results, analysis)], stopped_reason="no_suggestion")
    workdir = Path(tempfile.mkdtemp(prefix="labloop-audit-report-"))
    try:
        html_path = workdir / "report.html"
        json_path = workdir / "report.json"
        write_html_report(report, study, html_path)
        write_json_report(report, json_path)
        html = html_path.read_text(encoding="utf-8")
        if EXTERNAL_REF_RE.search(html):
            return "FAIL", "HTML 存在外部资源引用"
        if "<svg" not in html or 'class="chart-best"' not in html:
            return "FAIL", "HTML 缺少 SVG 图表或最佳点标注"
        payload = read_json(json_path)
        if payload["best_overall"]["value"] != "32" or payload["schema_version"] != "1.0":
            return "FAIL", f"report.json 内容不符：{payload.get('best_overall')}"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return "PASS", "HTML 自包含 + SVG 最佳点标注；report.json 可往返且 best 正确"


def check_20_10_gate_and_interrupt(tmp: Path) -> tuple[str, str]:
    design(tmp)
    code, output = run_cli("auto", "batch", "--workdir", str(tmp / ".labloop"), cwd=tmp)
    if code != 2 or "非交互终端" not in output:
        return "FAIL", f"非交互无 --yes 应跳过（exit 2）：exit={code}"
    runs_dir = tmp / ".labloop" / "studies" / "batch" / "rounds" / "round-001" / "runs"
    if runs_dir.exists() and list(runs_dir.glob("*/result.json")):
        return "FAIL", "跳过动作仍产生了执行结果"

    from labloop.cli import main as cli_main

    class _InterruptingRunner:
        def __init__(self, **_kwargs) -> None:
            pass

        def run_round(self, **_kwargs):
            raise KeyboardInterrupt

    original = cli_main.Runner
    cli_main.Runner = _InterruptingRunner  # type: ignore[assignment]
    sys.argv_backup = sys.argv
    sys.argv = ["labloop", "run", "batch", "--yes", "--workdir", str(tmp / ".labloop")]
    exit_code = None
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            try:
                cli_main.app()
            except SystemExit as exc:
                exit_code = exc.code
    finally:
        cli_main.Runner = original  # type: ignore[assignment]
        sys.argv = sys.argv_backup
    if exit_code != 130:
        return "FAIL", f"中断退出码应为 130，实际 {exit_code}"
    return "PASS", "非交互 → 不执行（exit 2）；合成中断 → exit 130（真实中断结果落盘由单元测试覆盖）"


def check_20_11_budget_and_timeout(tmp: Path) -> tuple[str, str]:
    del tmp
    from labloop.domain.models import ExperimentPlan, RunSpec

    study = _synth_study(["8", "16", "32"])
    study.cwd = str(Path(tempfile.mkdtemp(prefix="labloop-audit-runner-")))
    layout = StudyLayout(Path(study.cwd) / "studies" / "audit")
    recipe = build_generic(
        command="echo acc: 0.9 batch={batch}",
        metric_flags=[ACC_METRIC],
        var_values={"batch": ["8", "16", "32"]},
    )

    class _FakeSpawner:
        def __init__(self, *, timed_out: bool = False) -> None:
            self.calls: list[str] = []
            self.timed_out = timed_out

        def __call__(self, command, *, cwd, timeout_s, env_extra, log_path):
            self.calls.append(command)
            Path(log_path).write_text("acc: 0.9", encoding="utf-8")
            return (None if self.timed_out else 0), self.timed_out

    plan = ExperimentPlan(
        round=1,
        created_at="2026-09-22T22:00:00+08:00",
        runs=[
            RunSpec(id=f"r001-batch-{value}", params={"batch": value}, command=f"echo {value}")
            for value in study.parameter.values
        ],
    )
    try:
        spawner = _FakeSpawner()
        runner = Runner(
            layout=layout,
            study=study,
            plan=plan,
            recipe=recipe,
            spawner=spawner,
            env_collector=lambda: EnvRecord(platform="audit", python="3.11", packages_hash="audit"),
        )
        outcome = runner.run_round(max_runs=2)
        if outcome.stopped != "max_runs" or len(outcome.results) != 2 or len(spawner.calls) != 2:
            return "FAIL", f"--max-runs 未生效：{outcome.stopped} / {len(outcome.results)}"

        timed_spawner = _FakeSpawner(timed_out=True)
        runner_t = Runner(
            layout=layout,
            study=study,
            plan=plan.model_copy(update={"runs": [plan.runs[2]]}),
            recipe=recipe,
            redo=True,
            spawner=timed_spawner,
            env_collector=lambda: EnvRecord(platform="audit", python="3.11", packages_hash="audit"),
        )
        outcome_t = runner_t.run_round()
        if outcome_t.results[0].status != RunStatus.TIMEOUT:
            return "FAIL", f"超时状态不符：{outcome_t.results[0].status}"

        budget_spawner = _FakeSpawner()
        runner_b = Runner(
            layout=layout,
            study=study,
            plan=plan.model_copy(update={"runs": [plan.runs[0]]}),
            recipe=recipe,
            redo=True,
            spawner=budget_spawner,
            env_collector=lambda: EnvRecord(platform="audit", python="3.11", packages_hash="audit"),
        )
        outcome_b = runner_b.run_round(deadline=time.monotonic() - 1)
        if outcome_b.stopped != "budget" or budget_spawner.calls:
            return "FAIL", f"预算守卫未生效：{outcome_b.stopped}"
    finally:
        shutil.rmtree(study.cwd, ignore_errors=True)
    return "PASS", "--max-runs 截断 / 超时 → timeout 状态 / 预算耗尽不启动新 run"


def check_20_12_quality_gate(_tmp: Path, *, skip_tests: bool) -> tuple[str, str]:
    steps = [
        ("ruff", [sys.executable, "-m", "ruff", "check", "src", "tests", "scripts"]),
        ("ruff format", [sys.executable, "-m", "ruff", "format", "--check", "src"]),
        ("pyright", [sys.executable, "-m", "pyright"]),
        ("schemas", [sys.executable, "scripts/export_schemas.py", "--check"]),
    ]
    if not skip_tests:
        steps.append(("pytest", [sys.executable, "-m", "pytest", "-q", "-m", "not gpu"]))
    failures: list[str] = []
    for name, argv in steps:
        proc = subprocess.run(argv, cwd=str(ROOT), capture_output=True, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        if proc.returncode != 0:
            tail = proc.stdout.decode("utf-8", errors="replace").strip().splitlines()[-3:]
            failures.append(f"{name}: {' | '.join(tail)}")
    if failures:
        return "FAIL", "；".join(failures)
    names = " + ".join(name for name, _ in steps)
    return "PASS", f"{names} 全绿" + ("（pytest 由调用方执行）" if skip_tests else "")


def main() -> int:
    skip_tests = "--skip-tests" in sys.argv[1:]
    tmp_root = Path(tempfile.mkdtemp(prefix="labloop-mvp-"))
    checks = [
        ("20-1", check_20_01_design_offline),
        ("20-2", check_20_02_design_llm_and_fallback),
        ("20-3", check_20_03_plan_dry_run),
        ("20-4", check_20_04_resume_skips_done),
        ("20-5", check_20_05_plumbing_real_subprocess),
        ("20-6", check_20_06_analyze_offline_rules),
        ("20-7", check_20_07_analyze_llm),
        ("20-8", check_20_08_comparability_guard),
        ("20-9", check_20_09_report),
        ("20-10", check_20_10_gate_and_interrupt),
        ("20-11", check_20_11_budget_and_timeout),
    ]
    try:
        for number, check in checks:
            try:
                check_dir = tmp_root / number
                check_dir.mkdir(parents=True, exist_ok=True)
                status, detail = check(check_dir)
            except Exception as exc:
                status, detail = "FAIL", f"{type(exc).__name__}: {exc}"
            record(status, f"{number} {check.__name__.removeprefix('check_20_')}", detail)
        try:
            gate_dir = tmp_root / "20-12"
            gate_dir.mkdir(parents=True, exist_ok=True)
            status, detail = check_20_12_quality_gate(gate_dir, skip_tests=skip_tests)
        except Exception as exc:
            status, detail = "FAIL", f"{type(exc).__name__}: {exc}"
        record(status, "20-12 quality_gate", detail)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    passed = sum(1 for status, _, _ in RESULTS if status == "PASS")
    skipped = sum(1 for status, _, _ in RESULTS if status == "SKIP")
    failed = sum(1 for status, _, _ in RESULTS if status == "FAIL")
    print(f"\n{passed} PASS / {skipped} SKIP / {failed} FAIL")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
