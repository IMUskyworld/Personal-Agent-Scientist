"""Backend factory (DEVELOPMENT.md §10): offline / llm / auto with explicit errors."""

from __future__ import annotations

from labloop.design.backend import DesignBackend
from labloop.design.heuristic import HeuristicBackend
from labloop.design.llm import LlmBackend
from labloop.domain.enums import Backend
from labloop.errors import INVALID_ARGUMENT, LLM_DISABLED, DesignError
from labloop.llm import LlmClient


def build_backend(
    requested: str | Backend,
    *,
    client: LlmClient,
    heuristic: DesignBackend | None = None,
    allow_fallback: bool = False,
) -> tuple[DesignBackend, str | None]:
    """Returns (backend, note).

    ``offline`` → heuristics only; ``llm`` → error when not configured, and **no
    silent degradation** (ADR 0001 #4: an explicit LLM request must fail loudly);
    ``auto`` → LLM when ready, otherwise offline with an explicit note.

    ``allow_fallback`` is used by callers that inherit the backend from a study
    (the ``auto`` loop) — there a LLM outage degrades to the heuristics with a
    recorded reason instead of aborting a half-finished run.
    """
    offline = heuristic or HeuristicBackend()
    requested_text = requested.value if isinstance(requested, Backend) else requested
    if requested_text == "auto":
        ready, reason = client.ready()
        if ready:
            return LlmBackend(client, fallback=offline), None
        return offline, f"auto 降级为离线后端：{reason}"
    kind = Backend(requested_text) if requested_text in {item.value for item in Backend} else None
    if kind is None:
        raise DesignError(
            f"未知后端：{requested_text!r}（可选：auto / llm / offline）",
            code=INVALID_ARGUMENT,
            details={"backend": requested_text},
        )
    if kind == Backend.OFFLINE:
        return offline, None
    ready, reason = client.ready()
    if not ready:
        raise DesignError(f"LLM 后端不可用：{reason}", code=LLM_DISABLED)
    return LlmBackend(client, fallback=offline if allow_fallback else None), None
