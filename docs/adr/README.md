# Architecture Decision Records

Each ADR records one significant decision, its context, and its consequences. ADRs are immutable once
accepted; a change of direction is a new ADR that supersedes the old one.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-dual-plane-separation-of-duties.md) | Dual-plane architecture with separation of duties | Proposed |
| [0002](0002-langgraph-durable-orchestration.md) | LangGraph as the durable orchestration runtime | Proposed |
| [0003](0003-deterministic-gates-are-authoritative.md) | Deterministic gates are authoritative; LLM review is advisory | Proposed |
| [0004](0004-policy-as-code-opa.md) | Policy-as-code with OPA/Rego | Proposed |
| [0005](0005-hash-chained-evidence-log.md) | Hash-chained event log and signed attestations as system of record | Proposed |

Template: Context → Decision → Consequences (positive / negative) → Alternatives considered.
