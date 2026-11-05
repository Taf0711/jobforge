# Architecture

JobForge is a local-first, single-user job-search workbench. It runs on your machine, stores state in SQLite, uses your browser, and never submits without a human click except the explicit risk-consented modes.

## Principles

1. Local only: no account, cloud, or telemetry.
2. Explainable: every score has reasons and gaps.
3. Truthful: tailoring may reorder or rephrase verified facts, never invent.
4. Supervised: the default flow fills the form and stops. Submission requires explicit operation-bound authorization or the risk-consented autopilot modes.
5. Public job data only; no Jobbie API or authenticated scraping.
6. No captcha arms race: detect, pause, hand off.
7. One service layer for CLI, HTTP, and MCP.
8. The agent orchestrates; it does not bypass scoring, facts, quality, or human submit.

## Modules

```text
jobforge/
  config.py, db.py, models.py   config, SQLite, dataclasses
  sources/                      public ATS + public-sector discovery
  scan/                         first-pass filters + quick scan ranking
  matching/                     resume/JD parsing, skill graph, deep fit scoring
  tailoring/                    fact selection, grounding checks, Typst, ATS check
  automation/                   browser, form filling, cognition, decision model
  email/                        IMAP, verification codes, reply classification
  queue.py, autopilot.py        supervised and risk-gated application loops
  quality.py, risk.py           pre-browser gate, consent, daily cap
  locking.py                    cross-process domain-action lock
  agent/                        optional built-in agent and model providers
  workbench/                    profile-bound MCP read tools and proposals
  installer/                    host detection, install plan/apply/doctor
  web/                          local console, agent chat, providers, approvals
  audit/                        parity, security, adapter eval, decision replay
  cli.py, __main__.py           CLI entry points
```

## Data flow

### Discover

```text
sources → normalize → dedupe → first-pass filter → quick scan → scan_matches
```

Direct ATS sources are board-based. USAJobs and NYC Open Data are singleton sources. `github_jobs` keeps direct employer/ATS links only. A flat `scan_sources.yml` registry can replace the grouped boards and carries an explicit `competition_profile`. Discovery is not verification; open the posting before tailoring.

### Deep score

```text
selected jobs + profile skills + skill graph → coverage with partial credit
job requirements → hard gates (auth, years, degree, clearance, location risk)
fit = coverage × gate survival × thin/lead-only penalty × 100
```

Years are resolved from an explicit profile/preference value or a conservative
estimate from dated non-internship experience. Unset is unknown: it does not
multiply the score, and the match reports the unknown instead of treating it as
zero. Quick scan caps over-level, unknown, and unparsed year rows so they cannot
present as "strong" before deep scoring. Location is a risk/gap and a pursuit
modifier, not a fit knockout. Every match stores reasons, gaps, and a per-requirement table. Scan ranking and deep fit are separate; scanning does not deep-score every row.

### Tailor

```text
verified facts + job requirements + gaps
  → select/reorder/rephrase with entry-scoped source IDs
  → automated grounding checks, deterministic fallback
  → Typst render → output/profile-<id>/<company>/<role>-<job-id>/resume.pdf
  → one-page/text-layer/contact ATS check
```

### Apply

```text
apply_url → ATS detect → persistent Chromium → adapter fill
  → screenshot + fill report → human review → human Submit
```

`review_only` forces `allow_submit=False` and `submission_authorized=False` through the CLI, web, queue, and agent seams.

### Track

```text
IMAP poll → verification codes / replies → classify → event + status
```

## Trust boundaries

| Risk | Control |
|---|---|
| SSRF via pasted URL | `http(s)` only; block private/loopback/link-local targets; redirect and byte caps |
| Prompt injection in a JD | JD is untrusted data; system prompt forbids instruction following; tools are allowlisted |
| Invented resume content | Fact bank, source IDs, number/skill/entry checks, human PDF review |
| XSS / injection | Jinja autoescape, no unsafe HTML filter, parameterized SQL, CSP |
| Upload path traversal | basename-only names, UUID paths, confined output root |
| Secret leakage | keychain/0600/env only; never in config, DB, exports, templates, logs, or tool output |
| Agent privilege | no shell/SQL/arbitrary URL/browser-submit tool; writes are per-message opt-in in the built-in agent and scope/operation-gated over MCP |
| Unattended abuse | off by default; 10/day shared cap; consent phrase; quality gate; captcha/2FA stop |
| Credential theft | no password storage; persistent browser profile stays local |
| Rate limits | per-host token bucket and backoff |

Queue items and approved operations carry an explicit profile binding. Execution resolves that profile, not the ambient active profile, and writes under `output/profile-<id>/...`.

## Run modes

- `npx jobforge` — install the MCP server and skill into local agents.
- `jobforge app` — local console.
- `jobforge scan` — discovery, filtering, and quick ranking.
- `jobforge score --top 20` / `--job 42` — deep fit on demand.
- `jobforge tailor --job 42` — resume PDF.
- `jobforge apply --job 42` — fill and review.
- `jobforge queue ...` — supervised queue.
- `jobforge autopilot run --limit 5` — sequential, risk-gated pass.
- `jobforge email poll` — IMAP.
- `jobforge providers ...` / `agent ...` — optional built-in agent.
- `jobforge audit` — parity and security checks.

## Non-goals

Cloud hosting, multi-tenant auth, payments, mobile apps, employer marketplace, captcha solving, Indeed/LinkedIn automation, mass submission, and employer account creation.
