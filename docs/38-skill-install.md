# Skill/MCP installer

JobForge installs into your coding agent instead of running as a separate chat app. The host supplies the model; JobForge supplies local jobs, scoring, verified facts, rendering, and approval-gated actions.

## One command

```bash
npx jobforge
```

From a checkout before the npm release: `npx --yes --package ./installer jobforge`.

No host arguments. The wizard detects local agents, shows what it will write, and asks before changing anything.

It can:

- import a resume and create the profile;
- install JobForge with `uv` if the runtime is missing;
- offer Typst through `brew`/`winget` if missing;
- offer optional Chromium for application filling.

Every external download is a separate consent prompt. No model provider is configured or required.

## Hosts

| Host | Config | Skill directory | Method |
|---|---|---|---|
| Claude Code | `~/.claude.json` | `~/.claude/skills` | `claude mcp add --scope user` |
| Codex | `~/.codex/config.toml` | `~/.codex/skills` | `codex mcp add` |
| Pi | `~/.pi/agent/mcp.json` | `~/.pi/agent/skills` | `pi mcp add` |
| Hermes | `~/.hermes/config.yaml` | `~/.hermes/skills` | `hermes mcp add` |
| OMP | `~/.omp/agent/mcp.json` | `~/.agents/skills` | JSON merge |
| Generic MCP | `~/.config/jobforge/mcp.json` | `~/.agents/skills` | JSON merge |

Codex and Hermes require their CLI for safe TOML/YAML merging. If it is missing, that host is reported as `HOST_CLI_REQUIRED` and left unchanged.

## Engine

```bash
jobforge install hosts
jobforge install plan --hosts claude_code,pi
jobforge install apply --hosts claude_code,pi --yes
jobforge install doctor --hosts claude_code,pi
```

`plan` is read-only. `apply` backs up the host config, exports the skill, creates one profile-bound connection, writes the MCP entry, and verifies it. Repeated runs reuse the connection and replace the managed entry.

## Boundaries

- No model provider or API key.
- No submit, approval, employer account, or password storage.
- No unrelated packages. Chromium is the only optional download in this slice.
- No silent host-config edits; backups are written first.

## Current gap

The skill is still read-only plus approval-gated resume requests. Host-generated resume proposals and the review-only browser tool are the next capability slice. Targets, sources, and scanning still use the local console.
