# Email verification-code bridge

`jobforge.email.code_bridge` waits for an application verification code to arrive by IMAP, returns it to the caller, and records a redacted audit event. It is a **library only**. The browser flow remains parent-owned and supervised.

## Why it exists

Some ATS forms send a one-time code to the applicant's email and require it before the application can proceed. The bridge closes that loop deterministically without automating submission or touching captcha or account creation.

## Public interface

```python
from jobforge.email.code_bridge import (
    CodeRequest, CodeResult, match_message, redact_code,
    record_bridge_event, wait_for_code,
)
```

### `CodeRequest`

| Field | Type | Default | Meaning |
|---|---|---|---|
| `company` | `str` | `""` | Company name to look for in sender, subject, or body |
| `sender_hints` | `list[str]` | `[]` | Sender substrings (domains, addresses, display names) |
| `subject_hints` | `list[str]` | `[]` | Subject substrings |
| `since_iso` | `str` | `""` | UTC ISO timestamp; older messages are ignored |
| `timeout_s` | `int` | `120` | Total wait budget |
| `poll_interval_s` | `float` | `3.0` | Delay between inbox polls |
| `application_id` | `int \| None` | `None` | Application to attach the audit event to |

Hints are matched case-insensitively against `sender + subject + body`. At least one hint or the company must match; a code by itself never matches. A message also must have `category == "verification"`, a non-empty `verification_code`, and `received_at >= since_iso`.

### `CodeResult`

`matched: bool`, `code: str`, `message_id: str`, `sender: str`, `subject: str`, `received_at: str`, `elapsed_s: float`, `reason: str`. On timeout, `matched` is `False` and `reason` is `"timeout"`.

### `wait_for_code(conn, config, request, *, fetcher=None, sleeper=time.sleep, clock=time.monotonic)`

Poll until a match or timeout. The default fetcher calls `jobforge.email.imap.fetch_recent(config.email)`. The `conn` argument is reserved for callers that audit in the same transaction scope; the function itself does not write events. Use `record_bridge_event` explicitly.

`fetcher`, `sleeper`, and `clock` are injection seams for offline deterministic tests. Message ids are deduplicated across polls. IMAP/fetcher errors propagate so the caller can surface an auth or network problem instead of silently timing out.

### `record_bridge_event(conn, application_id, result)`

Writes one `events` row of kind `"verification_code"` through `jobforge.db.log_event`. The detail contains match metadata, the elapsed time, and `redact_code(result.code)` (bullets plus the last two characters). Separator-formatted codes (`123 456`, `123-456`, `123.456`, `123_456`) are redacted from the subject and message id too, so the full code is never written to events.

### `redact_code(code)`

Returns bullets plus the final two characters. Codes of two characters or fewer are fully bulleted, so the output never reveals the whole secret.

## Integration contract for `jobforge/automation/base.py`

The parent owns `base.py`; this lane does not edit it.

1. Detect a verification-code field on the current page **and** confirm the user opted in before calling the bridge. Do not poll the inbox speculatively.
2. Build a `CodeRequest`:
   - `company` = company from the job/application row.
   - `sender_hints` = ATS domain plus any configured `email.verification_senders`.
   - `subject_hints` = `["verification code", "security code", "verify your email"]` or ATS-specific variants.
   - `since_iso` = UTC ISO timestamp captured just before the browser action that triggered the email.
   - `application_id` = current application id.
3. Call `wait_for_code(conn, config, request)`. On `matched`, fill the code field and call `record_bridge_event(conn, request.application_id, result)`.
4. On timeout or failure, record the event too, leave the field to the human, and keep the browser open. Never guess a code.
5. **Never auto-submit.** The bridge does not call `adapter.submit` or click Submit. The human reviews the page and clicks Submit under the existing `browser.allow_submit = false` gate. A code is not consent to submit.
6. If IMAP is disabled or `email.username`/password are unset, skip the bridge and leave the field unresolved for the human.

## CLI

`python -m jobforge code --company Acme --timeout 120` polls the inbox and
prints a JSON result with the **redacted** code. Add `--reveal-code` only when
you need the full code at the terminal; the full code is still never written to
events. At least one of `--company`, `--sender`, or `--subject` is required.
When `--application-id` is supplied, a `verification_code` event is recorded for
both matches and timeouts. Sender, subject, message id, and separator-formatted
codes (`123 456`, `123-456`) are redacted in the default payload.

## Secrets policy

- Verification codes are credentials. Do not log, print, include in screenshots, or persist the full code in events. The one explicit exception is `jobforge code --reveal-code`, which writes the code only to the terminal on request.
- Audit details and UI messages must use `redact_code`.
- The browser fill report must not echo the code. If a screenshot would show it, redact the field or skip the screenshot for that step.
- Do not send codes to an LLM or any external service.

## Test command

```bash
cd $JOBFORGE_REPO
.venv/bin/python -m pytest tests/test_code_bridge.py tests/test_cli_code.py -q
```

Result: `10` bridge tests plus the CLI tests. Tests use fake fetchers/sleepers/clocks; no network or real IMAP.

## Open questions

- Whether the parent wants `since_iso` anchored before navigation (safest against a code sent during load) or before the "Send code" click.
- Whether the dashboard should show the redacted event automatically; the event is written but no UI is added in this lane.
- Whether `email.verification_senders` should be the sole sender allowlist in production or merely hints; the current implementation treats them as hints.
