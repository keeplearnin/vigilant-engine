# ADR-0010: Pricing, rating, underwriting, and claims-decisioning code excluded from agent autonomy

- **Status:** Proposed (needs AI governance committee approval)
- **Date:** 2026-11-17

## Context

Code that implements insurance decisions (rating algorithms and tables, underwriting rules,
claims triage, fraud scoring, reserving models) falls under AI-governance and unfair-discrimination
expectations. Examples include the NAIC AI Model Bulletin, NYDFS Circular Letter No. 7 (2024), and
Colorado SB21-169. Many changes in this area also require regulatory filings and actuarial review.
Code-level gates cannot establish actuarial soundness or fairness.

## Decision

- Paths and modules tagged `rating`, `pricing`, `underwriting-rules`, `claims-decisioning`, `fraud-scoring`, or `reserving-models` are **R4 (denied)** for agents.
- A **protected-attribute gate** blocks any agent-authored reference to protected-class attributes or listed proxies in decisioning-tagged modules, and notifies AI governance.
- Agents **may** still produce read-only artefacts for these systems (documentation, characterisation tests in a separate branch for human adoption) when AI governance approves.
- Revisit after Phase 3, together with model risk management, actuarial, and compliance.

## Consequences

- **Positive:** Keeps the program clear of the highest-scrutiny regulatory area while trust is built; easy to explain to examiners.
- **Negative:** Excludes some high-value work at first; depends on accurate tagging of decisioning code during repo onboarding (validated by app owners).
