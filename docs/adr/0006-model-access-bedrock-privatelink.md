# ADR-0006: Model access through Amazon Bedrock via PrivateLink (customer deployment)

- **Status:** Proposed (subject to discovery assumption A1, AWS as primary cloud)
- **Date:** 2026-11-17
- **Scope:** The Hartford engagement

## Context

The customer is a regulated insurer with NYDFS Part 500 third-party and data-protection
obligations. InfoSec and third-party risk management (TPRM) will judge the model access path on
three things: the data boundary, private connectivity, and whether the vendor is already approved.
Three options exist: Amazon Bedrock (AWS-operated), Claude Platform on AWS (Anthropic-operated,
AWS IAM/billing, full API parity), and the first-party Claude API.

## Decision

Use **Amazon Bedrock through a VPC interface endpoint** for all agent and Claude Code traffic,
behind the engine's model gateway.

- Service control policies block Bedrock calls outside the private endpoint and to unapproved models.
- Approved models: Claude Opus 5.5, Sonnet 5.5, Haiku 5.5. Covered Models (Fable/Mythos family) are excluded because they require 30-day retention.
- The gateway owns routing, DLP, budgets, refusal fallback (client-side middleware), and the replay tape.

## Consequences

- **Positive:** Data stays in the customer's cloud-provider environment; private networking; fits
  existing AWS approvals; IAM-native access control and cost allocation.
- **Negative:** Feature subset compared with the Anthropic API. No Message Batches (evals run on
  standard calls, which cost more), no server-side `fallbacks`, no task budgets, no Managed Agents,
  and new features can lag. All are mitigated in the engine (own budgets, client middleware,
  self-hosted runtime).

## Alternatives considered

- *Claude Platform on AWS:* full parity (Batches, `inference_geo`, fallbacks) with AWS billing. A
  strong candidate for a second phase, for example eval workloads. Moving to it is a gateway
  configuration change plus a TPRM review.
- *First-party API with ZDR:* full features, but adds a new vendor data path and public endpoint
  for InfoSec to approve.
