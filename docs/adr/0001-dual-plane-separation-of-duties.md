# ADR-0001: Dual-plane architecture with separation of duties

- **Status:** Proposed
- **Date:** 2026-11-17

## Context

Autonomous coding agents are non-deterministic and can be manipulated (prompt injection, poisoned
context). Regulated change-management controls require that the party making a change is not the
party approving and releasing it. If agents both write and promote code, an agent failure or
compromise becomes a production change.

## Decision

Split the system into an **Execution plane** (agents: creative, untrusted, sandboxed) and a
**Vigilance plane** (deterministic gates, risk engine, policy: authoritative), coordinated by a
**Control plane** and recorded by an **Evidence plane**.

- Agents hold no credentials to protected refs. They write only to an ephemeral git worktree.
- A dedicated **Promotion Service**, with its own identity and signing key, is the only component
  able to push or open PRs, and only for a passing `Decision` bound to an exact commit SHA.
- Vigilance code never imports agent code and shares no credentials or sandboxes with it.
- Guardrail files (policies, gate config, CI config, CODEOWNERS, the vigilance package) are
  prohibited paths for agents.

## Consequences

- **Positive:** An agent compromise cannot ship code on its own; clean audit story (maker ≠ checker);
  agents and models can be swapped without re-validating the governance layer.
- **Negative:** More components and identities to operate; promotion adds latency; a second sandbox
  is needed for clean verification.

## Alternatives considered

- *Agents with scoped push rights + CI checks only* — rejected: CI can be edited by the same actor and
  provides no risk-proportional human gating.
- *Single agent with self-review* — rejected: no independence of verification.
