# ADR-0007: Self-hosted agent runtime (Claude Agent SDK + Messages API), not Managed Agents

- **Status:** Proposed
- **Date:** 2026-11-17

## Context

Agents must read and write code, run builds and tests, and do so inside the customer's network
boundary, with every tool call bound to the governance plane. Anthropic offers a hosted agent loop
and sandbox (Managed Agents) and a library harness (the Claude Agent SDK) that runs on the customer's
own infrastructure.

## Decision

- **Coding agents** (Implementer, Test Author, Repair) run the **Claude Agent SDK** (`claude-agent-sdk`) inside the customer's microVM sandboxes:
  - `cwd` is the run's worktree, and `setting_sources=[]` so settings files committed in the repo are ignored.
  - `PreToolUse` / `PostToolUse` hooks (`HookMatcher`) call the engine's tool gateway and emit evidence.
  - `disallowed_tools` carries scoped deny rules.
- **Reasoning-only agents** (Planner, Critic, quarantine) call the **Messages API** with structured outputs.
- Managed Agents is not used for customer workloads.

## Consequences

- **Positive:** Code, execution, and tool traffic never leave the customer's boundary; works on Bedrock; every tool call is under the customer's policy and logging; no dependency on a beta hosted surface.
- **Negative:** The customer operates the sandbox fleet and the harness upgrades; the SDK version is pinned and upgraded through the governed path.

## Alternatives considered

- *Managed Agents:* least code to own. However, it is not available on Bedrock, not eligible for ZDR or HIPAA, and runs the loop and sandbox outside the customer boundary.
- *Hand-written Messages API tool loop for coding agents:* full control, but re-implements file tools, context management, and subagents that the SDK already provides.
