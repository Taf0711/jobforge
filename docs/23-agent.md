# Built-in agent

Status: optional. The skill/MCP path is the primary integration; this built-in agent is for users who want a chat surface inside JobForge.

## What it adds

- A tool-using model loop for search, scoring, fit explanations, tailoring, and readiness.
- A provider registry with API keys, local endpoints, OpenRouter OAuth, and an experimental Claude Code CLI bridge.
- Full candidate context with evidence, projects, history, network, and market data.
- Deterministic profile advice.

It cannot submit. There is no submit tool.

## Non-goals

No autonomous or bulk submission, captcha solving, Indeed/LinkedIn automation, employer accounts, password storage, invented resume facts, or unofficial consumer-provider sign-in.

## Runtime loop

```text
message + allow_writes
  → compact candidate context
  → provider chat with allowlisted tool schemas
  → tool calls (read always; write only when allowed for this message)
  → bounded results appended
  → final answer + tool trace + usage persisted
```

`allow_writes` is per message and rechecked on every iteration. It is never stored on a thread. A write proposed from history in a later run is refused unless that run explicitly enables writes.

## Write gating

Writable tools: `tailor_resume`, `scan_jobs`, `score_jobs`, `build_application_queue`, `open_application_review`.

`open_application_review` runs the quality gate, then opens the supervised fill browser in review mode. It never clicks Submit. There is no `apply_job` or `submit_application` tool.

## Providers

See `24-providers.md` for setup and storage.

| Provider | API key | Browser sign-in |
|---|---|---|
| OpenAI / ChatGPT | yes | official SIWC not wired |
| Anthropic / Claude | yes | no; consumer OAuth prohibited for third parties |
| Google Gemini | yes | OAuth/ADC not wired |
| OpenRouter | yes | OAuth PKCE |
| Claude Code CLI | no | experimental text-only CLI bridge |
| Groq, Mistral, xAI, DeepSeek, Together, Fireworks, Baseten | yes | no |
| Ollama, LM Studio | no | local endpoint |
| Custom | optional | OpenAI-compatible base URL |

Keys use the OS keychain, an atomic `0600` file, or environment variables. They never enter `config.yml`, SQLite, exports, templates, logs, or errors. Provider redirects are disabled.

## Candidate context

The context pack includes identity (email/phone omitted), work authorization, preferences, skills by evidence weight, experience bullets, projects, fact IDs, evidence, application history, network strength, and top jobs. It is capped at 60,000 characters; the agent can request deeper sections with `get_profile_context`.

Job descriptions and returned prose are untrusted data.

## Tools

Read: `get_profile_context`, `search_jobs`, `get_job`, `explain_fit`, `get_application_readiness`, `suggest_profile_improvements`, `get_warm_path`, `list_applications`, `get_job_market_overview`.

Write: `tailor_resume`, `scan_jobs`, `score_jobs`, `build_application_queue`, `open_application_review`.

No shell, SQL, arbitrary URL, or submit tool. `scan_jobs` uses only configured sources; pasted URLs remain a separate explicit action.

## Privacy

Email and phone are omitted from the default context. Enabling a provider sends the rest of the candidate context to that provider; use Ollama/LM Studio for local inference. Application-note contact details are redacted.

## Limitations

- Chat is non-streaming and cannot cancel an in-flight provider request.
- Tool-calling quality varies by model.
- The grounding checks are conservative, not semantic proof.
- Windows native execution is not an independently tested gate.

Tests: `tests/test_agent.py`, `tests/test_providers.py`, `tests/test_llm_resume.py`.
