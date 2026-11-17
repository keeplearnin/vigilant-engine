# ADR-0003: Deterministic gates are authoritative; LLM review is advisory

- **Status:** Proposed
- **Date:** 2026-11-17

## Context

LLM-as-judge reviewers catch issues static tools miss (logic, intent mismatch), but they are
non-deterministic, can be prompt-injected by the very diff they review, and cannot be explained to
an auditor as a control.

## Decision

- Only deterministic gates (scope, lint, types, SAST, secrets, supply chain, AST rules, tests) and
  the policy engine can **block or allow** a change.
- LLM reviewers (the Critic) produce advisory findings that can only **raise** the risk tier,
  never lower it, and never remove a deterministic finding.
- Gates fail closed: error, timeout, or missing output = block.
- Gates run in a clean environment at an exact commit SHA with pinned tool and rule-pack versions,
  recorded in the evidence bundle.

## Consequences

- **Positive:** Reproducible and explainable decisions; injection into the critic cannot cause an
  approval; LLM review still adds value by routing more changes to humans.
- **Negative:** Some semantic defects only reach humans via risk uplift, not via automatic block;
  fail-closed behavior may cause friction during tool outages.

## Alternatives considered

- *LLM judge as a blocking/approving gate* — rejected for authority; kept as a signal.
- *Majority vote of multiple LLM judges* — still non-deterministic; may be added as a stronger
  advisory signal later.
