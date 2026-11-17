# Reference configs

Starting-point configurations for discovery workshops. **Placeholders** (`<…>`, `example.internal`,
regions, budgets) must be replaced with values the customer validates.

| File | Consumer | Owner after hand-over | Notes |
|---|---|---|---|
| [`risk-map.yaml`](risk-map.yaml) | Risk Engine / OPA bundle | AI governance committee | Engine-specific schema (designed here). Paths and tags confirmed per repo at onboarding |
| [`model-routing.yaml`](model-routing.yaml) | Model gateway | Platform + AI governance | Engine-specific schema. Copy Bedrock model / inference-profile IDs from the Bedrock console |
| [`claude-code-managed-settings.json`](claude-code-managed-settings.json) | Claude Code on developer machines | Endpoint engineering + InfoSec | Deploy via MDM to `/Library/Application Support/ClaudeCode/managed-settings.json` (macOS), `/etc/claude-code/managed-settings.json` (Linux/WSL), `C:\Program Files\ClaudeCode\managed-settings.json` (Windows), or use server-managed settings from the admin console |

## Verify before rollout

These settings keys come from the current Claude Code documentation (code.claude.com:
managed settings, permissions, hooks, Amazon Bedrock, monitoring). Settings change often. Before
deploying, check the exact keys against the docs **for the version you deploy**, then confirm what
a machine actually loaded with `claude doctor` or `/status`. Items to double-check in particular:

- where `allowManagedHooksOnly` goes (top level vs nested) and the `sandbox.*` key names;
- the Bedrock model / inference-profile ID format for `ANTHROPIC_DEFAULT_*_MODEL`;
- whether the model gateway is fronted through `ANTHROPIC_BEDROCK_BASE_URL` or the Bedrock Mantle endpoint variables.

## Important limitation

Claude Code permission rules match the *form* of a command (for example, `Bash(curl *)` does not
match `/usr/bin/curl` or `bash -c "curl …"`), so the documentation says they are **not a security
boundary**. In this design they are usability guardrails. The authoritative controls run on the
server: Vigilance gates, the `vigilant/decision` status check, and GitHub rulesets.
