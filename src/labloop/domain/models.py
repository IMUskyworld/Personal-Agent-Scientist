"""Pydantic models: the data contract between pipeline stages.

Schemas are exported to ``docs/schemas/`` by ``scripts/export_schemas.py``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from labloop.domain.enums import Backend, Confidence, Goal, RunStatus, Source

SCHEMA_VERSION = "1.0"


class Note(BaseModel):
    """A non-fatal observation."""

    code: str
    message: str


class Parameter(BaseModel):
    name: str
    values: list[str]


class Hypothesis(BaseModel):
    statement: str
    variable: str
    expected_effect: str = "unknown"
    rationale: str = ""
    source: Source = Source.OFFLINE


class Study(BaseModel):
    schema_version: str = SCHEMA_VERSION
    slug: str
    question: str
    created_at: str
    recipe_id: str
    backend: Backend
    goal: Goal = Goal.MAXIMIZE
    primary_metric: str
    secondary_metrics: list[str] = Field(default_factory=list)
    parameter: Parameter
    fixed: dict[str, str] = Field(default_factory=dict)
    hypothesis: Hypothesis
    cwd: str
    runs_planned: int = 0


class RunSpec(BaseModel):
    id: str
    params: dict[str, str] = Field(default_factory=dict)
    command: str
    timeout_s: float | None = None


class ExperimentPlan(BaseModel):
    schema_version: str = SCHEMA_VERSION
    round: int
    created_at: str
    source: Backend | str = Backend.OFFLINE
    runs: list[RunSpec] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class GpuSummary(BaseModel):
    name: str
    driver: str | None = None


class EnvRecord(BaseModel):
    platform: str
    python: str
    packages_hash: str
    gpu: GpuSummary | None = None


class RunResult(BaseModel):
    schema_version: str = SCHEMA_VERSION
    run_id: str
    params: dict[str, str] = Field(default_factory=dict)
    status: RunStatus
    exit_code: int | None = None
    started_at: str
    ended_at: str
    duration_s: float
    metrics: dict[str, float] = Field(default_factory=dict)
    log_path: str | None = None
    env: EnvRecord | None = None
    error: str | None = None
    warnings: list[Note] = Field(default_factory=list)
    #: 固定上下文快照（§12.2 可比性守卫的第一条：固定参数/配方必须一致）
    fixed: dict[str, str] = Field(default_factory=dict)
    recipe_id: str | None = None


class Finding(BaseModel):
    kind: str
    detail: str


class BestPoint(BaseModel):
    param: str
    value: str
    metric_value: float


class NextSuggestion(BaseModel):
    param: str
    values: list[str]
    rationale: str


class Comparability(BaseModel):
    ok: bool = True
    issues: list[str] = Field(default_factory=list)


class RoundAnalysis(BaseModel):
    schema_version: str = SCHEMA_VERSION
    round: int
    generated_by: Backend
    summary: str
    findings: list[Finding] = Field(default_factory=list)
    best: BestPoint | None = None
    boundary: bool = False
    next: NextSuggestion | None = None
    confidence: Confidence = Confidence.MEDIUM
    comparability: Comparability = Field(default_factory=Comparability)
    fallback_reason: str | None = None


class ToolInfo(BaseModel):
    name: str = "labloop"
    version: str


class ReportRound(BaseModel):
    round: int
    analysis: RoundAnalysis | None = None
    runs: list[RunResult] = Field(default_factory=list)


class Report(BaseModel):
    schema_version: str = SCHEMA_VERSION
    tool: ToolInfo
    study_slug: str
    question: str
    generated_at: str
    rounds: list[ReportRound] = Field(default_factory=list)
    best_overall: BestPoint | None = None
    stopped_reason: str | None = None
