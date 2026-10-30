# Changelog

## 0.5.0 — skill/MCP installer (2026-10-30)

- Added `jobforge install hosts|plan|apply|doctor` with JSON output for the
  future TUI and browser wizard. The engine detects Claude Code, Codex, Pi,
  Hermes, OMP, and Generic MCP, creates profile-bound connections, exports the
  Agent Skill, backs up host configs, and verifies the result.
- Added the thin `npx jobforge` terminal wizard. It requires no host arguments,
  preselects detected agents, shows the exact config/skill/backup plan, and asks
  before applying. A first-run resume import is available from the TUI.
- Optional Chromium for application filling is offered separately and is never
  downloaded without consent. `jobforge setup` now skips it by default and
  requires `--install-browser`.
- The npx wrapper can bootstrap the Python package with `uv` and offers Typst
  installation via the platform package manager. Each external download is a
  separate consent prompt.
- No model provider is configured or required by the skill installer. The host
  agent supplies the model.
- Narrowed the project rule to forbid Node in the application runtime while
  allowing the thin npx bootstrap.

### Profile and workflow correctness (2026-10-29)

### Profile-bound artifacts and execution

- Resume artifacts now write under `output/profile-<profile-id>/<company>/<role>-<job-id>/`.
  Two profiles tailoring the same job can no longer overwrite each other's PDF.
- `services.tailor_job` and `services.apply_job` accept an explicit `profile_id`.
  Queue items, autopilot jobs, approved agent operations, and agent tailoring
  execute under the profile recorded when the work was created, not whichever
  profile is active later. Switching the active profile does not retarget
  queued or approved work.
- The approved-resume worker rechecks the connection at execution time. A
  disabled connection, missing scope, or changed profile binding fails the
  operation instead of rendering.

### Crash, tracking, and review fixes

- Fixed the overflow-trimming `NameError` by importing `copy` in the tailoring
  module. The compact/trimming path can now reduce an overlong resume instead
  of crashing.
- Application UPSERT now persists `submitted_at`, and queue/human-confirmed
  submissions update the application row as well as the queue item. Marking an
  application `submitted` from the dashboard also sets the missing timestamp.
- The application status dropdown now uses an external same-origin listener
  instead of a CSP-blocked inline `onchange` handler.
- Dashboard resume and screenshot links now use profile-scoped `/artifacts/...`
  routes instead of `file://` links. Job detail parses stored answers so the
  review panel shows them.
- Transfer credit now uses the source skill's evidence discount, so an adjacent
  skill cannot outrank the directly named skill with equal evidence.
- Autopilot's run limit now counts each attempted job once; a job that is both
  prepared and submitted consumes one slot.
- Removed the unconditional POSIX `fcntl` import from `risk.py`. POSIX uses
  `flock` and Windows uses `msvcrt.locking`; native Windows remains a separate
  untested gate.

### Verification

- `make test`: **504 passed**.
- `make audit`: **0 high / 0 medium** findings.
- `make decisions`: **14/14** frozen cases.
- `make e2e`: passed.
- Chromium smoke: status change POSTs under CSP, `submitted_at` persists, and
  the artifact link renders.

## Unreleased — agent, model providers, and full candidate context

### Human-approved AI answer drafts

- Added `jobforge/automation/drafter.py`: a motivation-answer drafter grounded
  in the selected job and entry-scoped verified facts. Drafts are saved to
  `data/answer_drafts.json` (`0600`), never auto-filled, and only enter
  `data/answers.json` after human approval. Sensitive/salary questions, uncited
  drafts, unsupported numbers, and unsupported skills fail closed.
- Added the `/answers` AI draft UI and `GET /api/answers/drafts`, plus approve
  and reject routes.
- Added a non-mutating `services.read_profile` / `require_profile` seam. Web,
  API, agent, answers, provider, queue, and autopilot read paths now fail closed
  with `SETUP_REQUIRED` instead of creating profile/preferences rows; setup,
  `init`, and explicit import remain the only initializers.
- Added a cross-process `jobforge/locking.py` action lock (POSIX `flock`;
  Windows `msvcrt` path exists but is untested) and wired it through queue
  mutations, service writers, autopilot, answer-bank/draft writes, setup,
  dashboard mutations, and provider changes. Queue and answer-bank writes
  re-read under the lock. Web writes use a nonblocking acquire and return BUSY
  instead of hanging.
- Added 14 tests across the drafter, draft routes, read seam, locking, and queue
  reload behavior.
- Fixed `autopilot watch` skipping events after the newest-500 window by adding
  trace offsets; added a regression test.

### Read-only agent workbench (P1 preview)

- Added schema v5 `agent_instances` / `agent_connections` with explicit profile
  binding, scopes, enabled state, and revocation generation.
- Added `jobforge/workbench/` strict contracts, authorization, and seven
  read-only tools: status, job search, get job, explain fit, readiness, profile,
  and application summaries. Outputs are bounded and redact contact details,
  answers, notes, paths, and unsafe URLs. Reads never use the active profile or
  call a provider, Typst, or a browser.
- Added `jobforge integrations setup|list|disable|doctor` and the machine CLI
  `jobforge tools list|call` (JSON from stdin, one envelope to stdout).
- Added strict JSON validation, cursor/revision handling, and a new
  `agent_integrations` feature flag.
- Added the official `mcp==2.3.0` stdio adapter (`jobforge mcp serve`) with
  current and legacy initialization tests, structured envelopes, read-only
  annotations, stdout cleanliness, and fail-closed startup. Added
  `integrations config` for a portable `mcpServers` entry.
- Refined the MCP surface against current tool-design guidance: tool titles,
  output schema, bounded input schemas with descriptions/enums, prompt-shaped
  descriptions, and strict parameter types. Added the MVP review in
  `docs/36-mcp-mvp-review.md`.
- Added the packaged generic Agent Skill under `jobforge/data/skills/jobforge/`
  and `jobforge skills export`. The skill contains no candidate data or host
  paths and instructs hosts to use only the read-only tools and never claim a
  submission. Host certification remains outstanding.

### Agent memory, dashboard, and approved resume requests

- Schema v6 adds profile-scoped `agent_memories`, `agent_operations`, and
  append-only operation events. Exports omit worker claim nonces; delete order is
  safe.
- Added `jobforge_remember|recall|forget` with `memory:read_write`, contact and
  secret rejection, and profile-level persistence across host sessions.
- Added `jobforge_dashboard|open_dashboard` and an `/operations` dashboard with
  approve/deny pages, a one-claim worker, and a profile-scoped resume artifact
  route.
- Added `jobforge_request_resume` / `get_operation` / `list_operations` with
  `proposals:create`; nothing renders until a human approves in the dashboard.
- Added adaptive one-page fitting that removes only already-selected bullets and
  skills after the compact template still overflows; no content is invented.
- Fixed the tailoring experience cap at four roles (the previous three-entry
  cap hid the newest KPMG role after profile import) and added a regression test.
- Added `integrations grant` for adding scopes without recreating a connection.
- Updated the Agent Skill to recall/remember durable preferences and to use the
  approval-gated resume flow.

### P0 review and artifact safety

- Added an explicit `review_only` policy through `services.apply_job`,
  `launch_apply_subprocess`, CLI `apply --review-only`, the web review route,
  the agent review tool, and queue review mode. It forces `allow_submit=False`
  and `submission_authorized=False` at `run_application`, cannot be combined
  with `--allow-submit`, and does not inherit `browser.allow_submit` or risk
  consent.
- Moved the protected-application check before `LLMClient` and
  `tailor_resume`. Submitted/interview/offer/rejected/withdrawn status,
  `submitted_at`, or a historical submission event now refuses without provider
  or render side effects, preserving sent bytes.
- Changed LLM resume source deduplication to entry-scoped identity, so identical
  bullets under two employers or projects remain independently citable.
- Corrected docs/UI wording from “truth verifier” to “automated grounding
  checks (not semantic proof)”.
- Added 7 tests for the P0 invariants.

### Decision model

- Added `docs/29-decision-model.md`: a typed observation → action contract for
  the autonomous application loop, grounded in resume-forge's hard-gate/evidence
  discipline and Splice's ordered trajectory rules. It defines page, field,
  action, and recovery sets; confidence/abstention; model-use limits; a frozen
  evaluation corpus; and a shadow-first rollout.
- Added `jobforge/automation/decision_model.py` as the offline reference policy:
  terminal guards first, review gaps before navigation, grounded fills only,
  submit gated by quality/risk/required/validation checks, and typed trajectory
  recovery. It is not wired into the browser yet.
- Added 30 unit tests for the contract and rule ordering.
- Implemented decision-model phase 1: a read-only DOM normalizer
  (`automation/observation.py`), a 14-case frozen replay corpus with
  `jobforge decisions replay`, a private shadow recorder
  (`data/decision_shadow.jsonl`, mode 0600, values redacted), and a live
  shadow hook in `run_application`/`services.apply_job`. Shadow mode never
  fills, clicks, or submits; `active` fails closed to `off`.
- Added 18 tests for the normalizer, replay, shadow privacy, the apply shadow
  path, and the resume-forge work-authorization keys.
- Added `jobforge decisions compare` / `make decision-compare`: runs the current
  adapter on the six ATS fixtures, observes the filled page, compares the policy
  action, and records fixture-sourced shadow decisions. The six ATS fixtures
  agree 6/6; the required-why N1 variant returns `request_review`.
- Added 2 comparison tests.
- Ran live supervised applies with shadow enabled (no submit). The first sample
  exposed an Ashby “Autofill from resume” uploader false positive and a
  combobox placeholder-label bug; both were fixed. The second sample recorded
  `request_review` on Why N1. A third sample, after adding the freshness rule,
  recorded `stop_stale` at age 210. Application 176 stayed `prepared`, and risk
  usage remained 4/10.
- Added a 30-day freshness gate to the decision contract (`job_age_days >
  30 → STOP_STALE`) and trajectory monitor. Verified the Ashby public API's true
  `publishedAt=2025-06-05` (508 days old) and corrected job 4136 with the real
  description, salary range, and posted date. N1 was not submitted.
- Saved a truthful, unapproved “Why N1?” draft at `data/answer_drafts.json`
  (`0600`); it is not in the answer bank and cannot be filled by autopilot.

### Tool-using career agent

- Added `/agent` and `jobforge agent`: a provider-backed, tool-using loop that
  can search stored jobs, explain fit and gaps, compare requirements against
  fact/project evidence, recommend profile improvements, tailor a resume, run a
  scan/score, and build the supervised queue.
- Added `agent_threads` and `agent_messages` (schema v4), a `/api/agent` thread
  API, persisted tool traces, token usage in `runs`, and a no-framework chat UI.
- Write tools (`tailor_resume`, `scan_jobs`, `score_jobs`,
  `build_application_queue`, `open_application_review`) are per-message opt-in
  and re-checked on every iteration. `open_application_review` opens the normal
  supervised fill browser only after the quality gate passes; it never clicks
  Submit. There is no submit tool.
- Added a prompt-injection test: a JD or stored text cannot trigger a write when
  writes are disabled, and write permission is not replayed into a later run.
- Bounded the loop by executed tool calls as well as steps/time
  (`agent.max_tool_calls`, default 6). Duplicate calls are refused, calls over
  the budget are not executed, and the final turn runs without tools and is
  forced to synthesize, so a run cannot end silently after burning its budget.
- Added LLM-first resume tailoring (`tailoring.mode: auto|llm|deterministic`):
  the model selects, reorders, shortens, and rephrases verified facts; a
  deterministic verifier rejects unknown source IDs, unsupported numbers,
  unowned skills, wrong employers/dates, and uncited bullets; Typst still renders.
- Added a sequential autopilot (`jobforge autopilot run|status|digest`, `/autopilot`):
  scan → score → LLM tailor → prepare → bounded submit → digest. It uses an ATS
  allowlist (Greenhouse, Lever, Ashby, Workable, SmartRecruiters) and stops on
  captcha, 2FA, account wall, or quality blocker.
- Added the `autopilot_submit` risk flag. It is off by default, requires the exact
  consent phrase, shares the 10/day cap with `unattended_submit`, and is
  sequential. The old `bulk_unattended` prohibition remains for mass/parallel
  submission.
- Added `docs/25-jobbie-parity.md` with the REA public-surface audit and the
  keep/change/adapt/reject matrix.
- Added live autopilot observability: atomic `data/autopilot_state.json`, append-only
  `data/autopilot_trace.jsonl`, `/api/autopilot/status`, a polling Live trace panel
  on `/autopilot`, and CLI `autopilot status|trace|watch`.
- Added a deterministic form-question cognition layer (`automation/cognition.py`)
  that routes profile, motivation, sensitive, and unknown questions to fill/review/
  skip; autopilot records `required_review` and stops before Submit.
- Fixed Ashby filling: resume upload now targets the required `_systemfield_resume`
  input and verifies `files.length`; location autocomplete is filled as a combobox;
  required file/combobox fields block auto-submit; blocked autopilot form runs close
  immediately instead of waiting for terminal input.
- Adapted the Kobra AI-editor interaction patterns to the vanilla-JS UI:
  selection-to-agent Ask on job descriptions and answers, word-chunk answer
  reveal, orb/shimmer status, tool timeline, and reduced-motion support. No
  React, npm, contentEditable, or external favicon service was added.
- Added `docs/23-agent.md`.

### Model providers and models

- Added `/providers` and `jobforge providers list|connect|models|use|test|disconnect`.
  Model setup is optional and linked from setup's final page.
- Added provider adapters for OpenAI/ChatGPT, Anthropic/Claude, Google Gemini,
  OpenRouter, Groq, Mistral, xAI, DeepSeek, Together, Fireworks, Baseten,
  Ollama, LM Studio, and custom OpenAI-compatible endpoints.
- Added OpenAI-compatible and Anthropic tool calling, model discovery, and a
  tiny explicit test-chat action.
- Added the official OpenRouter OAuth PKCE flow with state, verifier expiry,
  single-use codes, and no key in callbacks, URLs, templates, or exports.
- Provider keys use the OS keychain, an atomic `0600` file, or environment
  variables; they are never written to `config.yml`, SQLite, exports, templates,
  logs, errors, or tool output. Provider HTTP requests disable redirects.
- ChatGPT/Claude/Gemini consumer subscriptions are explicitly not treated as API
  credentials. OpenRouter OAuth is implemented. OpenAI's official Sign in with
  ChatGPT OSS flow (Responses API) and Google Gemini OAuth/ADC are documented
  upstream but not wired in this build; Anthropic consumer OAuth is prohibited
  for third-party apps.
- Added an experimental Claude Code CLI bridge: a text-only provider that runs
  the official `claude` CLI with tools, MCP, slash commands, and session
  persistence disabled. JobForge never reads or stores Claude Code credentials;
  the CLI manages its own sign-in. The bridge cannot use JobForge tools, write,
  or submit, and is subject to Anthropic's terms.
- Added `docs/24-providers.md`.
- Streamlined first-run setup from five steps to three: Resume, Targets, and
  Start. Sources now default to a recommended mix; advanced filters and custom
  boards are collapsed; model setup is optional on the final page. Finishing
  with no sources applies the recommended mix.

### Full candidate context and advisor

- The resume-forge importer now preserves `evidence_matrix.yml` on
  `Profile.evidence` and programs, verified quantifiers, sensitive wording,
  resume style rules, scoring inputs, preferences, and authorization on
  `Profile.context`.
- Added a bounded context pack with skills/evidence/proof, projects, experience,
  facts, application funnel/channel performance, network, and market snapshot.
  Email and phone are redacted by default.
- Added a deterministic advisor for strongest evidence, skills-list-only claims,
  target gaps, project portfolio quality, funnel/channel behavior, compensation
  policy, interview readiness, data-quality warnings, and next actions.
- Imported resume-forge tracker rows are excluded from the live job feed, queue,
  market snapshot, and agent search by default so historical applications do not
  masquerade as fresh high-fit roles.

### UI and flags

- Added **Agent** and **Models** nav links, an optional model link on setup's
  final page, and Settings runtime/feature-flag integration.
- Added `features.agent` and `JOBFORGE_FEATURE_AGENT`.
- Added tests for provider auth/storage/model parsing, OAuth, no-secret leakage,
  context redaction and evidence, advisor output, tool write gating, prompt
  injection, max-step stop, thread persistence, and the web API.
- Enriched the initial agent prompt context with the full skill inventory,
  project/experience bullets, project URLs, and up to 60 facts (60k-character
  budget), and updated the system prompt to synthesize cross-factor advice from
  that context instead of only restating the deterministic advisor.

## Unreleased — feature flags, calibrated scoring, and GitHub job lists

### GitHub job-list sources

- Added `sources.github_jobs` for public README tables such as
  `SimplifyJobs/New-Grad-Positions` and `SimplifyJobs/Summer2026-Internships`.
- Supports HTML and Markdown tables, grouped-company rows, status emoji cleanup,
  age parsing, and deduplication.
- Imports only rows with a direct employer/ATS URL; aggregator-only rows are
  skipped and tracking parameters are stripped. The employer page remains the
  source of truth for the full JD.
- Added the **Community GitHub job lists** setup preset and custom
  `Owner/Repo` field, the `github_jobs` source filter, `docs/22-github-job-lists.md`,
  and `tests/test_github_jobs_source.py`.

### Feature flags

- Added local `features` flags with environment overrides and a
  **Settings → Feature flags** form: `setup_wizard`, `github_connections`, and
  `scoring_v2`.
- Disabling `setup_wizard` hides `/setup`, the first-run redirect, and the setup
  banner; disabling `github_connections` hides `/connections`, repository sync,
  and project-edit links. Data is not deleted.
- Added `tests/test_features.py` and `docs/21-feature-flags.md`.

### Scoring v2

- Requirement parsing now recognizes more headings (`What We Look For`,
  `What You'll Bring`, `Minimum Requirements`, etc.), normalizes curly
  apostrophes/markdown, and filters salary, benefits, job IDs, headcount, and
  legal boilerplate out of requirements.
- Years, degree, GPA, clearance, citizenship, and ITAR gates are scoped to
  requirement/nice-to-have text instead of company bios. Years ranges use the
  floor (`2–12+` = `2+`), and company-age lines are ignored.
- `Sr.`/`Snr` trigger the senior-title gate.
- Remote detection no longer treats a company's "remote-first" blurb as a remote
  role; non-US remote scopes are penalized for US/Remote preferences.
- Skill coverage applies evidence to direct skills and stops giving positive
  credit to unparsed prose under `scoring_v2`. The legacy calibration remains
  available with `features.scoring_v2: false`.
- Thin-JD detection ignores skills found only in responsibilities.
- Added `tests/test_scoring_v2.py`; the exact calibration change is documented in
  `docs/21-feature-flags.md`.

## v0.4.2 — 2026-10-26

### GitHub connections

- Added `/connections` and `/connections/github`: optional read-only GitHub
  connection, repository sync, README/language context, and a review form before
  anything enters the resume.
- GitHub tokens are stored in the OS keychain when available, with an atomic
  `0600` local fallback, or supplied through `GITHUB_TOKEN` /
  `JOBFORGE_GITHUB_TOKEN`. Tokens never appear in the database, templates,
  exports, or error messages.
- Syncing is capped at 300 owner repositories and three API pages; README
  responses are capped at 64 KB. The client only talks to `api.github.com`, does
  not follow redirects, and never calls a write endpoint.
- README suggestions are extracted only from overview/features sections and
  skip install/setup/usage sections. They prefill an editable project form; the
  human submit is the truth boundary.
- Confirmed imports add a `source: github` project, project facts, URL, tech
  line, tags, and optionally detected canonical skills; jobs are rescored when
  skills change. Disconnecting clears the token/cache but keeps imported
  projects.
- Added `connections` and `github_repos` tables, `docs/20-connections.md`,
  `/connections` in offline e2e, and `tests/test_github_connection.py`.
- Added `keyring` to runtime dependencies.

### Resume tailoring

- Tailored resumes now use the refined resume-forge visual language: 20pt
  small-caps header, citizenship, Education → Experience → Projects → Technical
  Skills, grouped skills, resume-forge margins/fonts, no summary/certifications.
- The importer now carries project URLs, hides dead projects, merges education
  dates/courses from the published PDF, and keeps the base three experience
  entries on the page.
- Project bullets are capped at three per project; a compact variant preserves
  the same hierarchy when the full layout would exceed one page.

### First-run setup wizard

- Added a local, no-account `/setup` wizard: profile/resume → targets →
  sources → readiness, with skip/back and editable settings afterward.
- First empty launches redirect to setup; existing profiles/sources are not
  interrupted, and incomplete installs get a non-blocking banner.
- Added curated source presets (AI/developer tools, fintech/banking, product
  infrastructure, public sector) plus custom board fields. The wizard writes
  only `sources` and `scan` atomically into `config.yml`; preferences live in
  SQLite.
- Added preset detection, custom-board preservation, and readiness checks for
  profile, targets, sources, Typst, and browser.
- Added `/setup` tests for first-run redirect, profile upload, preferences,
  source writes, custom boards, finish/skip, all four rendered steps, and
  existing-install behavior.
- Product patterns came from public Jobbie/Simplify/Welcome to the Jungle/Jobiz
  onboarding pages; no Jobbie API, assets, copy, or authenticated data was used.
  See `docs/19-onboarding.md`.

## v0.4.1 — 2026-10-26

Security and integrity fixes from the tailoring/apply audit.

### Scanning

- Raised the response cap from 5 MB to 32 MB so Stripe, Cloudflare, Datadog,
  Anthropic, and Databricks Greenhouse boards no longer fail.
- Added scan-wide title include/exclude gates and a per-scan new-job budget;
  deferred jobs are picked up by later scans.
- Added USAJobs (env-gated) and NYC Open Data public-sector sources.
- Added the reference direct-ATS board set (Greenhouse, Ashby, Lever, Workday)
  to the local config; removed the dead `ashby:cursor` board and its rows.
- Fixed the bare `next` skill alias that matched "next generation".

### Tailoring

- Re-tailoring an existing application no longer resets it to `draft` or wipes
  answers, cover-letter paths, or screenshots. Submitted/interview/offer
  applications are refused instead of silently replaced.
- Tailored output directories include location and job id, so two postings with
  the same company/title no longer overwrite each other.
- The ATS gate is enforced: one page, text layer, email, phone, section headers,
  and no embedded images are blockers, with an automatic compact-template retry.
- The deterministic cover letter only claims skills the profile actually owns.
  LLM summaries/cover letters now reject unsupported skills as well as numbers.
- Profiles missing name/email/phone fail before rendering.

### Application filling

- Auto-submit requires a confirmation page/text or a success URL; a click alone
  is no longer recorded as submitted. Captcha after submit is detected and
  blocks confirmation.
- Unattended submit is rejected unless it came through `services.apply_job` and
  the persisted risk policy.
- `services.apply_job` runs the quality gate itself as defense in depth.
- The browser always opens a fresh tab instead of reusing the user's first tab.
- Cover letters render to both `.txt` (editable) and `.pdf` (ATS-uploadable),
  are sent to their labelled file input, and are preserved in the application
  record.
- Form filling does two passes for conditional fields, reports required
  checkboxes/empty required fields, and caps large forms with an explicit note.
- Resume/cover-letter uploads match labelled inputs; ambiguous multi-file forms
  are left for the human.
- Added a SmartRecruiters adapter; Workday now opens the posting's apply wizard
  and stops cleanly at account walls.
- Added Cloudflare challenge copy and stricter submit-button matching.
- Sponsorship answers prefer explicit profile work-auth values.

### Verification and release

- Adapter eval now covers six fixtures: Ashby, Greenhouse, Lever, Workable,
  SmartRecruiters, and Workday.
- Added `make smoke` / `scripts/smoke_live_ats.py`: a live, non-submitting fill
  of the public Lever demo form.
- Added GitHub Actions CI for tests, audit, adapter eval, and offline e2e.
- Added `requirements.lock`; `make lock` regenerates it.
- Regression tests for re-tailor state preservation, ATS blockers, grounded
  letters, upload targeting, auto-submit confirmation, quality-gate bypass,
  new-tab behavior, conditional-field filling, and the two new adapters.
