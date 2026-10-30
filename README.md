# JobForge

A local-first job-search workbench for your coding agent. JobForge finds public-ATS jobs, scores fit, tailors a verified one-page resume, and opens a review-only application form. The agent never submits; you review and click Submit.

The optional dashboard is a local review console, not a separate job-search app.

## What it does

- **Discover** jobs from Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Workday, USAJobs, NYC Open Data, and public GitHub job lists.
- **Score** fit as skill coverage × hard-gate survival, with reasons, gaps, and a requirement table.
- **Tailor** a one-page Typst resume from verified facts. No invented employers, dates, metrics, skills, or degrees.
- **Apply** in a persistent Chromium window. It fills the form, screenshots it, and stops for human review.
- **Track** applications, answers, statuses, events, and optional IMAP replies.
- **Connect** to Claude Code, Codex, Pi, Hermes, OMP, or any MCP host through the packaged Agent Skill.

## What it does not do

- No captcha solving, Indeed/LinkedIn automation, employer accounts, or password storage.
- Unattended submission is off by default. Two risk-consented modes exist (`unattended_submit`, `autopilot_submit`), share a 10/day cap, and stop on captcha/2FA/account wall.
- No invented resume content.
- No cloud account, telemetry, or Node application runtime. The npx package is only an installer.

## Install into your agent

Requires Python 3.11+, Typst 0.14+, and `uv` if JobForge is not installed yet.

```bash
npx jobforge
```

From a checkout before the npm release:

```bash
npx --yes --package ./installer jobforge
```

The wizard:

1. detects your agents;
2. shows the exact MCP config, skill directory, connection scopes, and backup paths;
3. exports the Agent Skill and registers the MCP server;
4. asks separately before installing Typst or optional Chromium.

Supported hosts: Claude Code, Codex, Pi, Hermes, OMP, and generic MCP. No JobForge model provider is required; the host agent supplies the model.

First run can import a resume. Target roles and sources are edited in the local console.

### Standalone app

```bash
uv tool install --from git+https://github.com/Taf0711/jobforge jobforge
jobforge setup
jobforge
```

Open <http://127.0.0.1:8765>. Windows installs are supported, but native Windows browser/locking behavior is not yet an independently tested gate.

## Use

```bash
npx jobforge                          # install into local agents
jobforge app                          # open the console
jobforge scan                         # discover jobs
jobforge score                        # score unscored jobs
jobforge tailor --job 42              # build a resume
jobforge apply --job 42               # fill + review; never submit
jobforge autopilot run --limit 5      # sequential, risk-gated pass
jobforge queue build --limit 10       # supervised queue
jobforge providers list               # optional, for the built-in agent
jobforge agent "Find my best matches" # built-in agent, optional
```

After installing the skill, restart your agent and ask:

> Use JobForge to find my best matches.

## How it works

```text
public ATS APIs / job pages
  → jobs + matches (SQLite)
  → verified profile facts
  → one-page Typst resume + ATS check
  → browser fill + screenshot
  → human review and Submit
```

Artifacts are stored under `output/profile-<profile-id>/...`. Queue items and approved operations execute under the profile that created them, even if another profile is active.

The quality gate blocks below-threshold, duplicate-company, already-submitted, missing-resume, and captcha-stuck jobs before a browser opens.

## Commands

| Command | Purpose |
|---|---|
| `npx jobforge` | Install the MCP server and skill into local agents |
| `jobforge install plan --hosts claude_code,pi` | Show the install plan without writing |
| `jobforge install doctor --hosts ...` | Verify connections, skills, and config entries |
| `jobforge app` | Start the local console |
| `jobforge scan` / `score` | Discover and score jobs |
| `jobforge tailor --job 42` | Generate a tailored resume |
| `jobforge apply --job 42` | Fill an application in review mode |
| `jobforge queue ...` | Build, prepare, and review the supervised queue |
| `jobforge autopilot run` | Sequential risk-consented pass |
| `jobforge risk status` | Show consent and daily usage |
| `jobforge export` | Export local data |

## Configuration

`config.yml` controls sources, paths, preferences, browser behavior, and the active optional provider. Provider keys never go in `config.yml`; they use the OS keychain, a `0600` fallback, or environment variables.

Feature flags: `setup_wizard`, `github_connections`, `scoring_v2`, `agent`, `agent_integrations`. See `docs/21-feature-flags.md`.

## Docs

- `docs/03-architecture.md` — modules, data flow, trust boundaries
- `docs/11-queue.md` — supervised queue
- `docs/12-quality.md` — pre-submit gate
- `docs/13-risk-consent.md` — unattended submit policy
- `docs/23-agent.md` — built-in agent
- `docs/24-providers.md` — model providers
- `docs/33-workbench-p1.md` — MCP read tools
- `docs/34-mcp.md` — MCP setup
- `docs/35-jobforge-skill.md` — Agent Skill
- `docs/37-agent-memory-and-resume.md` — memory and approved resume requests
- `docs/38-skill-install.md` — npx/TUI installer

## Tests

```bash
make test       # pytest
make audit      # parity + security
make decisions  # 14 frozen decision cases
make e2e        # offline FastAPI + Typst smoke
```

Dependencies are pinned in `requirements.lock`. CI runs `test`, `audit`, `decisions`, and `e2e`; the live ATS smoke is manual and never submits.

## Legal

Uses public ATS APIs and public job pages only. Automated form filling may still violate an employer or ATS's terms; keep the human review/submit gate.

MIT for the code. Third-party ATS data remains subject to the employer's terms. “Jobbie” is a trademark of its owner; this project is not affiliated with it.
