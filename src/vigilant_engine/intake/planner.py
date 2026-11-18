"""Idea -> backlog planning with a bounded, gate-driven repair loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from ..evidence import EventLog
from ..llm import Effort, LLMError, LLMResult, StructuredLLM
from . import prompts
from .models import Backlog, ClarifyingQuestions, IdeaBrief, output_schema
from .quality import Finding, Limits, has_errors, lint

BACKLOG_SCHEMA = output_schema(Backlog)
QUESTIONS_SCHEMA = output_schema(ClarifyingQuestions)


class PlannerError(RuntimeError):
    pass


@dataclass
class PlanResult:
    backlog: Backlog
    findings: list[Finding]
    attempts: int
    calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def publishable(self) -> bool:
        return not has_errors(self.findings)


class BacklogPlanner:
    def __init__(
        self,
        llm: StructuredLLM,
        *,
        limits: Limits | None = None,
        max_repairs: int = 2,
        effort: Effort = "high",
        events: EventLog | None = None,
        prompt_version: str = prompts.DEFAULT_PROMPT_VERSION,
    ):
        if prompt_version not in prompts.PROMPTS:
            raise ValueError(f"Unknown prompt version {prompt_version!r}; "
                             f"choose from {sorted(prompts.PROMPTS)}.")
        self.prompt_version = prompt_version
        self.system_prompt = prompts.PROMPTS[prompt_version]
        self.llm = llm
        self.limits = limits or Limits()
        self.max_repairs = max_repairs
        self.effort = effort
        self.events = events

    def clarify(self, brief: IdeaBrief, max_questions: int = 5) -> ClarifyingQuestions:
        result = self._call("clarify", prompts.clarify_prompt(brief, max_questions),
                            QUESTIONS_SCHEMA, effort="medium", max_tokens=16000)
        try:
            questions = ClarifyingQuestions.model_validate_json(result.text)
        except ValidationError as exc:
            raise PlannerError(f"Clarifying questions did not match the schema: {exc}") from exc
        questions.questions = questions.questions[:max_questions]
        return questions

    def plan(self, brief: IdeaBrief) -> PlanResult:
        calls: list[dict[str, Any]] = []
        result = self._call("decompose", prompts.decompose_prompt(brief, self.limits),
                            BACKLOG_SCHEMA, calls=calls)
        backlog = self._parse(result)
        findings = lint(backlog, self.limits)
        self._emit("intake.lint", {"attempt": 1, "findings": [f.to_dict() for f in findings]})

        attempts = 1
        while has_errors(findings) and attempts <= self.max_repairs:
            errors = [f for f in findings if f.severity == "error"]
            result = self._call("repair", prompts.repair_prompt(brief, backlog, errors),
                                BACKLOG_SCHEMA, calls=calls)
            backlog = self._parse(result)
            attempts += 1
            findings = lint(backlog, self.limits)
            self._emit("intake.lint",
                       {"attempt": attempts, "findings": [f.to_dict() for f in findings]})

        return PlanResult(backlog=backlog, findings=findings, attempts=attempts, calls=calls)

    # ------------------------------------------------------------------------------
    def _call(
        self,
        step: str,
        prompt: str,
        schema: dict[str, Any],
        *,
        effort: Effort | None = None,
        max_tokens: int = 64000,
        calls: list[dict[str, Any]] | None = None,
    ) -> LLMResult:
        self._emit("model.request", {"step": step, "model": self.llm.model,
                                     "prompt_version": self.prompt_version})
        try:
            result = self.llm.structured(
                system=self.system_prompt,
                prompt=prompt,
                schema=schema,
                effort=effort or self.effort,
                max_tokens=max_tokens,
            )
        except LLMError as exc:
            self._emit("model.error", {"step": step, "error": str(exc)})
            raise
        record = {
            "step": step,
            "prompt_version": self.prompt_version,
            "model": result.model,
            "stop_reason": result.stop_reason,
            "fell_back": result.fell_back,
            "usage": result.usage,
            "request_id": result.request_id,
        }
        if calls is not None:
            calls.append(record)
        self._emit("model.response", record)
        return result

    def _parse(self, result: LLMResult) -> Backlog:
        try:
            return Backlog.model_validate_json(result.text)
        except ValidationError as exc:
            raise PlannerError(f"Backlog did not match the schema: {exc}") from exc

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.events is not None:
            self.events.append(event_type, actor="agent:intake", payload=payload)
