# ADR-0009: Integrate with existing ITSM and enterprise security scanners rather than replace them

- **Status:** Proposed (subject to assumptions A3 and A5)
- **Date:** 2026-11-17

## Context

The customer already has a change-management system of record (assumed ServiceNow) and enterprise
SAST/SCA/secrets tooling that InfoSec owns and auditors rely on. Parallel tooling would create
conflicting findings and duplicate change records.

## Decision

- **ITSM:** the engine's signed approval is the *decision record*; ServiceNow is the *ITSM record*.
  - R0/R1 create pre-approved standard changes.
  - R2/R3 create normal changes with the engine's approvals and evidence digest attached.
  - CHG numbers are written back into the evidence bundle.
- **Scanners:** Vigilance gates call the enterprise scanners through adapters and normalise their output to SARIF. Open-source scanners are used only as fallbacks where coverage is missing, with InfoSec approval.

## Consequences

- **Positive:** One finding taxonomy; existing InfoSec SLAs and exception processes apply; auditors see familiar records.
- **Negative:** Gate latency depends on enterprise-scanner throughput. Adapters must handle vendor API changes. Scanner outages block promotion because the gates fail closed.
