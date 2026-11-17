# ADR-0004: Policy-as-code with OPA/Rego

- **Status:** Proposed
- **Date:** 2026-11-17

## Context

Promotion rules (which risk tier needs which approvers, which paths are protected, which findings
block) differ by organisation, repo, and regulation, and change over time. Risk and security teams
must be able to own and review them without changing engine code.

## Decision

- Express decision rules in **Rego**, evaluated by **OPA**, behind a `PolicyEngine` interface.
- Input is a single canonical document: `{contract, changeset, gate_results, risk, actor}`.
  Output is a `Decision` (`allow` | `require_approval{quorum, roles}` | `request_changes` | `deny`).
- Default rule is `deny` (fail closed).
- Policy bundles are versioned and signed; the engine loads them from outside the agent worktree and
  records the bundle digest in every decision.
- Every rule has `opa test` unit tests, required in CI.

## Consequences

- **Positive:** Separation of policy from mechanism; reviewable by non-engineers; reusable with
  existing OPA estates; decisions replayable from recorded inputs plus bundle digest.
- **Negative:** Rego learning curve; an extra binary or embedded evaluator to ship.

## Alternatives considered

- *Python rules in the engine* — simpler, but couples policy changes to releases and gives agents a
  larger attack surface.
- *Cedar* — strong authorization model; less adoption for CI/CD-style policies. Interface keeps it
  swappable.
