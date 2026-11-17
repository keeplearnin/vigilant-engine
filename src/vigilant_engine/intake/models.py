"""Domain models for the idea-intake agent.

Every model that Claude fills in is sent as a JSON schema through structured outputs
(`output_config.format`). Structured outputs require `additionalProperties: false` on
every object and do not support numeric or string-length constraints, so:

* every model forbids extra fields (which emits `additionalProperties: false`);
* every field is required (no defaults leak into the schema);
* size and count limits are enforced afterwards by `intake.quality`, not by the schema.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

Priority = Literal["must", "should", "could"]
StoryPoints = Literal[1, 2, 3, 5, 8, 13]
StoryKind = Literal["user", "enabler", "spike"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AcceptanceCriterion(_Strict):
    given: str
    when: str
    then: str


class Story(_Strict):
    ref: str
    title: str
    kind: StoryKind
    user_story: str
    description: str
    acceptance_criteria: list[AcceptanceCriterion]
    story_points: StoryPoints
    priority: Priority
    depends_on: list[str]
    labels: list[str]


class Feature(_Strict):
    ref: str
    title: str
    description: str
    benefit_hypothesis: str
    priority: Priority
    stories: list[Story]


class Epic(_Strict):
    ref: str
    title: str
    summary: str
    business_outcome: str
    success_metrics: list[str]
    priority: Priority
    in_mvp: bool
    features: list[Feature]


class Backlog(_Strict):
    product_name: str
    problem_statement: str
    vision: str
    target_users: list[str]
    epics: list[Epic]
    assumptions: list[str]
    open_questions: list[str]
    out_of_scope: list[str]
    risks: list[str]

    def iter_stories(self):
        for epic in self.epics:
            for feature in epic.features:
                for story in feature.stories:
                    yield epic, feature, story

    def counts(self) -> dict[str, int]:
        features = sum(len(e.features) for e in self.epics)
        stories = sum(1 for _ in self.iter_stories())
        points = sum(s.story_points for _, _, s in self.iter_stories())
        return {
            "epics": len(self.epics),
            "features": features,
            "stories": stories,
            "story_points": points,
        }


class ClarifyingQuestion(_Strict):
    question: str
    why_it_matters: str
    default_assumption: str


class ClarifyingQuestions(_Strict):
    questions: list[ClarifyingQuestion]


class IdeaBrief(BaseModel):
    """What the requester gives us. Never sent as a schema."""

    idea: str
    context: str = ""
    answers: list[tuple[str, str]] = []


# Keys the structured-outputs JSON schema subset does not accept.
_UNSUPPORTED_KEYS = {
    "default",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "pattern",
    "minItems",
    "maxItems",
    "uniqueItems",
}


def output_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON schema for `model`, checked against the structured-outputs subset."""
    schema = model.model_json_schema()
    _check(schema, path="$")
    return schema


def _check(node: Any, path: str) -> None:
    if isinstance(node, dict):
        bad = _UNSUPPORTED_KEYS & node.keys()
        if bad:
            raise ValueError(f"{path}: unsupported schema keys {sorted(bad)}")
        if node.get("type") == "object" and node.get("additionalProperties") is not False:
            raise ValueError(f"{path}: objects must set additionalProperties: false")
        for key, value in node.items():
            _check(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            _check(value, f"{path}[{i}]")
