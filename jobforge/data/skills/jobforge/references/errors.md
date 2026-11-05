# Error handling

| Code | Action |
|---|---|
| `SETUP_REQUIRED` | Stop; the human must finish JobForge setup. Do not create a profile or database. |
| `MIGRATION_REQUIRED` | Stop; the human must run the current JobForge setup/migration. |
| `INCOMPATIBLE_INTERFACE` | Stop; the skill and server interface do not match. |
| `INVALID_ARGUMENT` | Correct the input. Do not bypass validation. |
| `NOT_FOUND` | Do not infer existence in another profile. |
| `CONNECTION_DISABLED` | Stop; the human must enable the connection. |
| `SCOPE_DENIED` | Report the missing scope; do not use another data source. |
| `PROFILE_QUEUE_CONFLICT` | Stop; the shared queue has another profile's items. Do not clear or reinterpret them. |
| `QUALITY_BLOCKED` | Report the blockers; do not approve or open a browser. |
| `RISK_NOT_AUTHORIZED` | Report that the human must enable the explicit risk consent; never enable it yourself. |
| `DAILY_CAP_REACHED` | Stop for the day or apply manually; do not bypass the cap. |
| `CAPTCHA_REQUIRED` | Hand off to the human; there is no solver. |
| `SUBMISSION_NOT_CONFIRMED` | Do not claim submission; report the safe state. |
| `OPERATION_EXPIRED` | Create a new request only if the user asks; approval is one-use. |
| `STALE_CONTEXT` | Fetch the first page again with the current revision. |
| `CONTEXT_TOO_LARGE` | Narrow the query or request a smaller page. |
| `BUSY` | Another JobForge action is active; wait and retry the read. |
| `INTERNAL_ERROR` | Report the request ID and stop. Do not paste a traceback. |

A schema-level MCP tool error means the arguments did not match the published
tool schema. Correct the arguments or ask the user; do not switch to shell, SQL,
or a different transport to evade the schema.
