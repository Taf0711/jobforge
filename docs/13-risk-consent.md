# Risk consent

JobForge is local-first and human-supervised by default. Every risky mode is **off by default**. A mode can only be enabled by an explicit, recorded acknowledgement, and the UI/CLI show the consequences before the control is offered.

## What is available

### `unattended_submit` — off by default

Enabling it lets JobForge click Submit without a human reviewing the form.

Exact warning shown in the UI and CLI:

> UNATTENDED SUBMIT IS OFF BY DEFAULT.
> Enabling it lets JobForge click Submit without a human reviewing the form.
> Consequences you accept: applications you have not checked can go out,
> employer/ATS terms may be violated, and your accounts may be restricted.
> Captcha is never solved; a captcha stops the application and hands it back to you.
> Hard limit: 10 unattended submissions per calendar day.

Hard limits:

- One application per explicit invocation. There is no bulk unattended mode.
- Daily cap: 10 submissions per UTC calendar day.
- A captcha stops the application; no solver is called.
- No employer account creation, password storage, Indeed, or LinkedIn automation.
- The attempt reserves a daily slot atomically (`fcntl.flock` on
  `data/risk_usage.lock`) before the browser opens. A failed or captcha-blocked
  attempt still counts; under-counting would be unsafe.
- The submission is recorded in `events` as `unattended_submit` and increments
  `data/risk_usage.json`.

Enable:

```bash
python -m jobforge risk enable \
  --flag unattended_submit \
  --acknowledge "I UNDERSTAND THE RISK"
```

Status and disable:

```bash
python -m jobforge risk status
python -m jobforge risk disable --flag unattended_submit
```

Web UI: `/settings/risk`.

## What is deliberately unavailable

These flags appear in the disclosure list and cannot be enabled:

| Flag | Why it is unavailable |
|---|---|
| `bulk_unattended` | Mass submission is not a supported mode; one application per invocation. |
| `captcha_solver` | No captcha solving, farming, or bypass. Solve it yourself. |
| `indeed_linkedin_automation` | Both platforms treat automation as a terms violation; use them manually. |
| `workday_account_creation` | No employer account creation or password storage; sign in yourself. |

The UI/CLI show a warning for each and reject any attempt to enable them.

## Consent and audit

- `data/risk.json` stores the policy, consent timestamp, and consent phrase. File mode is `0600`.
- A hand-edited `"unattended_submit": true` without the exact consent phrase is ignored on load.
- `data/risk_usage.json` stores today's count. It resets on the next UTC date.
- Enable and disable actions append `risk_consent` rows to `events` (application
  id is null; the detail names the flag and, for enable, the cap).
- `events` records `unattended_submit` with a non-secret detail string for every
  submitted application.
- The daily slot is reserved before the browser opens; `can_submit_unattended`
  is the read-only pre-check and does not consume a slot.
- `jobforge audit` and the static security scanner flag `allow_submit=True`
  literals, including dict-item assignments such as `kwargs["allow_submit"] = True`.
  The one reviewed risk-gated call site carries
  `# security-audit: allow auto-submit enabled`. The marker must name the exact
  finding and must follow the match on the same line; it suppresses nothing
  else. Removing it makes the audit fail, so a silent regression is visible.

## Queue integration

```bash
python -m jobforge queue build --limit 10
python -m jobforge queue prepare
python -m jobforge queue apply            # human review + confirmation
python -m jobforge queue apply --hold 600 --confirm
python -m jobforge queue apply --auto     # only when risk is enabled; capped
```

Without `--auto`, the queue opens the application in review mode and asks whether you submitted when `--hold` is `0` or `--confirm` is passed. With `--auto`, the risk policy is checked before the browser opens; the daily slot is reserved atomically; if the policy is off or the cap is reached, the command refuses. A quality blocker (`QualityBlocked`) stops the browser before the risk check.

## Why this shape

The user asked for control over the risk, not for a black box. The default is safe; the opt-in is explicit, bounded, auditable, and still stops at captchas and account walls. The unavailable modes are not hidden — the product tells you they are manual-only instead of pretending to implement them.
