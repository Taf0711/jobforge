# Model providers

A provider is optional. It is needed only for the built-in agent and JobForge-internal LLM features (LLM resume tailoring, cover letters, answer drafts). The skill/MCP path uses the host agent’s model.

## Setup

Dashboard: **Models** (`/providers`) → choose provider → paste key or sign in → refresh models → use model. **Test chat** is a billable smoke test.

```bash
jobforge providers list
jobforge providers connect openai
jobforge providers models openai --refresh
jobforge providers use openai gpt-4.1-mini
jobforge providers disconnect openai
```

`connect` prompts with `getpass`; keys are never positional arguments.

## Auth

| Provider | Supported |
|---|---|
| OpenAI / ChatGPT | API key; official Sign in with ChatGPT not wired |
| Anthropic / Claude | API key; enterprise Workload Identity Federation |
| Google Gemini | API key; OAuth/ADC via a Google Cloud project |
| OpenRouter | OAuth PKCE or API key |
| Claude Code CLI | experimental text-only CLI bridge |
| Groq, Mistral, xAI, DeepSeek, Together, Fireworks, Baseten | API key |
| Ollama, LM Studio | local endpoint, no key |
| Custom | OpenAI-compatible base URL, optional key |

Anthropic consumer OAuth is prohibited for third-party apps. JobForge never reads Claude Code credential files or Keychain entries. OpenRouter OAuth is the only implemented browser sign-in.

## Storage

Priority: OS keychain → atomic `data/connections/providers/<provider>.key` (`0600`) → environment variable.

The database stores only provider id, auth mode, storage backend, base URL, cached models, and last check. The secret is never in SQLite, `config.yml`, exports, templates, logs, or errors.

Environment variables: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `GROQ_API_KEY`, `MISTRAL_API_KEY`, `XAI_API_KEY`, `DEEPSEEK_API_KEY`, `TOGETHER_API_KEY`, `FIREWORKS_API_KEY`, `BASETEN_API_KEY`, or `JOBFORGE_LLM_API_KEY`. `JOBFORGE_LLM_PROVIDER`, `JOBFORGE_LLM_MODEL`, and `JOBFORGE_LLM_BASE_URL` override the active selection.

## Endpoints

OpenAI-compatible: `POST <base>/chat/completions`. Anthropic: `POST /v1/messages`. Gemini uses its OpenAI-compatible endpoint. Model discovery uses each provider’s `/models` endpoint; failures fall back to a curated list.

OpenRouter OAuth: random verifier + S256 challenge → `openrouter.ai/auth` → local callback → verify state → exchange code at `/api/v1/auth/keys` → store key. Codes are single-use and expire.

The Claude Code bridge runs the official `claude` CLI with tools disabled. It is text-only, experimental, and subject to Anthropic’s terms.

## Config

```yaml
llm:
  enabled: true
  provider: openai
  model: gpt-4.1-mini
  base_url: https://api.openai.com/v1
  timeout: 120
  temperature: 0.2
  max_tokens: 4096
agent:
  max_steps: 8
  max_seconds: 300
  max_tool_calls: 6
  allow_writes: false
```

`llm.enabled` does not make background calls.

## Failures

| Symptom | Action |
|---|---|
| No provider configured | Connect one at `/providers` |
| Claude Code not signed in | `claude auth login` |
| 401 / invalid key | Reconnect or rotate |
| 403 | Check model/account permissions |
| 404 model | Refresh and select a listed model |
| 429 | Wait or switch provider/model |
| Context length | New thread or narrower question |
| Tools not supported | Pick a tool-capable model |
| OAuth state mismatch | Restart the flow |
| Callback unreachable | Use the API-key flow |

## Privacy

Enabling a provider sends candidate context and your messages to that provider. Email and phone are omitted by default; skills, projects, experience, preferences, application history, and network names are included. Use Ollama/LM Studio for local inference. Job descriptions are untrusted input.
