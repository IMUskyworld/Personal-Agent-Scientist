"""CLI end-to-end: offline chain, gate behavior, exit codes, artifacts."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEV = ROOT / "scripts" / "labloop_dev.py"
EXTERNAL_REF_RE = re.compile(r'(?:src|href)\s*=\s*"https?://')

ECHO_COMMAND = "echo acc: 0.9 batch={batch}"
ACC_METRIC = "accuracy=acc:\\s*([0-9.]+)"


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


def design(tmp_path: Path, *extra: str, env_extra: dict[str, str] | None = None) -> tuple[int, str]:
    workdir = str(tmp_path / ".labloop")
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
        workdir,
        *extra,
        cwd=tmp_path,
        env_extra=env_extra,
    )


def inject_results(tmp_path: Path, slug: str, round_no: int, pairs: list[tuple[str, float]]) -> None:
    """Write synthetic RunResults into the study store — no execution involved."""
    from support import make_result

    runs_dir = tmp_path / ".labloop" / "studies" / slug / "rounds" / f"round-{round_no:03d}" / "runs"
    for value, metric in pairs:
        run_id = f"r{round_no:03d}-batch-{value}"
        run_dir = runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        result = make_result(value, metric, run_id=run_id)
        (run_dir / "result.json").write_text(
            json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def test_cli_version(tmp_path: Path) -> None:
    code, output = run_cli("version", cwd=tmp_path)
    assert code == 0
    assert "labloop" in output


def test_design_offline_creates_study(tmp_path: Path) -> None:
    code, output = design(tmp_path)
    assert code == 0, output
    study_dir = tmp_path / ".labloop" / "studies" / "batch"
    study = json.loads((study_dir / "study.json").read_text(encoding="utf-8"))
    assert study["hypothesis"]["source"] == "offline"
    assert study["parameter"]["values"] == ["8", "16", "32"]
    assert study["backend"] == "offline"
    assert (study_dir / "recipe.json").is_file()
    plan = json.loads((study_dir / "rounds" / "round-001" / "plan.json").read_text(encoding="utf-8"))
    assert len(plan["runs"]) == 3
    assert "batch=8" in plan["runs"][0]["command"]


def test_design_duplicate_question_gets_suffixed_slug(tmp_path: Path) -> None:
    design(tmp_path)
    code, output = design(tmp_path)
    assert code == 0
    assert (tmp_path / ".labloop" / "studies" / "batch-2" / "study.json").is_file()
    assert "batch-2" in output


def test_design_explicit_existing_study_is_structured_error(tmp_path: Path) -> None:
    design(tmp_path)
    code, output = design(tmp_path, "--study", "batch")
    assert code == 3
    assert "STUDY_EXISTS" in output


def test_plan_dry_run_does_not_execute(tmp_path: Path) -> None:
    design(tmp_path)
    code, output = run_cli("plan", "batch", "--workdir", str(tmp_path / ".labloop"), cwd=tmp_path)
    assert code == 0
    assert "dry-run" in output
    runs_dir = tmp_path / ".labloop" / "studies" / "batch" / "rounds" / "round-001" / "runs"
    if runs_dir.exists():
        assert not list(runs_dir.glob("*/result.json"))


def test_run_non_interactive_without_yes_skips(tmp_path: Path) -> None:
    design(tmp_path)
    code, output = run_cli("run", "batch", "--workdir", str(tmp_path / ".labloop"), cwd=tmp_path)
    assert code == 2
    assert "非交互终端" in output


def test_auto_non_interactive_without_yes_skips(tmp_path: Path) -> None:
    design(tmp_path)
    code, output = run_cli("auto", "batch", "--workdir", str(tmp_path / ".labloop"), cwd=tmp_path)
    assert code == 2
    assert "非交互终端" in output
    runs_dir = tmp_path / ".labloop" / "studies" / "batch" / "rounds" / "round-001" / "runs"
    assert not runs_dir.exists() or not list(runs_dir.glob("*/result.json"))


def test_analyze_next_report_chain(tmp_path: Path) -> None:
    design(tmp_path)
    inject_results(tmp_path, "batch", 1, [("8", 0.831), ("16", 0.84), ("32", 0.844)])
    workdir = str(tmp_path / ".labloop")

    code, output = run_cli("analyze", "batch", "--workdir", workdir, cwd=tmp_path)
    assert code == 0, output
    analysis_path = tmp_path / ".labloop" / "studies" / "batch" / "rounds" / "round-001" / "analysis.json"
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    assert analysis["best"]["value"] == "32"
    assert analysis["generated_by"] == "offline"
    assert analysis["next"]["values"] == ["40"]  # 边界外推

    code, output = run_cli("next", "batch", "--workdir", workdir, cwd=tmp_path)
    assert code == 0, output
    plan2 = tmp_path / ".labloop" / "studies" / "batch" / "rounds" / "round-002" / "plan.json"
    assert plan2.is_file()
    payload = json.loads(plan2.read_text(encoding="utf-8"))
    assert payload["runs"][0]["params"]["batch"] == "40"

    code, output = run_cli("report", "batch", "--workdir", workdir, cwd=tmp_path)
    assert code == 0, output
    study_dir = tmp_path / ".labloop" / "studies" / "batch"
    html = (study_dir / "report.html").read_text(encoding="utf-8")
    assert EXTERNAL_REF_RE.search(html) is None
    assert "<svg" in html
    report = json.loads((study_dir / "report.json").read_text(encoding="utf-8"))
    assert report["best_overall"]["value"] == "32"


def test_status_and_missing_study(tmp_path: Path) -> None:
    design(tmp_path)
    code, output = run_cli("status", "batch", "--workdir", str(tmp_path / ".labloop"), cwd=tmp_path)
    assert code == 0
    assert "batch" in output

    code, output = run_cli("status", "nope", "--workdir", str(tmp_path / ".labloop"), cwd=tmp_path)
    assert code == 3
    assert "STUDY_NOT_FOUND" in output


def test_report_without_results_is_not_performed(tmp_path: Path) -> None:
    design(tmp_path)
    code, output = run_cli("report", "batch", "--workdir", str(tmp_path / ".labloop"), cwd=tmp_path)
    assert code == 2
    assert "还没有任何结果" in output


# --- regressions (release audit, batch 1) --------------------------------------


def test_design_failure_leaves_no_half_written_study(tmp_path: Path) -> None:
    """命令模板缺占位符 → 失败，且不留下 study.json/recipe.json。"""
    code, output = run_cli(
        "design",
        "batch 对精度的影响",
        "--command",
        "echo {missing}",
        "--metric",
        ACC_METRIC,
        "--var",
        "batch=8",
        "--study",
        "broken",
        "--workdir",
        str(tmp_path / ".labloop"),
        cwd=tmp_path,
    )
    assert code == 3
    assert "TEMPLATE_MISSING_VALUE" in output
    study_dir = tmp_path / ".labloop" / "studies" / "broken"
    assert not (study_dir / "study.json").exists()
    assert not (study_dir / "recipe.json").exists()
    # 空目录也不该留下（否则 slug 会被占用）
    assert not study_dir.exists()


def test_design_rejects_study_name_with_path_separator(tmp_path: Path) -> None:
    code, output = run_cli(
        "design",
        "batch 对精度的影响",
        "--command",
        ECHO_COMMAND,
        "--metric",
        ACC_METRIC,
        "--var",
        "batch=8",
        "--study",
        "..\\escaped",
        "--workdir",
        str(tmp_path / ".labloop"),
        cwd=tmp_path,
    )
    assert code == 3
    assert "INVALID_ARGUMENT" in output
    assert not (tmp_path / ".labloop" / "escaped").exists()


def test_auto_stops_when_max_runs_truncates_a_round(tmp_path: Path) -> None:
    """--max-runs 截断后必须停下：不能带着半轮数据进入下一轮。"""
    design(tmp_path)  # 3 个取值
    workdir = str(tmp_path / ".labloop")
    code, output = run_cli("auto", "batch", "--yes", "--max-runs", "1", "--workdir", workdir, cwd=tmp_path)
    assert code == 0, output
    assert "截断" in output

    study_dir = tmp_path / ".labloop" / "studies" / "batch"
    round1 = study_dir / "rounds" / "round-001"
    assert len(list((round1 / "runs").glob("*/result.json"))) == 1
    assert not (study_dir / "rounds" / "round-002").exists()
    # 半轮数据不产生结论
    assert not (round1 / "analysis.json").exists()
    report = json.loads((study_dir / "report.json").read_text(encoding="utf-8"))
    assert report["stopped_reason"] == "max_runs"


def test_next_refuses_to_overwrite_an_existing_plan(tmp_path: Path) -> None:
    design(tmp_path)
    inject_results(tmp_path, "batch", 1, [("8", 0.831), ("16", 0.84), ("32", 0.844)])
    workdir = str(tmp_path / ".labloop")
    assert run_cli("analyze", "batch", "--workdir", workdir, cwd=tmp_path)[0] == 0
    assert run_cli("next", "batch", "--workdir", workdir, cwd=tmp_path)[0] == 0

    code, output = run_cli("next", "batch", "--workdir", workdir, cwd=tmp_path)
    assert code == 3
    assert "已存在" in output

    code, output = run_cli("next", "batch", "--force", "--workdir", workdir, cwd=tmp_path)
    assert code == 0, output


def test_duplicate_var_is_rejected(tmp_path: Path) -> None:
    code, output = run_cli(
        "design",
        "batch 对精度的影响",
        "--command",
        ECHO_COMMAND,
        "--metric",
        ACC_METRIC,
        "--var",
        "batch=8,16",
        "--var",
        "batch=32",
        "--workdir",
        str(tmp_path / ".labloop"),
        cwd=tmp_path,
    )
    assert code == 3
    assert "INVALID_ARGUMENT" in output
    assert "重复" in output


def test_redo_gate_shows_the_real_run_count(tmp_path: Path) -> None:
    design(tmp_path)
    workdir = str(tmp_path / ".labloop")
    # 非交互（无 --yes）→ 不执行，但计划摘要仍应显示将执行的 run 数
    code, output = run_cli("run", "batch", "--redo", "--workdir", workdir, cwd=tmp_path)
    assert code == 2
    assert "3 个 run" in output


def test_rerun_invalidates_the_stale_analysis(tmp_path: Path) -> None:
    """结果变了 → 旧结论作废（否则报告会引用过期结论）。"""
    design(tmp_path)
    inject_results(tmp_path, "batch", 1, [("8", 0.831), ("16", 0.84), ("32", 0.844)])
    workdir = str(tmp_path / ".labloop")
    assert run_cli("analyze", "batch", "--workdir", workdir, cwd=tmp_path)[0] == 0
    analysis_path = tmp_path / ".labloop" / "studies" / "batch" / "rounds" / "round-001" / "analysis.json"
    assert analysis_path.is_file()

    code, output = run_cli("run", "batch", "--yes", "--redo", "--workdir", workdir, cwd=tmp_path)
    assert code == 0, output
    assert "作废" in output
    assert not analysis_path.exists()

    # 报告里这一轮应显示「未分析」，而不是引用旧结论
    assert run_cli("report", "batch", "--workdir", workdir, cwd=tmp_path)[0] == 0
    report = json.loads((tmp_path / ".labloop" / "studies" / "batch" / "report.json").read_text(encoding="utf-8"))
    assert report["rounds"][0]["analysis"] is None


def test_invalid_arguments_are_structured_errors(tmp_path: Path) -> None:
    code, output = design(tmp_path, "--goal", "maximizee")
    assert code == 3
    assert "INVALID_ARGUMENT" in output

    code, output = design(tmp_path, "--backend", "llmm")
    assert code == 3
    assert "INVALID_ARGUMENT" in output

    design(tmp_path)
    workdir = str(tmp_path / ".labloop")
    for extra in (["--max-runs", "0"], ["--timeout", "0"], ["--max-runs", "-1"]):
        code, output = run_cli("run", "batch", "--yes", *extra, "--workdir", workdir, cwd=tmp_path)
        assert code == 3, output
        assert "INVALID_ARGUMENT" in output

    code, output = run_cli("auto", "batch", "--yes", "--max-rounds", "0", "--workdir", workdir, cwd=tmp_path)
    assert code == 3
    assert "INVALID_ARGUMENT" in output
