# Agent workbench (MCP preview)

Profile-bound MCP tools for local agents. Read tools are the default. Optional scopes add memory, dashboard launch, scan/import/score/queue writes, and approval-gated resume or submission requests. The submission tool only creates a pending operation; a human must approve it in the dashboard. There is no unapproved submit path.

Status: protocol-tested preview. Host certification is not complete.

## Setup

```bash
jobforge integrations setup --profile 4 --client generic \
  --scopes profile:resume_context,applications:summary,memory:read_write,proposals:create,scan:run,jobs:write,email:write,matches:write,queue:write,submission:request
jobforge integrations list
```

`integrations setup` creates an instance if needed and returns a `conn_...` ID. Disable with `jobforge integrations disable --connection conn_...`. Disabling fails closed and does not delete profile or application data.

`npx jobforge` automates this for supported hosts. See `38-skill-install.md`.

## Tools

| Tool | Scope | Data |
|---|---|---|
| `jobforge_status` | none | Versions, connection, profile, readiness |
| `jobforge_search_jobs` | `jobs:read` | Bounded job summaries |
| `jobforge_get_job` | `jobs:read` | One job; untrusted truncated description |
| `jobforge_explain_fit` | `jobs:read`, `matches:read` | Fit, gates, reasons, gaps, requirements |
| `jobforge_get_readiness` | `jobs:read`, `matches:read` | Quality blockers and next human action |
| `jobforge_get_profile` | `profile:resume_context` | Bounded profile sections, contact-redacted |
| `jobforge_list_applications` | `applications:summary` | Status/timestamps; no answers or paths |
| `jobforge_remember/recall/forget` | `memory:read_write` | Profile-scoped notes |
| `jobforge_dashboard/open_dashboard` | none | Local console URL |
| `jobforge_scan` | `scan:run` | Scan configured sources or explicit known-ATS boards (for example `lever:palantir`); no arbitrary URLs |
| `jobforge_import_job` | `jobs:write` | Store and quick-score one posting the host already fetched from any public site; JobForge never fetches the URL |
| `jobforge_ingest_emails` | `email:write` | Classify/match employer messages the host already read through its own mail connector; forward-only status changes, no mailbox fetch |
| `jobforge_score` | `matches:write` | Deep-score unscored/all/explicit/top jobs |
| `jobforge_build_queue` | `queue:write` | Build a bounded supervised queue |
| `jobforge_request_resume` | `proposals:create`, `profile:resume_context` | Pending human-approved render |
| `jobforge_request_submission` | `submission:request` | Pending one-use submission approval |
| `jobforge_get/list_operations` | none (ownership-checked) | Safe operation state and artifact URL |

## MCP and CLI

```bash
jobforge mcp serve --connection conn_... --instance inst_...
jobforge integrations config --connection conn_...
printf '{"limit":5}' | jobforge tools call jobforge_search_jobs --connection conn_...
```

The MCP transport is stdio-only; stdout is protocol-only. The server rechecks connection, scopes, and feature flags on every call. `tools call` uses a read-only SQLite connection for read tools and a writable connection for action tools.

## Safety

- Strict inputs; unknown properties rejected.
- Pagination with revision-bound cursors; `STALE_CONTEXT` on change.
- Descriptions capped at 12,000 chars and marked untrusted.
- URLs limited to credential-free HTTP(S).
- Profile contact data, answers, notes, paths, and screenshots are redacted.
- Output is capped at 64 KiB; `CONTEXT_TOO_LARGE` instead of truncation.
- Read tools never call a provider, Typst, or a browser.
- The operation worker rechecks connection, profile binding, scopes, risk consent, quality gate, and daily cap before rendering or submitting.

Schema v6 stores instances, connections, memories, operations, and operation events. Connection IDs are selectors, not secrets.

## Not implemented

Host certification, host-generated resume proposals, attachments/review receipts, approval-gated review-only browser operations, and MCP-triggered autopilot runs.
