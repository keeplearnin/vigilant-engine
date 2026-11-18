"""Idea -> backlog planning with a bounded, gate-driven repair loop.

Robustness rules:
* Every attempt is handed to `on_attempt` as soon as it exists, so a later failure or an
  interrupt never loses a draft that was already paid for.
* The result is the best attempt (fewest gate errors, latest on a tie), not simply the last.
* Repairs are an optional improvement: if a repair call fails, planning returns the best
  draft so far and records why, instead of discarding everything.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import anthropic
from pydantic import ValidationError

from ..evidence import EventLog
from ..llm import DEFAULT_MAX_TOKENS, Effort, LLMError, LLMResult, StructuredLLM
from . import prompts
from .models import Backlog, ClarifyingQuestions, IdeaBrief, output_schema
from .quality import Finding, Limits, has_errors, lint

BACKLOG_SCHEMA = output_schema(Backlog)
QUESTIONS_SCHEMA = output_schema(ClarifyingQuestions)
SCOPE_DROP_RATIO = 0.75  # warn when a repair keeps fewer than 75% of the first draft's stories

AttemptCallback = Callable[[int, Backlog, list[Finding]], None]


class PlannerError(RuntimeError):
    pass


@dataclass
class PlanResult:
    backlog: Backlog
    findings: list[Finding]
    attempts: int
    chosen_attempt: int = 1
    repair_error: str | None = None
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
        on_attempt: AttemptCallback | None = None,
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
        self.on_attempt = on_attempt

    def clarify(self, brief: IdeaBrief, max_questions: int = 5) -> ClarifyingQuestions:
        result = self._call("clarify", prompts.clarify_prompt(max_questions), QUESTIONS_SCHEMA,
                            preamble=prompts.brief_block(brief), effort="medium",
                            max_tokens=16000)
        try:
            questions = ClarifyingQuestions.model_validate_json(result.text)
        except ValidationError as exc:
            raise PlannerError(f"Clarifying questions did not match the schema: {exc}") from exc
        questions.questions = questions.questions[:max_questions]
        return questions

    def plan(self, brief: IdeaBrief) -> PlanResult:
        calls: list[dict[str, Any]] = []
        preamble = prompts.brief_block(brief)
        result = self._call("decompose", prompts.decompose_prompt(self.limits), BACKLOG_SCHEMA,
                            preamble=preamble, calls=calls)
        backlog = self._parse(result)
        findings = lint(backlog, self.limits)
        self._record_attempt(1, backlog, findings)

        best = (1, backlog, findings)
        first_story_count = backlog.counts()["stories"]
        attempts, repair_error = 1, None
        while has_errors(findings) and attempts <= self.max_repairs:
            errors = [f for f in findings if f.severity == "error"]
            try:
                result = self._call("repair", prompts.repair_prompt(backlog, errors),
                                    BACKLOG_SCHEMA, preamble=preamble, calls=calls)
                backlog = self._parse(result)
            except (LLMError, PlannerError, anthropic.AnthropicError) as exc:
                repair_error = f"Repair {attempts} failed: {exc}"
                self._emit("intake.repair_failed", {"attempt": attempts + 1, "error": str(exc)})
                break
            attempts += 1
            findings = lint(backlog, self.limits)
            self._record_attempt(attempts, backlog, findings)
            if _error_count(findings) <= _error_count(best[2]):
                best = (attempts, backlog, findings)
            # the next repair (if any) starts from this latest draft and its errors

        chosen, backlog, findings = best
        findings = list(findings)
        stories = backlog.counts()["stories"]
        if first_story_count and stories < SCOPE_DROP_RATIO * first_story_count:
            findings.append(Finding(
                "warning", "scope-reduced", "backlog",
                f"Repairs reduced the backlog from {first_story_count} to {stories} stories; "
                "check that nothing important was dropped."))
        if chosen != attempts:
            self._emit("intake.best_attempt", {"chosen": chosen, "attempts": attempts})
        return PlanResult(backlog=backlog, findings=findings, attempts=attempts,
                          chosen_attempt=chosen, repair_error=repair_error, calls=calls)

    # ------------------------------------------------------------------------------
    def _record_attempt(self, attempt: int, backlog: Backlog, findings: list[Finding]) -> None:
        self._emit("intake.lint", {"attempt": attempt,
                                   "findings": [f.to_dict() for f in findings]})
        if self.on_attempt is not None:
            self.on_attempt(attempt, backlog, findings)

    def _call(
        self,
        step: str,
        prompt: str,
        schema: dict[str, Any],
        *,
        preamble: str | None = None,
        effort: Effort | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        calls: list[dict[str, Any]] | None = None,
    ) -> LLMResult:
        self._emit("model.request", {"step": step, "model": self.llm.model,
                                     "prompt_version": self.prompt_version})
        try:
            result = self.llm.structured(
                system=self.system_prompt,
                preamble=preamble,
                prompt=prompt,
                schema=schema,
                effort=effort or self.effort,
                max_tokens=max_tokens,
            )
        except (LLMError, anthropic.AnthropicError) as exc:
            self._emit("model.error", {"step": step, "error": str(exc),
                                       "kind": type(exc).__name__})
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


def _error_count(findings: list[Finding]) -> int:
    return sum(1 for f in findings if f.severity == "error")
