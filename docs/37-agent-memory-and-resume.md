# Memory and approved resume requests

The MCP workbench adds profile-scoped memory, dashboard launch, and a human-approved resume render. There is no submit or browser-fill tool.

## Scopes

| Scope | Grants |
|---|---|
| `memory:read_write` | remember, recall, forget profile notes |
| `proposals:create` | create/read resume requests |
| `profile:resume_context` | read profile context needed for a request |

```bash
jobforge integrations grant --connection conn_... \
  --scopes memory:read_write,proposals:create
```

## Memory

`jobforge_remember(key, value)` stores one bounded, profile-scoped note (key ≤80, value ≤2000 chars). Contact details, answers, API keys, tokens, and private keys are rejected. `recall` searches; `forget` deletes.

Memory is administrative context, not a verified resume fact. There is no automatic capture, and it is not permission to change jobs, profiles, applications, or the answer bank.

## Dashboard

`jobforge_dashboard` returns the local URL and `/operations` path. `jobforge_open_dashboard` starts and opens the console. The MCP server never approves its own request.

## Resume request

`jobforge_request_resume(job_id, idempotency_key)` creates a `pending_approval` operation. It does not call a provider, Typst, or a browser.

1. Request is bound to the connection/profile and job.
2. Human clicks **Approve and render** in `/operations`.
3. Worker claims once under the domain-action lock.
4. Worker rechecks connection enabled state, profile binding, and scopes.
5. Worker renders with `tailor_job`, using the operation’s recorded profile.
6. Operation becomes `succeeded` with an application ID, ATS summary, warnings, and `/artifacts/{id}/resume`.

```text
pending_approval -> approved -> running -> succeeded | failed
       |               |
       +-> denied      +-> expired (before claim)
```

Repeating the same idempotency key and payload returns the existing operation; a different payload returns `IDEMPOTENCY_CONFLICT`. Retrying a failed request needs a new key and a new human approval.

## Safety

- No submit, browser-fill, approval, risk-enable, or arbitrary file tool.
- Requests are proposals until a human approves them.
- Worker rechecks connection, profile, and scopes at execution time.
- Artifacts are profile-scoped under `output/profile-<id>/...`.
- Provider/Typst/browser work happens only in the approved worker.
- Dashboard and operation pages use the existing local auth middleware when a token is configured.

Tests: `tests/test_mcp.py` and `tests/test_operations.py`.
