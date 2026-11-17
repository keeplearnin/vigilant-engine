# Engagement: The Hartford (design exercise)

The forward-deployed engineering (FDE) package for deploying `vigilant-engine` at a large,
multi-line US insurer. It is built from public information only. Every customer-environment
detail is an assumption to validate in discovery.

| Document | Purpose | Primary audience |
|---|---|---|
| [SOLUTION-ARCHITECTURE.md](SOLUTION-ARCHITECTURE.md) | Deployment architecture, identity, data protection, Claude integration, gate packs, risk routing, audit design | Platform, InfoSec, architecture review board |
| [CONTROL-MATRIX.md](CONTROL-MATRIX.md) | SOX ITGC, NYDFS Part 500, NAIC, AI-governance, privacy, and PCI mapping to engine controls and evidence | Compliance, internal audit, AI governance |
| [ROLLOUT-PLAN.md](ROLLOUT-PLAN.md) | Phased delivery, exit gates, metrics, autonomy ratchet | Steering committee |
| [reference-configs/](reference-configs/) | Risk map, model routing, Claude Code managed settings | Platform, endpoint engineering, AI governance |
| ADRs [0006](../../adr/0006-model-access-bedrock-privatelink.md)–[0010](../../adr/0010-pricing-underwriting-code-excluded.md) | Customer-specific decisions | All |

Builds on the product [reference architecture](../../architecture/ARCHITECTURE.md).
