# ADR-0005: Hash-chained event log and signed attestations as system of record

- **Status:** Proposed
- **Date:** 2026-11-17

## Context

Auditors and incident responders need to know, long after the fact, exactly what an agent did, what
evidence the decision was based on, and who approved it — and to trust that the record was not edited.

## Decision

- Every state transition, model call, tool call, gate result, decision, and approval is written to an
  **append-only event log**. Each event stores `prev_hash` and `hash = H(prev_hash || canonical(event))`.
- Large payloads (diffs, reports, model I/O) are stored content-addressed; events hold their digests.
- Each promoted change gets an **evidence bundle** and an **in-toto/SLSA-style attestation**, signed
  by the Promotion Service (Sigstore keyless or enterprise KMS).
- Chain heads can optionally be anchored to an external transparency log or WORM storage.
- `vigilant verify <run_id>` validates the chain; `vigilant replay <run_id>` re-derives the decision.

## Consequences

- **Positive:** Tamper evidence; complete audit trail; enables deterministic replay and control
  testing.
- **Negative:** Storage growth (mitigated by content-addressing and retention policies); model I/O
  may contain sensitive data, so the log inherits the repo's data classification and access controls.

## Alternatives considered

- *Plain application logs* — mutable, unstructured, not audit-grade.
- *Full blockchain/ledger DB* — unnecessary operational weight; hash chain + external anchoring gives
  the needed tamper evidence.
