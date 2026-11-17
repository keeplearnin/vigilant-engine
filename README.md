# vigilant-engine

> **Governed multi-agent control plane driving autonomous software delivery, continuous verification, and lifecycle guardrails.**

`vigilant-engine` is a local-first, deterministic execution framework for governing autonomous developer agents across the enterprise Software Development Lifecycle (SDLC). Built on **Python 3.12** and **LangGraph**, it enforces a strict dual-plane architecture: active execution agents run alongside an uncompromised **Vigilance Layer** that performs static policy checks, AST analysis, security scanning, and human-in-the-loop (HITL) approval gating before any code modification hits production branches.

---

## Key Features

* **Dual-Plane Separation:** Decouples creative agent execution (planning, writing code, generating tests) from deterministic governance enforcement.
* **Deterministic Guardrails:** Inline evaluation loops using static analysis, security scanners, and pytest verification suites.
* **Stateful Orchestration:** Explicit, auditable state machine powered by LangGraph with full event-tracing capabilities.
* **MCP & Local Tooling Integration:** Native support for Model Context Protocol (MCP) servers, AST parsers, Git workflows, and CLI environments (`uv`).
* **CI/CD & HITL Approval Gates:** Configurable policy thresholds that automatically trigger manual human reviews for high-risk operations.

---

## Architectural Control Plane Overview

`vigilant-engine` operates on a dual-plane architecture designed to prevent unmonitored agent drift or unverified execution loops.
