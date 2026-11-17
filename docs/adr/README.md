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

### Customer engagement ADRs — [The Hartford](../engagements/the-hartford/)

| ADR | Title | Status |
|---|---|---|
| [0006](0006-model-access-bedrock-privatelink.md) | Model access through Amazon Bedrock via PrivateLink | Proposed |
| [0007](0007-self-hosted-agent-runtime.md) | Self-hosted agent runtime (Agent SDK + Messages API), not Managed Agents | Proposed |
| [0008](0008-unified-governance-claude-code.md) | One governance plane for Claude Code and autonomous agents | Proposed |
| [0009](0009-servicenow-and-enterprise-scanner-integration.md) | Integrate with existing ITSM and enterprise scanners | Proposed |
| [0010](0010-pricing-underwriting-code-excluded.md) | Pricing, underwriting, and decisioning code excluded from agent autonomy | Proposed |
