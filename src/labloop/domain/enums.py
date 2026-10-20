"""Enums shared across design / execute / analyze / report."""

from __future__ import annotations

from enum import Enum


class RunStatus(str, Enum):
    OK = "ok"
    FAILED = "failed"
    TIMEOUT = "timeout"
    INTERRUPTED = "interrupted"


class Backend(str, Enum):
    LLM = "llm"
    OFFLINE = "offline"


class Source(str, Enum):
    LLM = "llm"
    OFFLINE = "offline"
    USER = "user"


class Goal(str, Enum):
    MAXIMIZE = "maximize"
    MINIMIZE = "minimize"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class MetricKind(str, Enum):
    STDOUT_REGEX = "stdout_regex"
    CSV_LAST = "csv_last"
    JSON_FILE = "json_file"
