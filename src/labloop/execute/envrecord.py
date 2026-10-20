"""Per-run environment fingerprint (DEVELOPMENT.md §11) — makes runs comparable."""

from __future__ import annotations

import hashlib
import platform
import re
import shutil
import subprocess
import sys

from labloop.domain.models import EnvRecord, GpuSummary

_DRIVER_RE = re.compile(r"Driver Version:\s*([0-9][0-9.]*)")


def decode_bytes(data: bytes) -> str:
    """UTF-8 → GBK → lossy UTF-8; never assume the child used UTF-8 (LabBox lesson)."""
    for encoding in ("utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def packages_hash() -> str:
    import importlib.metadata as metadata

    entries = sorted(f"{dist.metadata['Name']}=={dist.version}" for dist in metadata.distributions())
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()[:12]


def _platform_text() -> str:
    name = platform.system() or "unknown"
    release = platform.release() or ""
    if name == "Windows":
        try:
            _, version, _, _ = platform.win32_ver()
            build = int(version.split(".")[-1]) if version else 0
            if build >= 22000:
                release = "11"
        except (OSError, ValueError):  # pragma: no cover - platform oddities
            pass
    return f"{name}-{release}".strip("-")


def _run_smi(argv: list[str], *, timeout_s: float = 5.0) -> str | None:
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout_s)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return decode_bytes(proc.stdout)


def gpu_summary() -> GpuSummary | None:
    """First GPU name + driver via nvidia-smi; ``None`` when unavailable (not an error)."""
    smi = shutil.which("nvidia-smi")
    if not smi:
        return None
    name_output = _run_smi([smi, "--query-gpu=name", "--format=csv,noheader"])
    if name_output is None:
        return None
    first_line = next((line.strip() for line in name_output.splitlines() if line.strip()), "")
    if not first_line:
        return None
    driver = None
    header_output = _run_smi([smi])
    if header_output and (match := _DRIVER_RE.search(header_output)):
        driver = match.group(1)
    return GpuSummary(name=first_line, driver=driver)


def collect_env() -> EnvRecord:
    info = sys.version_info
    return EnvRecord(
        platform=_platform_text(),
        python=f"{info.major}.{info.minor}.{info.micro}",
        packages_hash=packages_hash(),
        gpu=gpu_summary(),
    )
