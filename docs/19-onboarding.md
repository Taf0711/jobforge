# First-run onboarding

The v0.4.2 setup wizard adds a local, no-account `/setup` flow. This note
records the public product patterns that informed it and the boundaries that
keep JobForge's clean-room/local-first rules intact.

## What other products do

These observations come from public marketing, help, and unauthenticated
onboarding pages only. No Jobbie API, asset, copy, or authenticated data was
used.

| Product | Public first-run pattern | What is useful |
|---|---|---|
| Jobbie (`jobbie.bot/get-hired`) | One low-friction question first (“Are you currently living in the United States?”), “takes about 2 minutes”; alternate variants lead with “Get started with Jobbie / Let’s build your job search profile.” Resume/profile/preferences follow after signup. | Ask one easy question before the full profile; promise a short duration; keep the first action cheap. |
| Simplify | Signup, then onboarding prompts; upload a resume to prefill or enter details manually; progress bar, short explanations, skip fields; editable later. | Resume-first prefill, visible progress, skip optional fields, edit later. |
| Welcome to the Jungle | Account via Google/LinkedIn/email, then profile, search preferences, and matches. | A short sequence: identity → profile → preferences → results. |
| Jobiz | Name, resume upload (PDF/DOCX up to 10 MB), parsing, preferences. | File limits and parsing expectations stated up front. |
| GimmeJob (open source) | Welcome (“about 3 minutes”), goals, job preferences. | Goal-first framing and explicit time estimate. |
| Teal / Huntr | Tracker and resume-tailoring workflows after setup. | Tracking and tailoring are post-onboarding value, not setup gates. |

The common shape is: **small first step → resume/profile → preferences →
results**. The common failure mode is forcing account creation, too many
fields, or an all-or-nothing profile before the user sees any value.

## The JobForge combination

JobForge keeps the useful shape but removes the account and cloud layers:

1. **Resume** — existing profile or upload a resume/profile file (PDF, DOCX,
   TXT, MD, YAML, JSON; 10 MB). Prefill is heuristic; the user reviews it.
   “Skip / use existing” is explicit.
2. **Targets** — titles, locations, source mix, and match threshold. Advanced
   filters (work modes, experience, salary, sponsorship/relocation, exclusions,
   scan keywords, custom boards) are collapsed by default. Sources default to
   a recommended AI/product/GitHub-list mix; broad adds fintech and public
   sector. This keeps the source choice explicit without a five-checkbox wall.
   Preferences live in SQLite; sources/scan settings are written to `config.yml`.
3. **Start** — a short local checklist (resume, targets, sources, optional
   model, Typst/browser warnings), then finish or skip. If no sources are saved,
   finishing applies the recommended mix. The dashboard’s Run scan is next.

Model setup is no longer a wizard step. The Start page links to **Models** when
the agent is enabled; a provider is optional and the deterministic workbench
works without one. GitHub is intentionally optional and post-gate: Start links
to **Connections**, where a read-only token can be connected and repositories
reviewed. It never auto-adds a project.

Design rules:

- No account, email capture, telemetry, social login, or remote profile.
- Resume-first, but never resume-only: manual/skip paths exist.
- Progress is visible; every step is editable and linked from Settings/Profiles.
- The first-run redirect only fires when the install is truly empty. Existing
  profiles/sources are never interrupted; they get a non-blocking banner if
  one side is missing.
- Source presets are a starting point. Discovery is not verification; open the
  posting before tailoring. Indeed/LinkedIn automation, captcha solving, bulk
  submission, and employer account creation remain unavailable.

## Implementation map

- `jobforge/web/app.py` — `/setup` pages and POST handlers, first-run state,
  config writer.
- `jobforge/web/setup_presets.py` — preset registry, selection detection,
  custom-board merge.
- `jobforge/web/templates/setup.html` — three-step UI (Resume, Targets, Start).
  Sources are combined into Targets; models are optional on the final page.
- `jobforge/web/static/app.css` — progress/checklist/preset styles.
- `tests/test_setup.py` — first-run redirect, profile upload, preferences,
  source-mix defaults, source writes, custom boards, finish/skip, and
  existing-install behavior.
- Settings key `setup_complete` persists completion in SQLite.

The wizard writes `sources` and `scan` into `config.yml` atomically with `0600`
permissions. Model setup is no longer a wizard step: the final page links to
`/providers`, which writes the provider, model, and base URL only; the API key
goes to the OS keychain or a `0600` provider file, never to the config.
Preferences live in SQLite. A source-checkout config stays gitignored; an
installed config stays in the JobForge app home.
