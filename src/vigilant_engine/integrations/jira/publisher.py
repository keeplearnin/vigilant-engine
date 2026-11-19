"""Publish an approved backlog to Jira as Epic -> Feature -> Story issues.

Two steps, so that what a person approves is exactly what runs:

1. `prepare()` connects, checks issue types and hierarchy, and builds a `PublishPlan`.
   It writes nothing.
2. `execute(plan)` creates the issues in that plan. It does not re-plan.

Safety properties:
* A hard cap on issues per run.
* Resumable without duplicates: each created key is saved to a manifest straight away.
  The manifest also pins the project, feature mode and issue types, and a changed backlog
  cannot resume a partial publish.
* Ambiguous writes are never retried blindly. If Jira may have created an issue but the
  response was lost, the issue is recorded as uncertain and the run stops. The next run
  waits for Jira's search index, looks for the issue by the run label, and either adopts it
  or creates it.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ...evidence import EventLog, digest
from ...intake.models import Backlog, Epic, Feature, Story
from ...intake.render import display_ids
from . import adf
from .client import JiraAmbiguousWriteError, JiraClient

FeatureMode = Literal["auto", "issue-type", "label"]
ResolvedMode = Literal["issue-type", "label"]
DEFAULT_LEVELS = {"epic": 1, "story": 0}
SEARCH_SETTLE_SECONDS = 60


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

    def issue_types(self) -> dict[str, str]:
        return {"epic": self.epic_type, "feature": self.feature_type, "story": self.story_type}


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
    # (local_id, title) of the stories this one depends on; rewritten to Jira keys at execute
    depends_on: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class PublishPlan:
    """Exactly what will be created. Built by `prepare()`, approved, then executed."""

    project_key: str
    run_id: str
    run_label: str
    feature_mode: ResolvedMode
    issue_types: dict[str, str]
    story_points_field: str | None
    backlog_digest: str
    issues: list[PlannedIssue]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PublishPlan:
        data = dict(data)
        data["issues"] = [
            PlannedIssue(**{**i, "depends_on": [tuple(d) for d in i.get("depends_on", [])]})
            for i in data["issues"]
        ]
        return cls(**data)

    @property
    def digest(self) -> str:
        return digest({
            "project": self.project_key,
            "mode": self.feature_mode,
            "issue_types": self.issue_types,
            "story_points_field": self.story_points_field,
            "issues": [asdict(i) for i in self.issues],
        })


@dataclass
class PublishReport:
    feature_mode: ResolvedMode
    planned: list[PlannedIssue]
    created: dict[str, str] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    reconciled: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = True


def resolve_feature_mode(
    project: dict[str, Any] | None, opts: PublishOptions
) -> tuple[ResolvedMode, list[str]]:
    """Decide whether features become their own issues or labels on stories."""
    warnings: list[str] = []
    if project is None:  # offline dry run
        mode: ResolvedMode = "label" if opts.feature_mode == "label" else "issue-type"
        warnings.append("Preflight skipped (no Jira connection); hierarchy not verified.")
        return mode, warnings

    types = {t["name"].lower(): t for t in project.get("issueTypes", [])}
    available = ", ".join(sorted(t["name"] for t in types.values()))
    for role, required in (("epic", opts.epic_type), ("story", opts.story_type)):
        if required.lower() not in types:
            raise PublishError(
                f"Issue type '{required}' is not available in project {opts.project_key}. "
                f"Available: {available}. Choose one with --{role}-type."
            )
    if types[opts.epic_type.lower()].get("subtask"):
        raise PublishError(f"'{opts.epic_type}' is a sub-task type and cannot hold epics.")
    feature_type = types.get(opts.feature_type.lower())
    if feature_type is not None and feature_type.get("subtask"):
        feature_type = None  # a sub-task type cannot have children
    story_is_subtask = bool(types[opts.story_type.lower()].get("subtask"))

    def level(name: str, fallback_key: str | None = None) -> int | None:
        t = types.get(name.lower())
        if t is None:
            return None
        if "hierarchyLevel" in t:
            return t["hierarchyLevel"]
        if t.get("subtask"):
            return -1
        return DEFAULT_LEVELS.get(fallback_key) if fallback_key else None

    epic_lvl = level(opts.epic_type, "epic")
    story_lvl = level(opts.story_type, "story")
    feat_lvl = level(opts.feature_type) if feature_type is not None else None
    feature_exists = feature_type is not None
    ordered = (
        feature_exists
        and None not in (epic_lvl, feat_lvl, story_lvl)
        and epic_lvl > feat_lvl > story_lvl  # type: ignore[operator]
    )
    subtask_needs_features = (
        f"'{opts.story_type}' is a sub-task type, so stories must sit under a "
        f"'{opts.feature_type}' issue; they cannot go directly under an epic."
    )

    if opts.feature_mode == "label":
        if story_is_subtask:
            raise PublishError(subtask_needs_features + " Use --feature-mode issue-type.")
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
    if story_is_subtask:
        raise PublishError(subtask_needs_features)
    warnings.append(
        f"'{opts.feature_type}' is not available between {opts.epic_type} and "
        f"{opts.story_type} in {opts.project_key}; features will be labels on their stories."
    )
    return "label", warnings


def run_label(run_id: str) -> str:
    return _label(f"vigilant-{run_id}")


def plan_issues(backlog: Backlog, opts: PublishOptions, mode: ResolvedMode) -> list[PlannedIssue]:
    ids = display_ids(backlog)
    titles = {s.ref: s.title for _, _, s in backlog.iter_stories()}
    base_labels = [_label("ai-generated"), run_label(opts.run_id)]
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
                    depends_on=[(ids[d], titles.get(d, "")) for d in story.depends_on
                                if d in ids],
                ))
    return issues


def creation_order(issues: list[PlannedIssue]) -> list[PlannedIssue]:
    """Epics, then features, then stories with each story after the stories it depends on,
    so dependencies can be written as real Jira keys. Display order is unchanged."""
    epics = [i for i in issues if i.level == "epic"]
    features = [i for i in issues if i.level == "feature"]
    stories = [i for i in issues if i.level == "story"]
    pending = {s.local_id for s in stories}
    ordered: list[PlannedIssue] = []
    while stories:
        ready = [s for s in stories if not any(d in pending for d, _ in s.depends_on)]
        if not ready:  # a cycle (the quality gate blocks these); keep the remaining order
            ready = stories
        for s in ready:
            ordered.append(s)
            pending.discard(s.local_id)
        stories = [s for s in stories if s not in ready]
    return epics + features + ordered


def finalize_description(issue: PlannedIssue, keys: dict[str, str],
                         browse_url: Callable[[str], str], run_id: str,
                         anchor: str | None) -> dict[str, Any]:
    """Swap dependency IDs for linked Jira keys and add the approval anchor."""
    doc = copy.deepcopy(issue.description)
    replacements = {
        f"{local} {title}".strip(): (keys[local], title)
        for local, title in issue.depends_on if local in keys
    }
    if replacements:
        _replace_dependency_text(doc, replacements, browse_url)
    if anchor:
        doc["content"].append(adf.paragraph(adf.text(
            f"Approval record {anchor} (check with: vigilant intake verify --run "
            f"runs/{run_id} --expect-head {anchor})", italic=True)))
    return doc


def _replace_dependency_text(node: Any, replacements: dict[str, tuple[str, str]],
                             browse_url: Callable[[str], str]) -> None:
    if isinstance(node, dict):
        content = node.get("content")
        if (node.get("type") == "paragraph" and isinstance(content, list) and len(content) == 1
                and content[0].get("type") == "text" and content[0].get("text") in replacements):
            key, title = replacements[content[0]["text"]]
            link = {"type": "text", "text": key,
                    "marks": [{"type": "link", "attrs": {"href": browse_url(key)}}]}
            node["content"] = [link] + ([{"type": "text", "text": f" {title}"}] if title else [])
            return
        for child in content or []:
            _replace_dependency_text(child, replacements, browse_url)


class JiraPublisher:
    def __init__(
        self,
        client: JiraClient | None,
        manifest_path: Path,
        events: EventLog | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        settle_seconds: int = SEARCH_SETTLE_SECONDS,
    ):
        self.client = client
        self.manifest_path = manifest_path
        self.events = events
        self.clock = clock
        self.settle_seconds = settle_seconds

    # --- step 1: plan (no writes) ---------------------------------------------------------

    def prepare(self, backlog: Backlog, opts: PublishOptions) -> PublishPlan:
        opts = self._pin_to_manifest(opts)
        project = None
        if self.client is not None:
            self.client.myself()  # fails fast on bad credentials
            project = self.client.get_project(opts.project_key)
        mode, warnings = resolve_feature_mode(project, opts)
        issues = plan_issues(backlog, opts, mode)
        if len(issues) > opts.max_issues:
            raise PublishError(f"{len(issues)} issues planned; the per-run cap is "
                               f"{opts.max_issues}. Raise --max-issues deliberately if needed.")
        plan = PublishPlan(
            project_key=opts.project_key, run_id=opts.run_id, run_label=run_label(opts.run_id),
            feature_mode=mode, issue_types=opts.issue_types(),
            story_points_field=opts.story_points_field,
            backlog_digest=digest(backlog.model_dump()), issues=issues, warnings=warnings,
        )
        self._emit("jira.planned", {"project": plan.project_key, "mode": mode,
                                    "issues": len(issues), "plan_digest": plan.digest})
        return plan

    # --- step 2: execute the approved plan --------------------------------------------------

    def execute(self, plan: PublishPlan, *, anchor: str | None = None) -> PublishReport:
        """Create the issues in `plan`. `anchor` (the approval event's hash) is written into
        every created issue, so the run's evidence log can later be checked against Jira."""
        if self.client is None:
            raise PublishError("A Jira connection is required to publish.")
        report = PublishReport(feature_mode=plan.feature_mode, planned=plan.issues,
                               warnings=list(plan.warnings), dry_run=False)
        manifest = self._load_manifest(plan)
        report.reconciled = self._reconcile(manifest, plan)
        keys: dict[str, str] = dict(manifest["created"])

        for issue in creation_order(plan.issues):
            if issue.local_id in keys:
                if issue.local_id not in report.reconciled:
                    report.skipped[issue.local_id] = keys[issue.local_id]
                continue
            fields: dict[str, Any] = {
                "project": {"key": plan.project_key},
                "issuetype": {"name": issue.issue_type},
                "summary": issue.summary,
                "description": finalize_description(issue, keys, self.client.browse_url,
                                                    plan.run_id, anchor),
                "labels": issue.labels,
            }
            if issue.parent_local_id:
                fields["parent"] = {"key": keys[issue.parent_local_id]}
            if plan.story_points_field and issue.story_points is not None:
                fields[plan.story_points_field] = issue.story_points
            try:
                key = self.client.create_issue(fields)
            except JiraAmbiguousWriteError as exc:
                manifest["uncertain"][issue.local_id] = {
                    "summary": issue.summary,
                    "issue_type": issue.issue_type,
                    "at": self.clock().isoformat(),
                }
                self._save_manifest(manifest)
                self._emit("jira.write_uncertain", {"local_id": issue.local_id,
                                                    "error": str(exc)})
                raise PublishError(
                    f"Jira may or may not have created {issue.local_id} '{issue.summary}' "
                    f"({exc}). Wait a minute, then run the same command again: it will look "
                    "for the issue in Jira and continue without creating a duplicate."
                ) from exc
            keys[issue.local_id] = key
            report.created[issue.local_id] = key
            manifest["created"][issue.local_id] = key
            self._save_manifest(manifest)
            self._emit("jira.issue_created", {"local_id": issue.local_id, "key": key,
                                              "type": issue.issue_type})
        return report

    def publish(self, backlog: Backlog, opts: PublishOptions, *, dry_run: bool = True
                ) -> PublishReport:
        """Prepare and, unless `dry_run`, execute in one call."""
        plan = self.prepare(backlog, opts)
        if dry_run:
            return PublishReport(feature_mode=plan.feature_mode, planned=plan.issues,
                                 warnings=plan.warnings, dry_run=True)
        return self.execute(plan)

    # --- manifest ----------------------------------------------------------------------------

    def _read_manifest(self) -> dict[str, Any] | None:
        if not self.manifest_path.exists():
            return None
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest.setdefault("uncertain", {})
        return manifest

    @staticmethod
    def _started(manifest: dict[str, Any] | None) -> bool:
        return bool(manifest and (manifest["created"] or manifest["uncertain"]))

    def _pin_to_manifest(self, opts: PublishOptions) -> PublishOptions:
        """A partly published run must resume with the same project, types and mode."""
        manifest = self._read_manifest()
        if not self._started(manifest):
            return opts
        assert manifest is not None
        if manifest["project"] != opts.project_key:
            raise PublishError(f"This run was already published to {manifest['project']}.")
        pinned_types = manifest.get("issue_types")
        if pinned_types and pinned_types != opts.issue_types():
            raise PublishError(
                f"This run was partly published with issue types {pinned_types}; "
                "use the same --epic-type, --feature-type and --story-type."
            )
        pinned_mode = manifest.get("feature_mode")
        if pinned_mode and opts.feature_mode not in ("auto", pinned_mode):
            raise PublishError(
                f"This run was partly published in {pinned_mode} mode; resume with "
                f"--feature-mode {pinned_mode} (or auto)."
            )
        return replace(opts, feature_mode=pinned_mode) if pinned_mode else opts

    def _load_manifest(self, plan: PublishPlan) -> dict[str, Any]:
        manifest = self._read_manifest()
        if not self._started(manifest):
            return {
                "project": plan.project_key, "run_id": plan.run_id,
                "backlog_digest": plan.backlog_digest, "feature_mode": plan.feature_mode,
                "issue_types": plan.issue_types, "created": {}, "uncertain": {},
            }
        assert manifest is not None
        if manifest["project"] != plan.project_key:
            raise PublishError(f"This run was already published to {manifest['project']}.")
        if manifest["backlog_digest"] != plan.backlog_digest:
            raise PublishError(
                "The backlog changed after a partial publish. Resolve the issues already "
                "created (see jira-manifest.json) before publishing again."
            )
        if manifest.get("feature_mode", plan.feature_mode) != plan.feature_mode:
            raise PublishError("The plan's feature mode differs from the partial publish.")
        return manifest

    def _reconcile(self, manifest: dict[str, Any], plan: PublishPlan) -> dict[str, str]:
        """Resolve writes whose outcome was unknown, by searching Jira for them."""
        uncertain: dict[str, dict[str, str]] = manifest["uncertain"]
        if not uncertain:
            return {}
        now = self.clock()
        for local_id, record in uncertain.items():
            waited = (now - datetime.fromisoformat(record["at"])).total_seconds()
            if waited < self.settle_seconds:
                raise PublishError(
                    f"{local_id} was written moments ago with an unknown outcome. Wait "
                    f"{int(self.settle_seconds - waited) + 1} more seconds so Jira's search "
                    "index catches up, then run again."
                )
        assert self.client is not None
        found = self.client.search_issues(
            f'project = "{plan.project_key}" AND labels = "{plan.run_label}"',
            ["summary", "issuetype"],
        )
        known = set(manifest["created"].values())
        adopted: dict[str, str] = {}
        for local_id, record in list(uncertain.items()):
            matches = [
                i["key"] for i in found
                if i["key"] not in known
                and i.get("fields", {}).get("summary") == record["summary"]
                and i.get("fields", {}).get("issuetype", {}).get("name") == record["issue_type"]
            ]
            if len(matches) > 1:
                raise PublishError(
                    f"{local_id} '{record['summary']}' matches several issues in Jira "
                    f"({', '.join(sorted(matches))}). Delete the duplicates, then run again."
                )
            if matches:
                manifest["created"][local_id] = matches[0]
                known.add(matches[0])
                adopted[local_id] = matches[0]
                self._emit("jira.issue_reconciled", {"local_id": local_id, "key": matches[0]})
            else:
                self._emit("jira.uncertain_not_created", {"local_id": local_id})
            del uncertain[local_id]
            self._save_manifest(manifest)
        return adopted

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
        adf.paragraph(adf.text("Estimate: ", bold=True), f"{story.story_points} story points",
                      adf.text("  ·  Priority: ", bold=True), story.priority.capitalize(),
                      adf.text("  ·  Kind: ", bold=True), story.kind),
        adf.heading("Acceptance criteria"), adf.bullet_list(criteria),
    ]
    if deps:
        blocks += [adf.heading("Depends on"), adf.bullet_list(deps)]
    if mode == "label":
        blocks += [adf.paragraph(adf.text("Feature: ", bold=True), feature.title)]
    return adf.doc(*blocks, *_footer(run_id))
