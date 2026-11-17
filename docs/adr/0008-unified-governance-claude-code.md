# ADR-0008: One governance plane for interactive Claude Code and autonomous agents

- **Status:** Proposed
- **Date:** 2026-11-17

## Context

Developers will adopt Claude Code interactively well before autonomous agents reach scale. Two
separate governance stacks would produce inconsistent controls and two evidence trails for auditors.

## Decision

- Claude Code is configured through **managed settings**, deployed by MDM or server-managed settings:
  - Bedrock routed through the engine's model gateway, with pinned models.
  - Locks: `allowManagedPermissionRulesOnly`, `allowManagedHooksOnly`, and `disableBypassPermissionsMode: "disable"`.
  - Deny rules for guardrail paths.
  - Hooks that report session metadata to the engine.
  - OTel export to the SIEM.
- AI-assisted commits carry a commit trailer. **GitHub rulesets** require the `vigilant/decision`
  status check on those PRs, so they go through the same Vigilance gates and risk routing as
  autonomous changes.
- Client-side settings are treated as usability guardrails, not as audit-relied controls. The
  documentation states that permission rules are not a security boundary.

## Consequences

- **Positive:** One policy, one evidence model, and one audit population covering every AI-authored change; controls cannot be defeated by local configuration edits.
- **Negative:** Requires MDM coverage of developer machines (or server-managed settings) and ruleset changes on onboarded repos. Commit labelling depends on trailers, so repo onboarding adds a PR check that flags AI-assisted commits missing the trailer, based on Claude Code OTel `commit.count` correlation.
