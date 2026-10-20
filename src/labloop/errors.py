"""Structured error model.

Every failure labloop reports carries a stable code so that terminals, JSON
artifacts and tests can all match on the same identifier instead of on message text.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# --- error codes (stable public identifiers, do not rename) -------------------
STUDY_NOT_FOUND = "STUDY_NOT_FOUND"
STUDY_EXISTS = "STUDY_EXISTS"
STUDY_INVALID = "STUDY_INVALID"
STUDY_AMBIGUOUS = "STUDY_AMBIGUOUS"
RECIPE_INVALID = "RECIPE_INVALID"
RECIPE_NOT_FOUND = "RECIPE_NOT_FOUND"
TEMPLATE_MISSING_VALUE = "TEMPLATE_MISSING_VALUE"
VAR_REQUIRED_OFFLINE = "VAR_REQUIRED_OFFLINE"
TOO_MANY_VARIABLES = "TOO_MANY_VARIABLES"
MATRIX_TOO_LARGE = "MATRIX_TOO_LARGE"
DESIGN_FAILED = "DESIGN_FAILED"
PLAN_NOT_FOUND = "PLAN_NOT_FOUND"
PLAN_EMPTY = "PLAN_EMPTY"
NO_SUGGESTION = "NO_SUGGESTION"
RUN_FAILED = "RUN_FAILED"
RUN_TIMEOUT = "RUN_TIMEOUT"
ROUND_NOT_FOUND = "ROUND_NOT_FOUND"
METRIC_UNPARSEABLE = "METRIC_UNPARSEABLE"
CONFIG_INVALID = "CONFIG_INVALID"
INVALID_ARGUMENT = "INVALID_ARGUMENT"
WORKDIR_CREATE_FAILED = "WORKDIR_CREATE_FAILED"
ARTIFACT_WRITE_FAILED = "ARTIFACT_WRITE_FAILED"
CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
LLM_DISABLED = "LLM_DISABLED"
LLM_REQUEST_FAILED = "LLM_REQUEST_FAILED"
LLM_OUTPUT_INVALID = "LLM_OUTPUT_INVALID"
REPORT_GENERATION_FAILED = "REPORT_GENERATION_FAILED"
UNKNOWN_ERROR = "UNKNOWN_ERROR"


class LabloopError(Exception):
    """An error with a machine-readable code.

    ``details`` must never contain secrets: it is rendered to terminals and may
    be embedded in reports.
    """

    code: str = UNKNOWN_ERROR
    component: str = "core"
    recoverable: bool = True

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        component: str | None = None,
        recoverable: bool | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if component is not None:
            self.component = component
        if recoverable is not None:
            self.recoverable = recoverable
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "component": self.component,
            "recoverable": self.recoverable,
            "details": self.details,
        }

    def __str__(self) -> str:  # pragma: no cover - convenience for tracebacks
        return f"[{self.code}] {self.message}"


class StudyError(LabloopError):
    code = STUDY_INVALID
    component = "study"


class AmbiguousStudyError(StudyError):
    code = STUDY_AMBIGUOUS


class RecipeError(LabloopError):
    code = RECIPE_INVALID
    component = "recipes"


class DesignError(LabloopError):
    code = DESIGN_FAILED
    component = "design"


class ConfigError(LabloopError):
    code = CONFIG_INVALID
    component = "config"
    recoverable = False


class LlmError(LabloopError):
    code = LLM_REQUEST_FAILED
    component = "llm"


def describe(exc: BaseException, *, fallback_component: str = "core") -> dict[str, Any]:
    """Normalize any exception into the structured error shape used in JSON files."""
    if isinstance(exc, LabloopError):
        return exc.to_dict()
    return {
        "code": getattr(exc, "code", UNKNOWN_ERROR) if exc else UNKNOWN_ERROR,
        "message": f"{type(exc).__name__}: {exc}",
        "component": fallback_component,
        "recoverable": True,
        "details": {"exception_type": type(exc).__name__},
    }
