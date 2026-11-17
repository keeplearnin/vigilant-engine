# ADR-0002: LangGraph as the durable orchestration runtime

- **Status:** Proposed
- **Date:** 2026-11-17

## Context

The delivery lifecycle is a long-running, branching process with loops (repair), human pauses that
can last days (approvals), and a need to resume after crashes and to replay runs for audit.

## Decision

Implement the lifecycle as a LangGraph `StateGraph`:

- Typed state (Pydantic models) passed between explicit nodes; conditional edges encode routing.
- A durable checkpointer (SQLite locally, Postgres in team/enterprise mode) persists state after
  every node.
- Human gates use `interrupt()`; runs resume via a signed approval command.
- Every node transition emits an event to the evidence log (ADR-0005).
- Agent logic lives behind interfaces so that the graph is not coupled to any model provider.

## Consequences

- **Positive:** Resumability, time-travel debugging, native HITL, explicit and reviewable control
  flow, strong Python ecosystem fit.
- **Negative:** Dependency on a fast-moving library — mitigated by pinning versions and keeping
  domain logic in plain Python modules that the graph calls, so the runtime is replaceable.

## Alternatives considered

- *Temporal* — excellent durability; heavier to run locally, less native for LLM state. Possible
  future backend for enterprise mode.
- *Hand-rolled state machine* — full control but re-implements checkpoints, interrupts, and tracing.
- *Free-form multi-agent chat frameworks* — rejected: implicit control flow is hard to audit.
