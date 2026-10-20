"""Metric extraction from run output (DEVELOPMENT.md §9/§11).

A metric that cannot be parsed is a warning, never a fabricated value.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
from pathlib import Path

from labloop.domain.enums import MetricKind
from labloop.recipes.model import MetricRule

#: Read size when scanning a log file for the last metric match.
SCAN_CHUNK_CHARS = 64 * 1024
#: Characters kept between chunks so a match spanning a chunk boundary is found.
SCAN_OVERLAP_CHARS = 4096


def parse_metrics(
    rules: list[MetricRule],
    *,
    stdout_text: str | None = None,
    stdout_path: Path | None = None,
    cwd: Path,
    template_vars: dict[str, str],
) -> tuple[dict[str, float], list[str]]:
    """Extract metrics from a finished run.

    ``stdout_path`` is the run's log file: it is scanned in chunks, so a huge log
    never has to fit in memory. ``stdout_text`` is kept for callers that already
    hold the text (tests, synthetic runs).
    """
    metrics: dict[str, float] = {}
    warnings: list[str] = []
    for rule in rules:
        try:
            value = _extract(
                rule,
                stdout_text=stdout_text,
                stdout_path=stdout_path,
                cwd=cwd,
                template_vars=template_vars,
            )
        except Exception as exc:
            warnings.append(f"指标 {rule.name} 解析失败：{type(exc).__name__}: {exc}")
            continue
        if value is None:
            warnings.append(f"指标 {rule.name} 未在输出中找到")
            continue
        if not math.isfinite(value):
            warnings.append(f"指标 {rule.name} 不是有限数值（{value}），已忽略")
            continue
        metrics[rule.name] = value
    return metrics, warnings


def _last_regex_match(pattern: str, path: Path) -> str | None:
    """Last match of ``pattern`` in a log file, scanned chunk by chunk."""
    compiled = re.compile(pattern)
    last: str | None = None
    buffer = ""
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        while True:
            chunk = handle.read(SCAN_CHUNK_CHARS)
            if not chunk:
                break
            buffer += chunk
            matches = compiled.findall(buffer)
            if matches:
                last = matches[-1]
            if len(buffer) > SCAN_OVERLAP_CHARS:
                buffer = buffer[-SCAN_OVERLAP_CHARS:]
    return last


def _extract(
    rule: MetricRule,
    *,
    stdout_text: str | None,
    stdout_path: Path | None,
    cwd: Path,
    template_vars: dict[str, str],
) -> float | None:
    if rule.kind == MetricKind.STDOUT_REGEX:
        assert rule.pattern is not None
        if stdout_path is not None:
            if not stdout_path.is_file():
                raise FileNotFoundError(f"日志文件不存在：{stdout_path}")
            raw = _last_regex_match(rule.pattern, stdout_path)
        else:
            matches = re.findall(rule.pattern, stdout_text or "")
            raw = matches[-1] if matches else None
        if raw is None:
            return None
        return float(raw)
    if rule.kind == MetricKind.CSV_LAST:
        path = _resolve(rule.file or "", template_vars, cwd)
        text = _read_text(path)
        # csv 模块：支持带引号的字段（split(",") 会在引号内逗号处出错）
        rows = [row for row in csv.reader(io.StringIO(text)) if any(cell.strip() for cell in row)]
        if not rows:
            return None
        header = [cell.strip() for cell in rows[0]]
        column = rule.column or ""
        if column not in header:
            raise ValueError(f"CSV 缺少列「{column}」")
        index = header.index(column)
        last_cells = rows[-1]
        if index >= len(last_cells):
            raise ValueError("CSV 最后一行列数不足")
        return float(last_cells[index].strip())
    if rule.kind == MetricKind.JSON_FILE:
        path = _resolve(rule.file or "", template_vars, cwd)
        payload = json.loads(_read_text(path))
        node: object = payload
        for part in (rule.point or "").split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
                node = node[int(part)]
            else:
                raise KeyError(f"JSON 路径 {rule.point} 在「{part}」处中断")
        return float(node)  # type: ignore[arg-type]
    return None


def _resolve(template: str, template_vars: dict[str, str], cwd: Path) -> Path:
    rendered = template.format(**template_vars)
    path = Path(rendered)
    return path if path.is_absolute() else cwd / path


def _read_text(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"文件不存在：{path}")
    data = path.read_bytes()
    for encoding in ("utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")
