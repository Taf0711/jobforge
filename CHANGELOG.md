# Changelog

## 0.8.2 — JD parsing integrity and full-posting retrieval (2026-11-13)

- Fixed two parser artifacts that collapsed gate survival. Imperative prose such
  as "Master our development process" is no longer read as a master's-degree
  floor, and "AMTS"/"College Grad" software titles are no longer reported as a
  title-level mismatch. Degree detection is structural and returns the lowest
  required degree; preferred-only degree clauses do not set a floor.
- Added section-aware retrieval over the complete job description. The built-in
  agent and MCP `jobforge_get_job` accept `description_query` and
  `description_limit` for relevant, section-labelled excerpts, plus
  `description_offset` for paging the raw text. The parser now retains every
  recognized section, so culture, preferred qualifications, and prose
  requirements remain addressable instead of being lost behind a truncated
  prefix or treated as must-haves.
- Fixed the parity audit's stale "Fill application" needle. The check now
  verifies the actual apply form action in `job_detail.html`; audit parity is
  47/47 again.
- Gates: **624 passed**; audit **47/47**, **0 high / 0 medium**; decisions
  **14/14**; adapter eval **6/6**; offline e2e **ok**.

## 0.8.1 — host-agent email ingestion (2026-11-13)

- Added `jobforge_ingest_emails` (`email:write`) for host-agent ingestion.
  An agent that already has a Gmail, Outlook, or other mail connector can pass
  normalized employer messages to JobForge instead of giving JobForge a mailbox
  credential. JobForge still performs deterministic classification, matching,
  dedupe, and forward-only status changes; the host cannot assert a category or
  application. `apply_status: false` links without changing a stage.
- Relative paths in `config.yml` now resolve against the config file's
  directory, not the process cwd. An installed app or host-agent MCP server
  started from a checkout now reads the same app-home database and connection.
- Work authorization is now editable in **Profiles → Work authorization** and
  readable through `jobforge_get_profile` with `section: "work_authorization"`.
  Unset citizenship, ITAR eligibility, or clearance eligibility adds a gap
  instead of zeroing the fit; an explicit No still applies the hard gate.
- Gates: **611 passed**; audit **0 high / 0 medium**; decisions **14/14**.

## 0.8.0 — email-driven application tracker (2026-11-12)

- Added an email-driven application tracker. IMAP polling now matches employer
  mail to one application using company, role, sender domain, ATS, and job URL
  signals; ambiguous mail stays unlinked for human review. Confirmation,
  assessment, interview, offer, and rejection messages advance the stage
  forward-only, record a timeline event, and set `submitted_at` when the email
  proves an application exists. The dashboard shows stage provenance, a
  per-application timeline, an inbox review queue, and a background sync while
  the dashboard is open. `jobforge email poll|status|watch` covers the CLI.
  The IMAP password is keychain-first (0600 fallback) and never enters config,
  SQLite, exports, logs, or templates. Schema v8 adds email match/status
  provenance and `applications.status_source`.
- The answer bank now answers yes/no minimum-years screening questions
  (for example "3+ years of relevant non-internship professional
  experience?") from the confirmed `years_of_experience` preference.
  An unconfirmed count stays unresolved for the human.
- Pending operation requests now transition to `expired` after their TTL
  passes. The dashboard, operations page, operation polling, and MCP operation
  reads sweep stale requests so they no longer sit in the approval queue
  forever.
- Resume renders can now export a role-named PDF copy into the caller's working
  directory. `jobforge_request_resume` accepts an absolute `destination_dir`
  (normally the agent session cwd) and the MCP server defaults to its working
  directory when it looks like a session workspace. `jobforge tailor --out-dir`
  exposes the same copy for the CLI. The canonical artifact stays in the
  JobForge output store for the dashboard and approval trail; the operation
  result reports `export_path`.
- Added `jobforge import-url <url>` and a Jobs-page URL form. A human can paste
  one public posting; JobForge fetches it, stores it, quick-scores it, and
  deep-scores it. The generic URL parser now recovers company and location from
  Greenhouse, Workday, and other pages, prefers the job-description container,
  and strips page chrome such as breadcrumbs and "Back to jobs" from the
  description.
- Added the `jobs:write` scope and `jobforge_import_job` MCP tool. A host agent
  can read a public posting from any site with its own browser or web-fetch
  tool and import normalized fields. JobForge validates the URL without
  fetching it, dedupes by canonical fingerprint, stores the job, and
  quick-scores it for the bound profile. Deep scoring stays separate.

## 0.7.1 — resume courses, page fill, and inline previews (2026-11-12)

- Resume tailoring now preserves the education course list. The PDF parser
  reads `Relevant Courses:` lines (including wrapped lines), re-imports keep
  curated courses when the parser cannot see them, and older profiles fall back
  read-only to their source resume. The profile page shows the parsed education
  and courses.
- Resume layout no longer leaves a blank band at the bottom. Spacing is
  explicit, the summary renders when present, and a one-page fill pass spreads
  leftover height into leading and section gaps without adding or removing
  content. The renderer prefers keeping all three projects and trims bullets
  before dropping a project.
- The tailored PDF is named `<First>_<Last>_Resume.pdf`, and the dashboard
  artifact route serves it inline so it opens in the browser instead of
  downloading as `jobforge-<id>-resume.pdf`.

## 0.7.0 — workbench dashboard, actionable queue, and live approvals (2026-11-07)

- Replaced the static dashboard shell with a workbench: left rail on desktop,
  off-canvas drawer on mobile, live store/profile/version context, and no
  auto-opening "More" menu. Every page now identifies the store it is reading.
- Rebuilt Today around "Needs you": approvals, queue items, failed runs, setup,
  pipeline counts, fit-sorted top roles, agent activity, and actionable empty
  states. Scan-sorted rows no longer masquerade as best matches.
- Approvals are now a review workspace: job/profile context, fit, artifact
  preview, quality/risk/cap state, requested-by connection, timeline, and a
  sticky approve/deny bar. `/api/operations/{id}` and a polling client update
  approved/running/succeeded states without a refresh.
- Queue is actionable from the dashboard: build, prepare, open, skip, reopen,
  prune below-floor items. Skipped and failed items no longer reserve their job
  id or company slot; a better same-company job can re-enter.
- Jobs is a fit-first explorer: labeled filters, fit/scan/newest sort,
  pagination, neutral unscored rows, human-readable sources, row actions, and
  mobile card rows. Job detail gets a sticky action bar and requirement rows
  that distinguish "not scored" and "thin evidence".
- Profiles ask before applying an import, use a CSP-safe reparse confirmation,
  and expose the project bank earlier. Agents group scopes read/write, update
  last-seen from an MCP startup heartbeat, and provide copy-config guidance.
- Models are searchable; settings have section navigation; runs, inbox,
  answers, and autopilot have clearer empty/error states and less raw config.
- Added `scripts/ui_smoke.py` and `make ui-smoke`: 30 desktop/mobile route
  checks for console errors, overflow, the old More menu, store identity, and
  labeled filters.
- Tests: **575 passed**; audit **0 high / 0 medium**; decisions **14/14**;
  adapter eval and offline e2e **ok**; UI smoke **30/30**. The submission tool
  remains experimental until a successful live submit smoke on a captcha-free,
  employer-sanctioned test form.

## 0.6.7 — queue reservations and live-store dashboard (2026-11-07)

- Queue builds now treat only `pending`, `prepared`, and `submitted` items as
  active reservations. `skipped` and `failed` items no longer permanently
  consume their job id or company slot.
- Rebuilding for a skipped/failed job reopens that item instead of appending a
  duplicate; a better job at the same company can enter when it ranks first.
- Tests: **571 passed**; audit **0 high / 0 medium**.

## 0.6.6 — title and sparse-requirement calibration (2026-11-07)

- Title relevance now requires a software-adjacent title token for the 0.85
  adjacency band; non-software roles (sales/support/IT operations) fall to the
  0.50 outside-target band instead of inheriting credit from the word
  "engineer".
- Sparse-JD penalty is limited to one parsed clause with fewer than three
  parsed skills. A rich OR clause such as Palantir's language list remains a
  real match instead of being discarded as sparse.
- Gates: **569 passed**; audit **0 high / 0 medium**; decisions **14/14**;
  adapter eval and offline e2e **ok**.

## 0.6.5 — scoring integrity fixes (2026-11-07)

- Unparsed prose requirements stay visible but no longer contribute weight to
  the calibrated coverage denominator.
- One requirement clause is one row: OR lists such as "Java, Rust, Scala, C++,
  or C" use the best matching alternative instead of emitting one full-weight
  row per alternative.
- Requirement parsing uses only the job description; title, company, and
  location are hints, not skill evidence. Body paragraphs without a bullet or
  a recognized requirements section no longer become must-have rows.
- `Spring 2027` and "go to market" are context-guarded, like the existing C/R
  homonym guards.
- Direct skills-list-only evidence is reported as thin (`thin: true`) rather
  than a missing gap; missing skills remain gaps.
- Sparse requirement sets (one parsed clause plus unparsed prose) receive the
  existing thin-JD penalty. Title relevance falls back to the profile's recent
  titles/headline when preferences do not list target titles.
- Tests: **569 passed**; audit **0 high / 0 medium**; decisions **14/14**;
  adapter eval and offline e2e **ok**.

## 0.6.4 — known-ATS board selection and full Lever descriptions (2026-11-07)

- `jobforge_scan` now accepts explicit board selectors for the implemented
  public ATS adapters, for example `lever:palantir`, `greenhouse:affirm`, or
  `ashby:replit`. This is bounded to known ATS board APIs; arbitrary URLs are
  still rejected. Source-name scans continue to use only configured boards.
- Lever ingestion now merges `descriptionPlain`, `lists` sections, and
  `additionalPlain` into the stored description. Previously only the intro was
  kept, so real requirements looked like thin/lead-only stubs and deep scores
  collapsed.

## 0.6.3 — installer skill backups outside the skills root (2026-11-06)

- `jobforge install apply` no longer writes the previous skill to
  `<skills>/<name>.jobforge.bak`. That directory was indexed by hosts as a
  second skill with the same name, so an agent could load the stale copy while
  the live `<skills>/jobforge` was current. Backups now land beside the skills
  root in `<skills>.jobforge.bak/jobforge`, which no host scans.
- Existing hosts that already carry `<skills>/jobforge.jobforge.bak` have that
  duplicate removed on the next `install apply`.

## 0.6.2 — MCP write surface, project repair, and authorized submission (2026-11-04)

- Added scope-gated MCP write tools: `jobforge_scan` (`scan:run`),
  `jobforge_score` (`matches:write`), `jobforge_build_queue` (`queue:write`),
  and `jobforge_request_submission` (`submission:request`).
- `jobforge_request_submission` only creates a pending one-use operation. A
  human must approve that exact request in `/operations`; the worker rechecks
  the connection, profile, scope, quality gate, risk consent, and 10/day cap
  before calling the existing apply path with submission enabled.
- The approval page is action-aware: submission requests show the job, bound
  profile, quality blockers, risk-consent state, daily cap, and the explicit
  consequence of approval.
- Read tools still use the read-only SQLite connection; action tools use a
  writable connection and always resolve the bound profile rather than the
  active profile.
- Added `scan:run`, `matches:write`, `queue:write`, and `submission:request`
  scopes. They are opt-in; the installer defaults remain read-oriented.
- Updated the Agent Skill, MCP docs, and `docs/40-mcp-write-surface.md` with the
  corrected rule: the agent never submits without explicit operation-bound
  authorization.
- Resume parsing now treats each project header as its own entry instead of
  reusing the experience parser. Project titles, tech lines, dates, URLs, and
  bullets stay together, and a following project header is no longer swallowed
  into the previous project's last bullet.
- Added `resume_import.project_mode` (default `merge`) and a **Settings →
  Resume imports** selector. Merge keeps curated projects, updates matching
  titles, and appends new projects; replace mirrors the resume. Added
  `jobforge reparse-profile` to refresh only a profile's projects and project
  facts from its saved resume, with `--profile`, `--resume`, `--mode`, and
  `--dry-run`.
- `mcp==2.3.0` is now a core dependency, so a plain `uv tool install`/`npx
  jobforge` runtime can serve the MCP adapter without a separate extra.
- Tests cover schemas, scope denial, bound-profile scoring, queue conflicts,
  submission requests, approval UI, worker risk/quality/captcha handling,
  hidden hCaptcha detection, Lever-style Yes/No checkbox groups, and project
  section parsing.
- The live non-submitting ATS smoke passes with hCaptcha handoff. A live submit
  attempt against the public Lever demo correctly returned `CAPTCHA_REQUIRED`
  and did not submit. `jobforge_request_submission` remains **experimental**
  until a successful live submit smoke on a captcha-free, employer-sanctioned
  test form. It is still double-gated by dashboard approval and risk consent.

## 0.6.1 — years-of-experience correctness (2026-11-02)

- Deep scoring no longer treats an unset years value as `0`. Years resolve from
  an explicit profile/preference value, then a conservative estimate from dated
  non-internship experience; otherwise the year gate is skipped and the match
  reports that years are unknown.
- Year mentions outside a recognized requirements section are reported as
  "mentioned but not parsed" instead of being treated as having no year
  requirement.
- Quick scan parses required years when mentioned and caps over-level, unknown,
  and unparsed rows at 35/50/70, so a 5+ year role cannot present as "strong"
  before deep scoring.
- Setup/settings expose a years field and preserve an explicit `0`; blank means
  derive-or-unknown. Resume-forge import maps the preference when present.
  Legacy stored `0` values without a confirmation flag are treated as unset.
- Job detail shows the effective years, source, and required floor.
- Claude Code provider tests now patch the CLI status at the route/client
  boundary, so CI no longer depends on a locally installed `claude` binary.
- Tests: **531 passed**; audit **0 high / 0 medium**; decisions **14/14**.

## 0.6.0 — scan-first ranking (2026-11-01)

- Added a two-stage discovery pipeline: fast scan ranking in `scan_matches`,
  then deep fit in `matches`.
- Added first-pass filters for stale, non-US, internship, senior, non-SWE,
  already-applied, recent-rejection, excluded-company, and duplicate rows.
- Added the resume-forge quick-scan rubric: level, archetype relevance,
  location/relocation, freshness/competition, domain adjacency, and stack.
- Added flat `scan_sources.yml` registry support with per-source
  `competition_profile` values.
- Added HN and GitHub-pipe source adapters.
- Stopped multiplying location into deep fit; location is now a risk/gap and a
  cost-of-living pursuit modifier.
- Lead-only descriptions are marked provisional instead of being presented as
  precise fits.
- MCP, CLI, and web jobs lists now expose scan score, confidence, competition
  profile, age, and scan reasons.

## 0.5.3 — Installer runtime updates (2026-11-01)

- Fixed the Hermes installer path: `hermes mcp add` now receives a non-interactive
  answer to its tool-selection prompt, and the installer rejects entries the host
  saved as disabled instead of reporting a false success.
- `npx jobforge` now checks the installed uv-managed runtime version and offers
  to update a stale runtime to the installer's matching release tag. Use
  `--update` to force or `--no-update` to skip.
- Pinned the default npm runtime spec to `git+...@v${VERSION}` instead of
  floating on `main`.
- Switched the npm installer workflow to GitHub OIDC trusted publishing.
- The first `jobforge` npm publish is a one-time manual OTP bootstrap; later
  releases use the trusted publisher and need no long-lived token.
- Added the npm repository metadata required for trusted publishing.

## 0.5.2 — UI refinement (2026-10-30)

- Refined the console typography, spacing, surface depth, table scanability, and
  status badges.
- Added consistent hover, focus-visible, active, disabled, and reduced-motion
  states.
- Switched the palette to OKLCH tokens and added semantic surface/state colors.
- Added a skip link, `aria-current` navigation, tabular numbers, and mobile
  column hiding for data tables.
- Collapsed the job description and replaced raw artifact paths in quality and
  history rows with readable states and PDF links.

## 0.5.1 — console redesign (2026-10-30)

- Replaced the standalone dashboard hero and built-in-agent CTA with a
  skill/MCP-first console: connected agents, pending approvals, quick actions,
  then best matches.
- Added `/agents` with profile-bound connections, detected hosts, MCP/skill
  status, and the `npx jobforge` install/repair command.
- Reduced the primary nav to Console, Jobs, Applications, Approvals, Agents,
  Queue, Profiles, and Settings; utility pages are under **More**.
- No behavior change to the agent, queue, quality, or submit gates.

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
