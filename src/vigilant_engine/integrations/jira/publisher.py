"""Publish an approved backlog to Jira as Epic -> Feature -> Story issues.

Safety properties:
* Dry run by default. Nothing is written unless the caller passes ``dry_run=False``.
* Preflight checks that the issue types exist and that the project's hierarchy allows the
  parent links, before the first write.
* A hard cap on issues per run.
* Resumable: every created key is saved to a manifest straight away, so a failed run can be
  re-run without creating duplicates. A changed backlog cannot resume a partial publish.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ...evidence import EventLog, digest
from ...intake.models import Backlog, Epic, Feature, Story
from ...intake.render import display_ids
from . import adf
from .client import JiraClient

FeatureMode = Literal["auto", "issue-type", "label"]
DEFAULT_LEVELS = {"epic": 1, "story": 0}


class PublishError(RuntimeError):
    pass


@dataclass
class PublishOptions:
    project_key: str
    run_id: str
    epic_type: str = "Epic"
    feature_type: str = "Feature"
    story_type: str = "Story"
    feature_mode: FeatureMode = "auto"
    story_points_field: str | None = None
    extra_labels: tuple[str, ...] = ()
    max_issues: int = 150


@dataclass
class PlannedIssue:
    local_id: str
    ref: str
    level: Literal["epic", "feature", "story"]
    issue_type: str
    summary: str
    description: dict[str, Any]
    labels: list[str]
    parent_local_id: str | None
    story_points: int | None = None


@dataclass
class PublishReport:
    feature_mode: Literal["issue-type", "label"]
    planned: list[PlannedIssue]
    created: dict[str, str] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = True


def resolve_feature_mode(
    project: dict[str, Any] | None, opts: PublishOptions
) -> tuple[Literal["issue-type", "label"], list[str]]:
    """Decide whether features become their own issues or labels on stories."""
    warnings: list[str] = []
    if project is None:  # offline dry run
        mode = "label" if opts.feature_mode == "label" else "issue-type"
        warnings.append("Preflight skipped (no Jira connection); hierarchy not verified.")
        return mode, warnings

    types = {t["name"].lower(): t for t in project.get("issueTypes", []) if not t.get("subtask")}
    for required in (opts.epic_type, opts.story_type):
        if required.lower() not in types:
            raise PublishError(
                f"Issue type '{required}' is not available in project {opts.project_key}. "
                f"Available: {', '.join(sorted(t['name'] for t in types.values()))}."
            )

    def level(name: str, fallback_key: str | None = None) -> int | None:
        t = types.get(name.lower())
        if t is None:
            return None
        if "hierarchyLevel" in t:
            return t["hierarchyLevel"]
        return DEFAULT_LEVELS.get(fallback_key) if fallback_key else None

    epic_lvl = level(opts.epic_type, "epic")
    story_lvl = level(opts.story_type, "story")
    feat_lvl = level(opts.feature_type)
    feature_exists = opts.feature_type.lower() in types
    ordered = (
        feature_exists
        and None not in (epic_lvl, feat_lvl, story_lvl)
        and epic_lvl > feat_lvl > story_lvl  # type: ignore[operator]
    )

    if opts.feature_mode == "label":
        return "label", warnings
    if opts.feature_mode == "issue-type":
        if not feature_exists:
            raise PublishError(
                f"Issue type '{opts.feature_type}' does not exist in {opts.project_key}. "
                "Use --feature-mode label, or ask your Jira admin to add it."
            )
        if not ordered:
            raise PublishError(
                f"The hierarchy in {opts.project_key} does not allow "
                f"{opts.epic_type} > {opts.feature_type} > {opts.story_type} "
                f"(levels {epic_lvl} / {feat_lvl} / {story_lvl}). Use --feature-mode label."
            )
        return "issue-type", warnings
    if ordered:
        return "issue-type", warnings
    warnings.append(
        f"'{opts.feature_type}' is not available between {opts.epic_type} and "
        f"{opts.story_type} in {opts.project_key}; features will be labels on their stories."
    )
    return "label", warnings


def plan_issues(
    backlog: Backlog, opts: PublishOptions, mode: Literal["issue-type", "label"]
) -> list[PlannedIssue]:
    ids = display_ids(backlog)
    titles = {s.ref: s.title for _, _, s in backlog.iter_stories()}
    base_labels = [_label("ai-generated"), _label(f"vigilant-{opts.run_id}")]
    base_labels += [_label(x) for x in opts.extra_labels]
    issues: list[PlannedIssue] = []

    for epic in backlog.epics:
        issues.append(PlannedIssue(
            local_id=ids[epic.ref], ref=epic.ref, level="epic", issue_type=opts.epic_type,
            summary=_summary(epic.title), description=_epic_doc(epic, ids, opts.run_id),
            labels=_dedupe(base_labels + [_label(f"moscow-{epic.priority}")]
                           + ([_label("mvp")] if epic.in_mvp else [])),
            parent_local_id=None,
        ))
        for feature in epic.features:
            feature_label = _label(f"feature-{feature.ref}")
            if mode == "issue-type":
                issues.append(PlannedIssue(
                    local_id=ids[feature.ref], ref=feature.ref, level="feature",
                    issue_type=opts.feature_type, summary=_summary(feature.title),
                    description=_feature_doc(feature, opts.run_id),
                    labels=_dedupe(base_labels + [_label(f"moscow-{feature.priority}")]),
                    parent_local_id=ids[epic.ref],
                ))
            for story in feature.stories:
                labels = base_labels + [_label(f"moscow-{story.priority}"),
                                        _label(f"kind-{story.kind}")]
                labels += [_label(x) for x in story.labels]
                if mode == "label":
                    labels.append(feature_label)
                issues.append(PlannedIssue(
                    local_id=ids[story.ref], ref=story.ref, level="story",
                    issue_type=opts.story_type, summary=_summary(story.title),
                    description=_story_doc(story, feature, ids, titles, opts.run_id, mode),
                    labels=_dedupe(labels),
                    parent_local_id=ids[feature.ref] if mode == "issue-type" else ids[epic.ref],
                    story_points=story.story_points,
                ))
    return issues


class JiraPublisher:
    def __init__(self, client: JiraClient | None, manifest_path: Path,
                 events: EventLog | None = None):
        self.client = client
        self.manifest_path = manifest_path
        self.events = events

    def publish(self, backlog: Backlog, opts: PublishOptions, *, dry_run: bool = True
                ) -> PublishReport:
        project = None
        if self.client is not None:
            self.client.myself()  # fails fast on bad credentials
            project = self.client.get_project(opts.project_key)
        mode, warnings = resolve_feature_mode(project, opts)
        planned = plan_issues(backlog, opts, mode)
        if len(planned) > opts.max_issues:
            raise PublishError(f"{len(planned)} issues planned; the per-run cap is "
                               f"{opts.max_issues}. Raise --max-issues deliberately if needed.")

        report = PublishReport(feature_mode=mode, planned=planned, warnings=warnings,
                               dry_run=dry_run)
        if dry_run:
            self._emit("jira.dry_run", {"project": opts.project_key, "mode": mode,
                                        "planned": len(planned)})
            return report
        if self.client is None:
            raise PublishError("A Jira connection is required to publish.")

        manifest = self._load_manifest(backlog, opts)
        keys: dict[str, str] = dict(manifest["created"])
        for issue in planned:
            if issue.local_id in keys:
                report.skipped[issue.local_id] = keys[issue.local_id]
                continue
            fields: dict[str, Any] = {
                "project": {"key": opts.project_key},
                "issuetype": {"name": issue.issue_type},
                "summary": issue.summary,
                "description": issue.description,
                "labels": issue.labels,
            }
            if issue.parent_local_id:
                fields["parent"] = {"key": keys[issue.parent_local_id]}
            if opts.story_points_field and issue.story_points is not None:
                fields[opts.story_points_field] = issue.story_points
            key = self.client.create_issue(fields)
            keys[issue.local_id] = key
            report.created[issue.local_id] = key
            manifest["created"][issue.local_id] = key
            self._save_manifest(manifest)
            self._emit("jira.issue_created", {"local_id": issue.local_id, "key": key,
                                              "type": issue.issue_type})
        return report

    # ------------------------------------------------------------------------------
    def _load_manifest(self, backlog: Backlog, opts: PublishOptions) -> dict[str, Any]:
        backlog_digest = digest(backlog.model_dump())
        if self.manifest_path.exists():
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if manifest["project"] != opts.project_key:
                raise PublishError(f"This run was already published to {manifest['project']}.")
            if manifest["backlog_digest"] != backlog_digest and manifest["created"]:
                raise PublishError(
                    "The backlog changed after a partial publish. Resolve the issues already "
                    "created (see jira-manifest.json) before publishing again."
                )
            manifest["backlog_digest"] = backlog_digest
            return manifest
        return {"project": opts.project_key, "run_id": opts.run_id,
                "backlog_digest": backlog_digest, "created": {}}

    def _save_manifest(self, manifest: dict[str, Any]) -> None:
        tmp = self.manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.manifest_path)

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.events is not None:
            self.events.append(event_type, actor="system:jira-publisher", payload=payload)


# --- Formatting helpers ----------------------------------------------------------------

def _label(value: str) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "-", value.strip().lower()).strip("-")[:255]


def _dedupe(labels: list[str]) -> list[str]:
    return list(dict.fromkeys(label for label in labels if label))


def _summary(title: str) -> str:
    title = " ".join(title.split())
    return title if len(title) <= 255 else title[:252] + "..."


def _footer(run_id: str) -> list[dict[str, Any] | None]:
    return [adf.rule(), adf.paragraph(adf.text(
        f"Drafted by the vigilant-engine intake agent (run {run_id}). "
        "Review and refine before sprint planning.", italic=True))]


def _epic_doc(epic: Epic, ids: dict[str, str], run_id: str) -> dict[str, Any]:
    return adf.doc(
        adf.paragraph(epic.summary),
        adf.heading("Business outcome"), adf.paragraph(epic.business_outcome),
        adf.heading("Success metrics"), adf.bullet_list(epic.success_metrics),
        adf.heading("Features"),
        adf.bullet_list([f"{ids[f.ref]} {f.title}" for f in epic.features]),
        *_footer(run_id),
    )


def _feature_doc(feature: Feature, run_id: str) -> dict[str, Any]:
    return adf.doc(
        adf.paragraph(feature.description),
        adf.heading("Benefit hypothesis"), adf.paragraph(feature.benefit_hypothesis),
        *_footer(run_id),
    )


def _story_doc(story: Story, feature: Feature, ids: dict[str, str], titles: dict[str, str],
               run_id: str, mode: str) -> dict[str, Any]:
    criteria = [
        adf.paragraph(adf.text("Given ", bold=True), ac.given, adf.text(", when ", bold=True),
                      ac.when, adf.text(", then ", bold=True), ac.then)
        for ac in story.acceptance_criteria
    ]
    deps = [f"{ids.get(d, d)} {titles.get(d, '')}".strip() for d in story.depends_on]
    blocks = [
        adf.paragraph(adf.text(story.user_story, bold=True)),
        adf.paragraph(story.description),
        adf.heading("Acceptance criteria"), adf.bullet_list(criteria),
    ]
    if deps:
        blocks += [adf.heading("Depends on"), adf.bullet_list(deps)]
    if mode == "label":
        blocks += [adf.paragraph(adf.text("Feature: ", bold=True), feature.title)]
    return adf.doc(*blocks, *_footer(run_id))
