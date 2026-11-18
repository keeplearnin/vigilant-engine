# Standards: identity, accounts and credentials

Pass this file to the intake agent for any idea that touches sign-in, passwords, recovery,
MFA, accounts or permissions:

```bash
uv run vigilant intake plan --idea "..." --context-file standards/security-identity.md --interactive
```

The agent treats this file as context from the requester. The deterministic quality gate
still decides what can be published.

## Rules for flows that change credentials, recovery methods or permissions

- Notify the account owner on an already-trusted channel (for example the corporate mailbox)
  whenever a recovery method or credential is added, changed or removed, including the
  first enrolment. Enrolment is the easiest point for an attacker with stolen credentials
  to take over an account.
- Apply a cooling-off period (for example 24 to 72 hours) before a newly added or changed
  recovery method can be used to recover the account, and say what happens during it.
- After a password reset, revoke the account's existing sessions and refresh tokens, so a
  reset after compromise actually locks the attacker out.
- Each step of a multi-step verification must refuse requests that skipped an earlier step.
  Write an acceptance criterion for the final step (for example, setting the new password),
  not only for the middle ones.
- Responses for existing and non-existing (or ineligible) accounts must be identical at
  every step: start, verification, resend, lockout, errors and outages. Timing differences
  count too.
- Check new passwords against a list of known breached passwords, in addition to the
  directory's complexity policy.
- Privileged and administrator accounts are excluded from self-service recovery unless a
  stronger method is specified.
- Never store or log one-time codes, passwords or full contact details. Mask them on screen.
- Rate-limit code requests per account and per source address, and cap message spend.
