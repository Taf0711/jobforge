# Changelog

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
