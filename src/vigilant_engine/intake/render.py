"""Human-readable review of a backlog, written next to backlog.json for approval."""

from __future__ import annotations

from .models import Backlog
from .quality import Finding

_PRIORITY = {"must": "Must", "should": "Should", "could": "Could"}


def display_ids(backlog: Backlog) -> dict[str, str]:
    """Stable, position-based IDs (E1, F1.2, S1.2.3) keyed by ref."""
    ids: dict[str, str] = {}
    for e, epic in enumerate(backlog.epics, 1):
        ids[epic.ref] = f"E{e}"
        for f, feature in enumerate(epic.features, 1):
            ids[feature.ref] = f"F{e}.{f}"
            for s, story in enumerate(feature.stories, 1):
                ids[story.ref] = f"S{e}.{f}.{s}"
    return ids


def to_markdown(backlog: Backlog, findings: list[Finding], run_id: str) -> str:
    ids = display_ids(backlog)
    c = backlog.counts()
    out: list[str] = [
        f"# {backlog.product_name}: backlog for review",
        "",
        f"Run `{run_id}` · {c['epics']} epics · {c['features']} features · "
        f"{c['stories']} stories · {c['story_points']} points",
        "",
        "## Problem", backlog.problem_statement, "",
        "## Vision", backlog.vision, "",
        "## Target users", *[f"- {u}" for u in backlog.target_users], "",
    ]

    errors = [f for f in findings if f.severity == "error"]
    warnings = [f for f in findings if f.severity != "error"]
    out += ["## Quality gate", ""]
    out.append("**Blocked: fix the errors below before publishing.**" if errors
               else "**Passed.** Warnings are listed for the reviewer.")
    out.append("")
    for f in errors + warnings:
        label = ids.get(f.ref, f.ref)
        out.append(f"- `{f.severity}` {label}: {f.message} _({f.rule})_")
    out.append("")

    for epic in backlog.epics:
        mvp = " · MVP" if epic.in_mvp else ""
        out += [f"## {ids[epic.ref]} {epic.title}",
                f"_Epic · {_PRIORITY[epic.priority]}{mvp}_", "",
                epic.summary, "",
                f"**Outcome:** {epic.business_outcome}", ""]
        if epic.success_metrics:
            out += ["**Success metrics**", *[f"- {m}" for m in epic.success_metrics], ""]
        for feature in epic.features:
            pts = sum(s.story_points for s in feature.stories)
            out += [f"### {ids[feature.ref]} {feature.title}",
                    f"_Feature · {_PRIORITY[feature.priority]} · {pts} points_", "",
                    feature.description, "",
                    f"**Benefit hypothesis:** {feature.benefit_hypothesis}", ""]
            for story in feature.stories:
                out += [f"#### {ids[story.ref]} {story.title}",
                        f"_{story.kind.capitalize()} story · {_PRIORITY[story.priority]} · "
                        f"{story.story_points} pts_", "",
                        story.user_story, ""]
                if story.description.strip():
                    out += [story.description, ""]
                out.append("Acceptance criteria:")
                out += [f"- **Given** {ac.given}, **when** {ac.when}, **then** {ac.then}"
                        for ac in story.acceptance_criteria]
                if story.depends_on:
                    deps = ", ".join(ids.get(d, d) for d in story.depends_on)
                    out += ["", f"Depends on: {deps}"]
                out.append("")

    for title, items in (("Assumptions", backlog.assumptions),
                         ("Open questions", backlog.open_questions),
                         ("Out of scope", backlog.out_of_scope),
                         ("Risks", backlog.risks)):
        if items:
            out += [f"## {title}", *[f"- {i}" for i in items], ""]
    return "\n".join(out)
