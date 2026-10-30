# MCP server

JobForge exposes its local workbench over MCP stdio. There is no listening socket, OAuth server, or submit tool. The host agent supplies the model; JobForge needs no provider for read tools.

Status: protocol-tested preview.

## Register

```bash
jobforge integrations setup --profile 4 --client claude-code \
  --scopes profile:resume_context,applications:summary,memory:read_write,proposals:create
jobforge integrations config --connection conn_...
```

The output is a portable `mcpServers` entry:

```json
{"mcpServers":{"jobforge":{"command":"/path/to/python","args":["-m","jobforge","--config","/path/to/config.yml","mcp","serve","--connection","conn_...","--instance","inst_..."]}}}
```

`npx jobforge` writes this entry, exports the skill, and verifies it for supported hosts. See `38-skill-install.md`.

## Tools

Read: `jobforge_status`, `search_jobs`, `get_job`, `explain_fit`, `get_readiness`, `get_profile`, `list_applications`.

Optional: `remember`/`recall`/`forget` (`memory:read_write`), `dashboard`/`open_dashboard`, `request_resume` (`proposals:create`, `profile:resume_context`), `get_operation`/`list_operations`.

Every tool has a bounded schema, title, and JobForge envelope. Read tools advertise `readOnlyHint`; `forget` is destructive. The host should treat `structuredContent` as the source of truth, not generated prose.

## Test

```bash
.venv/bin/python -m pytest tests/test_mcp.py -q
```

Covers a real stdio subprocess, current/legacy initialization, schemas, annotations, status, search, memory, resume requests, idempotency, scope denial, stdout cleanliness, and disabled connections.

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

The server opens read-only SQLite per call and never uses the active profile, a provider, Typst, or a browser.

## Resume requests

`jobforge_request_resume` creates a pending request. A human approves it in `/operations`; a worker renders and `jobforge_get_operation` returns the artifact URL. See `37-agent-memory-and-resume.md`.

## Skill

```bash
jobforge skills export --destination ~/.claude/skills
```

The skill contains no candidate data or host paths. See `35-jobforge-skill.md`.

## Not included

Host certification, host-generated resume proposals, attachments/review receipts, and scan/score/queue/browser/submit actions.
