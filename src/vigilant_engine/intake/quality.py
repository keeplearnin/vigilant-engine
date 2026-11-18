"""Deterministic backlog quality gate.

Claude drafts the backlog; this module decides whether it is good enough to publish.
Errors block publishing (fail closed). Warnings are shown to the reviewer.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Literal

from .models import Backlog

Severity = Literal["error", "warning", "info"]

# Commas are optional: "As a user I want X so that Y" is as valid as the comma form.
USER_STORY_RE = re.compile(
    r"^\s*as an? .+?,?\s+i want .+?,?\s+so that .+", re.IGNORECASE | re.DOTALL
)
JIRA_SUMMARY_MAX = 255

# Properties a description can promise; each should be tested by an acceptance criterion.
CLAIMS = {
    "encrypted": re.compile(r"\bencrypt(?:s|ed|ing|ion)?\b", re.I),
    "hashed": re.compile(r"\bhash(?:es|ed|ing)?\b", re.I),
    "notified": re.compile(r"\bnotif(?:y|ies|ied|ying|ication|ications)\b", re.I),
    "audited": re.compile(r"\baudit(?:s|ed|ing)?\b", re.I),
    "masked": re.compile(r"\bmask(?:s|ed|ing)?\b", re.I),
    "revoked": re.compile(r"\b(?:revok(?:e|es|ed|ing)|revocation)\b", re.I),
}
# A claim preceded closely by one of these is a negation ("no notification is sent").
NEGATION_RE = re.compile(r"\b(?:no|not|never|without|nor)\b(?:\W+\w+){0,2}\W*$", re.I)


def promises(text: str, pattern: re.Pattern[str]) -> bool:
    """True when `text` asserts the property, ignoring negated mentions."""
    def negated(start: int) -> bool:
        before = re.split(r"[.;:!?\n]", text[max(0, start - 40):start])[-1]
        return NEGATION_RE.search(before) is not None

    return any(not negated(m.start()) for m in pattern.finditer(text))


@dataclass(frozen=True)
class Limits:
    max_epics: int = 8
    max_features_per_epic: int = 6
    max_stories_per_feature: int = 8
    max_total_issues: int = 150


@dataclass(frozen=True)
class Finding:
    severity: Severity
    rule: str
    ref: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def lint(backlog: Backlog, limits: Limits = Limits()) -> list[Finding]:
    findings: list[Finding] = []
    add = findings.append

    # --- Size limits -------------------------------------------------------------
    counts = backlog.counts()
    total = counts["epics"] + counts["features"] + counts["stories"]
    if not backlog.epics:
        add(Finding("error", "no-epics", "backlog", "The backlog has no epics."))
    if counts["epics"] > limits.max_epics:
        add(Finding("error", "too-many-epics", "backlog",
                    f"{counts['epics']} epics; the limit is {limits.max_epics}."))
    if total > limits.max_total_issues:
        add(Finding("error", "too-many-issues", "backlog",
                    f"{total} issues in total; the limit is {limits.max_total_issues}."))
    if backlog.epics and not any(e.in_mvp for e in backlog.epics):
        add(Finding("warning", "no-mvp", "backlog", "No epic is marked as part of the MVP."))
    if backlog.open_questions:
        add(Finding("info", "open-questions", "backlog",
                    f"{len(backlog.open_questions)} open question(s) to resolve before "
                    "sprint planning."))

    # --- Unique refs -------------------------------------------------------------
    seen: dict[str, str] = {}
    for kind, ref in _all_refs(backlog):
        if not ref.strip():
            add(Finding("error", "empty-ref", kind, f"A {kind} has an empty ref."))
        elif ref in seen:
            add(Finding("error", "duplicate-ref", ref,
                        f"Ref '{ref}' is used by more than one item."))
        else:
            seen[ref] = kind

    story_refs = {s.ref for _, _, s in backlog.iter_stories()}

    # --- Per item ----------------------------------------------------------------
    for epic in backlog.epics:
        _check_title(add, epic.ref, epic.title)
        if not epic.features:
            add(Finding("error", "empty-epic", epic.ref, "Epic has no features."))
        if len(epic.features) > limits.max_features_per_epic:
            add(Finding("error", "too-many-features", epic.ref,
                        f"{len(epic.features)} features; the limit is "
                        f"{limits.max_features_per_epic}."))
        if not epic.success_metrics:
            add(Finding("warning", "no-success-metric", epic.ref, "Epic has no success metric."))

        for feature in epic.features:
            _check_title(add, feature.ref, feature.title)
            if not feature.stories:
                add(Finding("error", "empty-feature", feature.ref, "Feature has no stories."))
            if len(feature.stories) > limits.max_stories_per_feature:
                add(Finding("error", "too-many-stories", feature.ref,
                            f"{len(feature.stories)} stories; the limit is "
                            f"{limits.max_stories_per_feature}."))

            for story in feature.stories:
                _check_title(add, story.ref, story.title)
                if story.kind == "user" and not USER_STORY_RE.match(story.user_story):
                    add(Finding("error", "user-story-format", story.ref,
                                "User stories must read 'As a <user>, I want <goal>, "
                                "so that <benefit>'."))
                min_ac = 1 if story.kind == "spike" else 2
                if len(story.acceptance_criteria) < min_ac:
                    add(Finding("error", "acceptance-criteria", story.ref,
                                f"Needs at least {min_ac} acceptance criteria "
                                f"(has {len(story.acceptance_criteria)})."))
                for i, ac in enumerate(story.acceptance_criteria, 1):
                    if not (ac.given.strip() and ac.when.strip() and ac.then.strip()):
                        add(Finding("error", "acceptance-criteria-incomplete", story.ref,
                                    f"Acceptance criterion {i} is missing given/when/then text."))
                ac_text = " ".join(f"{ac.given} {ac.when} {ac.then}"
                                   for ac in story.acceptance_criteria)
                for claim, pattern in CLAIMS.items():
                    if promises(story.description, pattern) and not pattern.search(ac_text):
                        add(Finding("warning", "untested-claim", story.ref,
                                    f"Description says {claim}, but no acceptance criterion "
                                    "tests it."))
                if story.story_points > 5 and (" and " in story.title.lower()
                                               or "," in story.title):
                    add(Finding("warning", "story-too-broad", story.ref,
                                f"{story.story_points} points and the title combines several "
                                "things; consider splitting."))
                if story.story_points == 13:
                    add(Finding("warning", "story-too-large", story.ref,
                                "13 points is too large for one sprint; consider splitting."))
                for dep in story.depends_on:
                    if dep == story.ref:
                        add(Finding("error", "self-dependency", story.ref,
                                    "Story depends on itself."))
                    elif dep not in story_refs:
                        add(Finding("error", "unknown-dependency", story.ref,
                                    f"Depends on unknown story ref '{dep}'."))

    for cycle_ref in _dependency_cycles(backlog):
        add(Finding("error", "dependency-cycle", cycle_ref,
                    "Story is part of a dependency cycle."))
    return findings


def has_errors(findings: list[Finding]) -> bool:
    return any(f.severity == "error" for f in findings)


def _check_title(add, ref: str, title: str) -> None:
    if not title.strip():
        add(Finding("error", "empty-title", ref, "Title is empty."))
    elif len(title) > JIRA_SUMMARY_MAX:
        add(Finding("error", "title-too-long", ref,
                    f"Title is {len(title)} characters; Jira allows {JIRA_SUMMARY_MAX}."))
    elif len(title) > 120:
        add(Finding("warning", "title-long", ref, "Title is over 120 characters."))


def _all_refs(backlog: Backlog):
    for epic in backlog.epics:
        yield "epic", epic.ref
        for feature in epic.features:
            yield "feature", feature.ref
            for story in feature.stories:
                yield "story", story.ref


def _dependency_cycles(backlog: Backlog) -> list[str]:
    graph = {s.ref: [d for d in s.depends_on if d != s.ref] for _, _, s in backlog.iter_stories()}
    state: dict[str, int] = {}  # 0 = visiting, 1 = done
    in_cycle: set[str] = set()

    def visit(node: str, stack: list[str]) -> None:
        state[node] = 0
        stack.append(node)
        for nxt in graph.get(node, []):
            if nxt not in graph:
                continue
            if state.get(nxt) == 0:
                in_cycle.update(stack[stack.index(nxt):])
            elif nxt not in state:
                visit(nxt, stack)
        stack.pop()
        state[node] = 1

    for node in graph:
        if node not in state:
            visit(node, [])
    return sorted(in_cycle)
