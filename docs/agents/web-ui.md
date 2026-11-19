# Local web UI

A browser front end for the intake agent, for people who'd rather not use a terminal. It
runs on your own computer and uses the same engine as the CLI: the same planner, quality
gate, Jira publisher and audit log.

## Start it

```bash
cd vigilant-engine
git pull && uv sync

# In the same Terminal window, set your keys (hidden input, kept out of shell history)
read -s ANTHROPIC_API_KEY && export ANTHROPIC_API_KEY
read -s JIRA_API_TOKEN && export JIRA_API_TOKEN
export JIRA_BASE_URL=https://your-site.atlassian.net
export JIRA_EMAIL=you@example.com

uv run vigilant ui
```

Your browser opens at `http://127.0.0.1:8765/`. Press Ctrl+C in the Terminal to stop.
The header shows whether Claude and Jira are configured. Options: `--port 8800`,
`--runs-dir somewhere/else`, `--no-browser`.

## Using it

1. **New idea.** Describe the idea, tick the standards to apply, and fill in whatever
   context you know (users, problem, scope, systems, constraints, decisions, success
   measures). Leave "Ask me clarifying questions first" on.
2. **Answer questions.** Up to five, each with a suggested answer. Leave a box empty to
   accept the suggestion.
3. **Wait.** A progress panel shows the current step and elapsed time; planning usually
   takes 3–8 minutes. You can leave the page and come back from **Runs**.
4. **Review.** The backlog appears as an epic › feature › story tree.
   - Quality-gate errors and notes are listed at the top and flagged on each story.
   - Open a story and choose **Edit story** to change its title, user story, points,
     priority or acceptance criteria (one per line: `Given … | When … | Then …`). The gate
     runs again when you save.
   - For bigger changes, use **Edit the whole backlog as JSON**.
5. **Preview in Jira.** Enter the project key (and the story issue type if your project
   has no Story type, for example `Subtask`). This reads Jira and creates nothing.
6. **Approve.** Enter your name, type the project key, and choose **Approve and create**.
   Exactly the previewed plan is created. If the backlog changed after the preview, approval
   is refused until you preview again.
7. **Audit.** The run page shows who approved it, how, and when, plus the approval record
   hash that is also written into each Jira issue. **Check audit log** verifies the run's
   log against it.

## Safety

- Listens only on `127.0.0.1`, so other computers can't reach it.
- Refuses requests that don't come from its own address, which defends against
  DNS-rebinding attacks.
- Every form carries a secret token, so other websites can't submit forms to it (CSRF).
- Pages refuse to be embedded in other sites (clickjacking).
- Model output is escaped before it is shown.
- Publishing stays locked while quality-gate errors remain, and only the approved plan runs.
- Standards files are read-only in the UI. Change them through a reviewed pull request.
- Approver identity is self-asserted on a local machine. The approval method is recorded
  with it, and a hosted deployment would take identity from single sign-on.

## UI and CLI side by side

| Task | UI | CLI |
|---|---|---|
| Plan an idea | New idea form | `vigilant intake plan` |
| Clarifying questions | Form with suggested answers | `--interactive` |
| Edit the backlog | Edit story, or the JSON editor | Edit `backlog.json` |
| Preview and publish | Preview → Approve | `publish`, then `publish --apply` |
| Audit check | Check audit log | `vigilant intake verify` |
| Prompt evaluation | Not in the UI | `evals/intake/run_eval.py` |

Runs created in either appear in both: they share the same `runs/` folder.
