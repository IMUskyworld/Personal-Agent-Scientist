"""Configuration loading (DEVELOPMENT.md §15).

Search order: ``--config`` > ``./labloop.toml`` > user config. Environment variables
override file values. The LLM API key lives **only** in the environment and is never
part of the config model or any artifact.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from labloop.errors import CONFIG_INVALID, ConfigError


class LlmConfig(BaseModel):
    enabled: bool = False
    base_url: str | None = None
    model: str | None = None


class DefaultsConfig(BaseModel):
    timeout_s: float = 1800.0
    max_runs: int = 16
    max_rounds: int = 3


class LabloopConfig(BaseModel):
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)


def user_config_path() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "labloop" / "config.toml"
    return Path.home() / ".config" / "labloop" / "config.toml"


def find_config_path(explicit: str | None, cwd: Path | None = None) -> Path | None:
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise ConfigError(f"配置文件不存在：{path}", code=CONFIG_INVALID, details={"path": str(path)})
        return path
    local = (cwd or Path.cwd()) / "labloop.toml"
    if local.is_file():
        return local
    fallback = user_config_path()
    return fallback if fallback.is_file() else None


def load_config(explicit_path: str | None = None, cwd: Path | None = None) -> LabloopConfig:
    path = find_config_path(explicit_path, cwd)
    data: dict[str, object] = {}
    if path is not None:
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(
                f"无法解析配置文件：{path}",
                code=CONFIG_INVALID,
                details={"path": str(path), "reason": f"{type(exc).__name__}: {exc}"},
            ) from exc
    try:
        config = LabloopConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(
            f"配置文件字段无效：{path}",
            code=CONFIG_INVALID,
            details={"path": str(path), "reason": exc.errors()[:5]},
        ) from exc
    _apply_env_overrides(config)
    return config


def _apply_env_overrides(config: LabloopConfig) -> None:
    base_url = os.environ.get("LABLOOP_LLM_BASE_URL")
    if base_url:
        config.llm.base_url = base_url
    model = os.environ.get("LABLOOP_LLM_MODEL")
    if model:
        config.llm.model = model


def llm_api_key() -> str | None:
    """The API key is read from the environment at call time and never stored."""
    return os.environ.get("LABLOOP_LLM_API_KEY") or None


def llm_ready(config: LabloopConfig) -> tuple[bool, str]:
    """Return (ready, reason-when-not-ready).

    Ready = endpoint + model + key are all present; either ``[llm] enabled=true``
    or an explicitly configured base_url counts as "the user wants the LLM".
    """
    if not (config.llm.enabled or config.llm.base_url):
        return False, "LLM 未启用（labloop.toml 里把 llm.enabled 设为 true，或设置 LABLOOP_LLM_BASE_URL）"
    if not config.llm.base_url:
        return False, "未配置 LLM 端点（llm.base_url 或 LABLOOP_LLM_BASE_URL）"
    if not config.llm.model:
        return False, "未配置 LLM 模型名（llm.model 或 LABLOOP_LLM_MODEL）"
    if not llm_api_key():
        return False, "未配置 LLM API key（环境变量 LABLOOP_LLM_API_KEY）"
    return True, ""
