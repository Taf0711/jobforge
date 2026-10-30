# Risk consent

Every risky mode is off by default. Enabling one requires the exact phrase `I UNDERSTAND THE RISK`. The UI and CLI show the consequences before the control appears.

## Available modes

| Mode | What it does |
|---|---|
| `unattended_submit` | One application per invocation; clicks Submit without a human review |
| `autopilot_submit` | Sequential queue pass; prepares and submits ready applications |

Both share a hard **10-per-UTC-day** cap. A slot is reserved atomically before the browser opens. A failed or captcha-blocked attempt still counts.

Unavailable and rejected: bulk unattended, captcha solver, Indeed/LinkedIn automation, Workday account creation.

## Exact disclosures

`unattended_submit`:

> UNATTENDED SUBMIT IS OFF BY DEFAULT. Enabling it lets JobForge click Submit without a human reviewing the form. Applications you have not checked can go out, employer/ATS terms may be violated, and your accounts may be restricted. Captcha is never solved; a captcha stops the application and hands it back to you. Hard limit: 10 per calendar day.

`autopilot_submit`:

> AUTOPILOT SUBMIT IS OFF BY DEFAULT. Enabling it lets the scheduled queue prepare and submit ready applications without a human review of each form. Unchecked applications can go out, employer/ATS terms may be violated, and your accounts may be restricted. Autopilot is sequential, uses an ATS allowlist and the quality gate, and stops at captcha, 2FA, or an account wall. Hard limit: 10 total unattended + autopilot submissions per calendar day.

## Limits

- One submission at a time; no parallel workers.
- Autopilot uses the ATS allowlist and quality gate.
- Captcha, 2FA, account wall, or unsupported page stops the run.
- No employer account creation, password storage, or credential reuse.
- Every submission is recorded with the answers sent.

## Consent and audit

- `data/risk.json` stores policy, consent time, and phrase; mode `0600`.
- A hand-edited `true` without the exact phrase is ignored.
- `data/risk_usage.json` stores the daily count and resets on the next UTC date.
- Enable/disable writes `risk_consent` events.
- Submitted applications write `unattended_submit` / `autopilot_submit` events.
- The static audit flags `allow_submit=True` literals; the reviewed call site carries a same-line marker.

## Queue and autopilot

```bash
jobforge queue apply --auto
jobforge autopilot run --limit 5
```

Without `--auto`, the queue is forced into review-only mode and asks whether you submitted. With `--auto`, the risk policy is checked before the browser and the daily slot is reserved first.

When a submission is reported or confirmed, the queue updates both the queue item and the application row (`status=submitted`, `submitted_at`). The queue item is bound to its recorded profile.

`autopilot run --limit N` bounds attempted jobs, not the sum of the `prepared` and `submitted` counters.
