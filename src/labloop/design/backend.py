"""Design backend contracts (DEVELOPMENT.md §10)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from labloop.domain.enums import Backend, Confidence, Goal
from labloop.domain.models import Finding, Hypothesis, NextSuggestion, Parameter, RunResult, Study
from labloop.recipes.model import Recipe


@dataclass
class DesignHints:
    var_values: dict[str, list[str]] = field(default_factory=dict)
    max_runs: int = 16
    goal: Goal = Goal.MAXIMIZE


@dataclass
class DesignOutcome:
    hypothesis: Hypothesis
    parameter: Parameter
    notes: list[str] = field(default_factory=list)


@dataclass
class AnalysisOutcome:
    summary: str
    findings: list[Finding] = field(default_factory=list)
    best_value: str | None = None
    best_metric: float | None = None
    boundary: bool = False
    next: NextSuggestion | None = None
    confidence: Confidence = Confidence.MEDIUM
    fallback_reason: str | None = None


class DesignBackend(Protocol):
    backend_id: Backend

    def design(self, *, question: str, recipe: Recipe, hints: DesignHints) -> DesignOutcome: ...

    def analyze(self, *, study: Study, results: list[RunResult], round_no: int) -> AnalysisOutcome: ...
