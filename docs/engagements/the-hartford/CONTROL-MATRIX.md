# Control Matrix: Regulatory and Framework Mapping

> This maps public regulatory themes to engine controls **for design discussion**. It is not legal
> advice and does not determine whether anything applies. Customer Legal, Compliance, and internal
> audit own the final applicability decisions and control descriptions. Section references are to
> the public texts and must be confirmed against current versions.

**Legend.** *Engine control* is what `vigilant-engine` enforces or produces. *Evidence* is what an
examiner or auditor receives. *Customer owner* is the function accountable for the control
(assumed titles).

## 1. SOX 404: IT general controls (financially significant applications)

| ITGC area | Engine control | Evidence | Customer owner |
|---|---|---|---|
| Change authorisation | `ChangeContract` confirmed by the requester; R2/R3 require role-based approval; R4 denied | Contract + approvals in the evidence bundle; ServiceNow CHG link | App owner / change management |
| Change testing | G1–G3 gates at the exact SHA in a clean sandbox; coverage and test-integrity rules | SARIF reports, test results, coverage delta | App owner |
| Segregation of duties | Requester ≠ approver ≠ promoter enforced in code; platform admins excluded from approver groups | Identities in the bundle; period-end IdP group export | ITGC owner / IAM |
| Migration to production | Engine only opens or merges PRs. Deployment stays in the existing pipeline under existing controls | PR and pipeline linkage | Release management |
| Logical access to the control itself | Policy repo with two-person review; signed policy bundles; no engine bypass | Policy-repo PR history; bundle signatures | InfoSec / AI governance |
| Completeness of population | Population export reconciled to Git merge history | Signed population report + reconciliation | Internal audit (tests) |
| Control operating effectiveness | Scheduled gate canaries | Monthly canary report (target 100% blocked) | Platform owner |

## 2. NYDFS 23 NYCRR Part 500 (cybersecurity regulation)

| Section (theme) | Engine control | Evidence |
|---|---|---|
| 500.2 / 500.3 Cybersecurity program and policies | The engine is a documented component of the SDLC and AI-use policy; policies as code | Policy bundle, architecture docs |
| 500.6 Audit trail | Hash-chained, append-only event log; WORM evidence store | Chain-verification report; S3 Object Lock configuration |
| 500.7 Access privileges and access management | Least-privilege workload identities; agents hold no credentials; short-lived run-scoped tokens; PAM for admins | IAM policies, token TTL configuration, access reviews |
| 500.8 Application security | Secure-development gates (SAST, SCA, secrets, AST rules) on all AI-authored code | Gate reports per change |
| 500.11 Third-party service provider security | Anthropic and AWS assessed under TPRM; data boundary via Bedrock PrivateLink; DLP at the gateway | TPRM file, data-flow diagram, DLP events |
| 500.14 Monitoring and training | SIEM integration; developer training on Claude Code use under managed settings | SIEM rules; training records |
| 500.15 Encryption of nonpublic information | KMS CMKs for the evidence store and DB; TLS everywhere; no NPI in sandboxes | KMS configuration; repo certification results |
| 500.16 Incident response | Runbooks for sandbox-escape suspicion, DLP spikes, evidence-integrity alarms | Runbooks; incident tickets |

## 3. NAIC Insurance Data Security Model Law (#668, as adopted by states)

| Theme | Engine control | Evidence |
|---|---|---|
| Risk assessment | Threat model for AI-authored change; repo onboarding risk certification | Threat model doc; onboarding records |
| Information security program safeguards | Sandbox isolation, DLP, encryption, access control | As above |
| Oversight of third-party service providers | Model access through the customer's cloud provider; contractual data terms; TPRM | TPRM file |
| Monitoring and incident handling | SIEM detections and runbooks | SIEM, tickets |

## 4. AI governance for insurers

| Source | Theme | Engine control | Evidence |
|---|---|---|---|
| NAIC Model Bulletin: Use of AI Systems by Insurers (2023; adopted by many states) | Written AI Systems (AIS) program, governance, risk management, internal controls, third-party AI oversight, documentation | Engine registered in the AI inventory; risk-tiered human oversight; pinned models with governed upgrades; eval and red-team records | Model inventory entry; governance minutes approving the risk-tier policy; eval reports |
| NYDFS Circular Letter No. 7 (2024): AI and external consumer data in underwriting and pricing | Governance, fairness testing, and documentation for AI used in underwriting/pricing | Decisioning code **excluded** from agent autonomy (R4); protected-attribute gate | Policy rule, gate logs, AI governance notifications |
| Colorado SB21-169 and implementing regulations | Governance and testing to prevent unfair discrimination from external data and algorithms | Same exclusion and gate; applies to agent-authored code touching decisioning logic | Same |
| NIST AI RMF 1.0 | Govern, Map, Measure, Manage | Govern: policy-as-code and committee approvals. Map: use-case register. Measure: evals, canaries, escape rate. Manage: risk tiers, incident runbooks | Program artefacts |
| ISO/IEC 42001 (if the customer adopts it) | AI management system | Same artefacts organised to the AIMS clauses | Program artefacts |

## 5. Privacy and data protection

| Source | Theme | Engine control |
|---|---|---|
| GLBA privacy/safeguards (as implemented for insurers through state law) | Protection of consumers' nonpublic personal information | Repo certification (no NPI in fixtures), DLP at the gateway, Restricted-class routing |
| State privacy laws (e.g., CCPA/CPRA, where applicable) | Data minimisation, purpose limitation | Model I/O logging by data class; retention schedule |
| HIPAA (where group-benefits systems are in scope as a covered entity or business associate) | PHI safeguards | Restricted-class rules; Phase 3 only, after a PIA; no Managed Agents (not HIPAA-eligible) |

## 6. Payments and secure SDLC

| Source | Theme | Engine control |
|---|---|---|
| PCI DSS v4.0, Requirement 6 (secure systems and software) | Secure development, code review, vulnerability management for in-scope software | PCI-scoped paths are R3 with InfoSec approval; SAST/SCA gates; reviewer independence |
| NIST SSDF (SP 800-218) | Protect software, produce well-secured software, respond to vulnerabilities | Gates, provenance, UC1 remediation flow |
| SLSA / in-toto | Build and source provenance | Signed attestations per promoted change |

## 7. Control testing calendar (proposed)

| Frequency | Activity | Owner |
|---|---|---|
| Continuous | Gate canaries on every engine release and policy change | Platform |
| Monthly | Canary effectiveness report; DLP and denied-call review | Platform + InfoSec |
| Quarterly | Access review of approver and admin groups; population sample walkthrough | IAM + internal audit |
| Per model/prompt change | Eval, canary, and red-team suites; AI governance approval | Platform + AI governance |
| Annually | Pen test and red-team of the engine; policy recertification by AI governance | InfoSec + AI governance |
