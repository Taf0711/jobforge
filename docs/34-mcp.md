# MCP server

JobForge exposes its local workbench over MCP stdio. There is no listening socket or OAuth server. Read tools are default; scope-gated write tools add scan, agent-normalized import, score, queue, memory, resume requests, and pending submission requests. The host agent supplies the model; JobForge needs no provider for read or deterministic write tools.

Status: protocol-tested preview.

## Register

```bash
jobforge integrations setup --profile 4 --client claude-code \
  --scopes profile:resume_context,applications:summary,memory:read_write,proposals:create,scan:run,jobs:write,email:write,matches:write,queue:write,submission:request
jobforge integrations config --connection conn_...
```

The output is a portable `mcpServers` entry:

```json
{"mcpServers":{"jobforge":{"command":"/path/to/python","args":["-m","jobforge","--config","/path/to/config.yml","mcp","serve","--connection","conn_...","--instance","inst_..."]}}}
```

`npx jobforge` writes this entry, exports the skill, and verifies it for supported hosts. See `38-skill-install.md`.

## Tools

Read: `jobforge_status`, `search_jobs`, `get_job`, `explain_fit`, `get_readiness`, `get_profile`, `list_applications`.

Write, scope-gated: `scan` (`scan:run`; configured sources or explicit known-ATS board selectors such as `lever:palantir`), `import_job` (`jobs:write`; one posting the host agent already fetched from any public site; JobForge never fetches the URL), `ingest_emails` (`email:write`; employer messages the host already read through its own Gmail/Outlook/other connector; JobForge never fetches the mailbox), `score` (`matches:write`), `build_queue` (`queue:write`), `remember`/`recall`/`forget` (`memory:read_write`), `request_resume` (`proposals:create`, `profile:resume_context`), `request_submission` (`submission:request`).

`get_operation`/`list_operations` are ownership-checked and need no extra scope. `dashboard`/`open_dashboard` are local UI actions.

The human paste-a-link path is separate: `jobforge import-url <url>` or the Jobs-page URL form fetches one public posting and scores it. That path is not exposed as an MCP tool; MCP imports accept agent-normalized fields.

`request_submission` creates a pending one-use operation. It does not open a browser. A human must approve the exact operation in `/operations`; the worker rechecks the connection, profile, scope, quality gate, risk consent, and 10/day cap before calling the existing apply path.

Every tool has a bounded schema, title, and JobForge envelope. Read tools advertise `readOnlyHint`; `forget` and `request_submission` are destructive hints. The host should treat `structuredContent` as the source of truth, not generated prose.

## Test

```bash
.venv/bin/python -m pytest tests/test_mcp.py -q
```

Covers a real stdio subprocess, current/legacy initialization, schemas, annotations, status, search, memory, scan/import/score/queue writes, resume and submission requests, idempotency, scope denial, stdout cleanliness, and disabled connections.

## Failures

| Condition | Result |
|---|---|
| Missing DB | `SETUP_REQUIRED` |
| Old schema | `MIGRATION_REQUIRED` |
| Unknown connection | `NOT_FOUND` |
| Disabled connection/feature | `CONNECTION_DISABLED` |
| Missing scope | `SCOPE_DENIED` |
| Invalid input | schema error or `INVALID_ARGUMENT` |
| Internal error | `INTERNAL_ERROR` with request ID |

Read tools open read-only SQLite; action tools open a writable connection. Every tool resolves the connection's bound profile and never uses the active profile, a provider, Typst, or a browser (submission runs in the approved worker).

## Resume requests

`jobforge_request_resume` creates a pending request. A human approves it in `/operations`; a worker renders and `jobforge_get_operation` returns the artifact URL. The optional `destination_dir` is an absolute writable directory, normally the agent session's current working directory; the worker keeps the canonical artifact and also exports a role-named PDF copy there. Without it, the MCP server uses its working directory when that looks like a session workspace rather than the JobForge app home or checkout. `result.export_path` reports the copy. See `37-agent-memory-and-resume.md`.

`jobforge_request_submission` creates a pending one-use operation. It never opens a browser. The approval page shows the job, profile, resume, quality blockers, risk-consent state, and daily cap. The worker rechecks all of them and calls the existing apply path with `allow_submit=True` only after approval. A captcha/2FA/account-wall or unconfirmed form records a failed operation; there is no automatic retry.

## Skill

```bash
jobforge skills export --destination ~/.claude/skills
```

The skill contains no candidate data or host paths. See `35-jobforge-skill.md`.

## Not included

Host certification, host-generated resume proposals, attachments/review receipts, approval-gated review-only browser operations, and MCP-triggered autopilot runs.
