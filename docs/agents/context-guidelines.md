# Context guidelines for the intake agent

How to give the idea-to-Jira agent the context it needs to write a backlog your teams would
accept, and how to keep that context safe, cheap and maintainable.

The model can only be as specific as what it's told. Given a one-line idea, it fills gaps
with sensible but generic defaults. In the first real run, "password reset via SMS" became an
employee reset with a generic security posture, and missed the five controls an identity
reviewer would demand. Good context closes those gaps before the backlog is written, instead
of in review.

---

## 1. The four layers of context

| Layer | What it is | Who owns it | Changes | Where it goes |
|---|---|---|---|---|
| **System prompt** | How to write any backlog: hierarchy, story format, quality bar, honesty rules | Platform / AI engineering | Rarely, versioned (`--prompt-version`), eval-gated | `src/vigilant_engine/intake/prompts.py` |
| **Standards files** | Domain rules that apply to many ideas (identity, payments, claims, accessibility) | Domain owners: security, privacy, compliance | Like policy: reviewed pull requests | `standards/*.md`, passed with `--context-file` |
| **Idea context** | Facts about *this* idea: users, systems, constraints, metrics | The requester (product owner) | Per idea | A file passed with `--context-file` |
| **Clarifying answers** | Answers to Claude's questions at run time | The requester | Per run | `--interactive` |

Two things are **not** context:

- **Quality rules that must always hold** belong in the quality gate (`intake/quality.py`),
  not in a prompt. A prompt *guides* Claude; the gate *decides*. Write a rule in both places
  when it can be checked mechanically.
- **Secrets, credentials and customer data** never go into any layer (see §6).

`--context-file` can be repeated. Put standards first, then the idea's own context:

```bash
uv run vigilant intake plan --idea-file idea.md \
    --context-file standards/security-identity.md \
    --context-file my-idea-context.md --interactive
```

---

## 2. What good idea context contains

Use `examples/context/TEMPLATE.md`. In order of how much it changes the backlog:

1. **Who it is for.** Name the users and the ones it is *not* for. "Employees resetting
   their corporate directory password" and "policyholders resetting their portal password"
   are different products.
2. **Problem and evidence.** What happens today, how often, what it costs. A number
   ("~1,200 password tickets a month") lets Claude write measurable success metrics.
3. **Scope.** In scope, out of scope, and what is explicitly later.
4. **Constraints.** Systems it must integrate with (by name), regulations, data
   classification, deadlines, budget, channels that are not allowed.
5. **Decisions already made.** Vendor chosen, architecture fixed, policy agreed. Without
   these, Claude raises open questions and writes spikes for things that are settled.
6. **Success measures and baseline.** The target and today's value.
7. **Glossary.** Terms your organisation uses differently ("member", "producer",
   "first notice of loss").

Keep it to facts. Leave out solution designs unless they are real constraints: an early
design narrows the backlog to one approach before discovery.

---

## 3. Writing standards files

Use `standards/TEMPLATE.md`. These practices come from how current Claude models respond to
instructions.

| Do | Because |
|---|---|
| **State the behaviour and the reason.** "Notify the owner on first enrolment, because enrolment is the easiest takeover point." | Current models generalise from reasons. A bare rule is followed literally; a reasoned one is applied to cases you didn't list. |
| **Make every rule observable in the output.** | If you can't tell from the backlog whether a rule was followed, it can't be reviewed or evaluated. Rewrite it until you can. |
| **Write principles with one example**, not lists of cases. | "Responses must be identical at every step, for example resend and outage screens" covers screens you forgot. |
| **Use plain, calm language.** No ALL CAPS, "CRITICAL" or "NEVER EVER". | Emphatic wording makes newer models over-apply a rule and become rigid elsewhere. |
| **Prefer "do X" over "don't do Y"**, and say what to do instead when you prohibit. | It tells the model what good looks like. |
| **Keep it short:** one page, roughly 300–800 words. | Every rule competes for attention. Ten precise rules beat forty vague ones. |
| **Open with scope:** when to use the file and when not to. | Teams pick the right file, and the model knows the domain it is in. |
| **Put an owner, a version and a date at the top.** | Standards are policy. Someone must keep them current. |

Write rules about **the backlog**, not about the tool. "Every credential change notifies the
owner" is a rule about the product. "Ignore the quality gate for this idea" is an attempt to
change the tool's behaviour: Claude treats it as data, and the gate still applies.

---

## 4. Anti-patterns

| Anti-pattern | What happens | Instead |
|---|---|---|
| Pasting a 40-page policy PDF | Rules get diluted, cost rises, contradictions creep in | Extract the 5–10 rules that change backlogs; link the full policy |
| Vague adjectives ("secure", "fast", "user-friendly") | Claude writes vague acceptance criteria to match | Numbers and observable outcomes ("locked for 60 minutes after 5 failures") |
| Contradictory files ("always email" vs "SMS only") | Unpredictable output, wasted repairs | One owner per domain; review files together |
| Stale facts (old vendor, retired system) | Confident backlog built on wrong assumptions | Date each file; review quarterly |
| Ticket or email text pasted as context | It may contain instructions aimed at the model | Summarise it in your own words, or rely on the tags (the agent treats it as data) |
| Customer data, real names, account numbers | Sent to the model provider and kept in the run folder | Synthetic examples only |
| Solution designs posing as context | Backlog locked to one approach before discovery | State constraints, not designs |

---

## 5. How the agent treats context (trust model)

- Idea, context and answers are sent in the **user turn**, wrapped in `<idea>`, `<context>`
  and `<answers>` tags. The system prompt tells Claude to treat them as material to analyse,
  not as instructions that change its rules. **Requester material is never placed in the
  system prompt**, so it never gains operator authority.
- Standards written as requirements ("must notify the owner") are followed as requirements
  *from the requester*. That is the intent: they shape the backlog, not the agent's rules.
- Nothing in context can bypass the safeguards that matter. The **quality gate** still
  blocks bad backlogs, **human approval** is still required, and the agent never holds Jira
  credentials. A malicious or careless context file produces, at worst, a backlog a reviewer
  rejects.

---

## 6. Security and privacy

Context leaves your machine. It is sent to the model provider (Anthropic, or Amazon Bedrock
in your cloud account) and saved in the run folder. `brief.json` holds the full text; the
event log holds a digest.

- No secrets, tokens, passwords or connection strings, ever.
- No personal data or customer records. Use synthetic examples.
- Classify standards files like any internal policy document. For restricted projects, use
  the data-classification routing in the solution architecture (for example, Bedrock only).
- Treat run folders as records: they contain the idea, context and model output.

---

## 7. Size, cost and caching

- Each run sends the context on every call: clarifying questions, the draft, and up to two
  repairs. The agent sends the idea and context as a **separate cached block**, so repeats
  within a run are cache reads, billed at about a tenth of normal input.
- Caching only works on **identical bytes**. Don't put timestamps or run-specific values in
  standards files. The cache lasts about five minutes between calls, which comfortably covers
  one run.
- A one-million-token context window is a ceiling, not a target. Aim for standards under
  roughly 2,000 tokens (about 1,500 words) and idea context under 1,000. More context means
  more cost and more chances for rules to conflict, without better backlogs.

---

## 8. Lifecycle and governance

1. **Store** standards in `standards/` in the repository, one file per domain.
2. **Review** changes by pull request, approved by the domain owner (for example, security
   for `security-identity.md`). For regulated programmes, the AI governance group owns the
   list of standards, as in the solution architecture's RACI.
3. **Evaluate before adopting** (§9). A standards change is a prompt change.
4. **Trace:** each run records the prompt version and a digest of the context it used
   (`run.started` in `events.jsonl`), so any backlog can be traced to the exact rules
   behind it.
5. **Retire** rules that the system prompt or the quality gate now cover, so they aren't
   stated twice.

---

## 9. Testing a context change

Every rule you add should have an **expectation that would catch its absence**:

1. Add or extend a case in `evals/intake/cases.json`. Give it an idea and, for each new rule,
   an expectation pattern that matches when the backlog follows the rule.
2. Run before and after. Planning runs cost money and ask first; scoring and comparing
   are free:
   ```bash
   uv run python evals/intake/run_eval.py run --label before --only <case>
   uv run python evals/intake/run_eval.py run --label after  --only <case> \
       --context-file standards/<file>.md
   uv run python evals/intake/run_eval.py compare evals/results/before evals/results/after
   ```
3. Adopt the change if the target expectations are met and nothing else got worse. Read the
   backlogs too: keyword checks approximate a reviewer, they don't replace one.
4. Model output varies. For an important change, run each case two or three times.

---

## 10. Worked example: password reset

**Before** (idea only): "Automated Self-Service Password Reset via SMS". The result was a
strong but generic backlog, which missed session revocation, first-enrolment notification,
a cooling-off period, a breached-password check and a final-step guard.

**After** (idea + standards + idea context):

```markdown
<!-- standards/security-identity.md, then: -->
# Idea context
Users: about 8,000 employees in the US; contractors excluded. Privileged accounts excluded.
Problem: about 1,200 password-reset tickets a month (ITSM, last 3 months), about 9 minutes each.
Systems: Microsoft Entra ID (cloud-only), Twilio for SMS (contract signed), ServiceNow.
Constraints: no personal email as a factor (policy); SOX-scoped apps behind SSO; WCAG 2.1 AA.
Decided: SMS + authenticator app; no security questions.
Success: tickets down 50% in 3 months; median reset under 3 minutes.
Out of scope: account unlock; customer-facing reset.
```

Expected effect:
- The five controls appear in the backlog.
- The vendor spike disappears, because Twilio is decided.
- The second factor changes from personal email to an authenticator app.
- Success metrics use the real baseline.
- Contractors and customer resets are listed as out of scope.

Check it with the evaluation harness before rolling it out to other teams.
