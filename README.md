# JobForge

A local-first job search workbench: discover fresh roles from public ATS boards, score them with an explainable model, tailor a truthful resume, fill the application in your own browser, and track the outcome.

Built as an independent clean-room replacement for the useful parts of paid auto-apply services. It does not use their API, data, code, or branding.

## What it does

| Loop | What happens |
|---|---|
| **Discover** | Pulls jobs from Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Workday, USAJobs, and NYC Open Data public APIs; accepts a pasted job URL |
| **Decide** | Scores each job as `weighted skill coverage × hard-gate survival`, with reasons, gaps, and a per-requirement table |
| **Prepare** | Selects the most relevant verified bullets, reorders skills, optionally writes a grounded summary, renders a one-page Typst PDF, and checks the text layer |
| **Apply** | Opens a persistent Chromium window, fills the form, uploads the resume, screenshots the result, and **stops**. You review and click Submit |
| **Track** | Stores applications, answers, statuses, events, and (optionally) IMAP messages with verification codes and reply classification |
| **Warm path** | Reads a local `network.yml`, finds contacts at the employer, and recommends referral-first vs. cold channel without changing the stored fit score |
| **Answer bank** | `/answers` lets you edit reusable screening answers; overrides match question substrings and sensitive disclosures are never guessed |

## What it does not do

- No captcha solving. If a challenge appears, you solve it.
- Unattended submission is **off by default**. A single-application, capped, opt-in mode exists behind an explicit `I UNDERSTAND THE RISK` acknowledgement; see [Risk settings](#risk-settings).
- No bulk unattended submission, no Indeed or LinkedIn automation. Indeed and LinkedIn URLs are rejected at runtime before a browser opens.
- No employer account creation or password storage.
- No payments, referrals, affiliate, gifts, or enterprise multi-tenancy.
- No cloud account, telemetry, or npm build step.

## Install from GitHub (recommended)

Requires Python 3.11+ and Typst 0.14+.

**macOS**

```bash
# 1. Install uv (Python tool runner)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Install Typst
brew install typst

# 3. Install JobForge from GitHub
uv tool install --from git+https://github.com/Taf0711/jobforge jobforge

# 4. First-run setup: config, data dirs, browser
jobforge setup

# 5. Launch the dashboard
jobforge
```

**Linux**: install uv with the same command, install Typst from
<https://typst.app/docs/guides/installation/>, then run the same
`uv tool install` and `jobforge setup` commands. Playwright may ask for system
libraries on a minimal distro (`playwright install-deps chromium`).

**Windows**: install uv with
`powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`, install Typst with
`winget install Typst.Typst`, then run the same `uv tool install` and
`jobforge setup` commands from PowerShell.

If you are installing from a private fork, authenticate first (`gh auth login` and
`gh auth setup-git`) or use the SSH URL:

```bash
uv tool install --from git+ssh://git@github.com/Taf0711/jobforge.git jobforge
```

For a reproducible release, pin the tag:

```bash
uv tool install --from "git+https://github.com/Taf0711/jobforge@v0.4.1" jobforge
```

`jobforge setup` writes config, data, output, and browser state under a per-user
app home, not into the Python installation:

| Platform | Location |
|---|---|
| macOS | `~/Library/Application Support/JobForge` |
| Linux | `~/.local/share/jobforge` |
| Windows | `%APPDATA%\JobForge` |
| override | `JOBFORGE_HOME` |

Check the install with `jobforge doctor`. Update with
`uv tool install --force --from git+https://github.com/Taf0711/jobforge jobforge`.

## Quick start from a checkout

Requires Python 3.11+ and Typst 0.14+.

```bash
cd /path/to/jobforge
make bootstrap            # venv + deps + Chromium
cp config.example.yml config.yml
# edit config.yml: add board slugs, your resume path, preferences
.venv/bin/python -m jobforge init
.venv/bin/python -m jobforge import-profile --file /path/to/resume.pdf
.venv/bin/python -m jobforge scan
.venv/bin/python -m jobforge score
.venv/bin/python -m jobforge serve
```

Open <http://127.0.0.1:8765>. In a checkout, the launcher is also available:

```bash
uv tool install --from . jobforge
jobforge app
```

### First useful run

1. `import-profile` parses the resume into contact, experience, education, projects, skills, and a fact bank.
2. Add 5–20 target company board slugs to `config.yml`. Examples:
   - Greenhouse: `stripe`, `discord`, `figma`
   - Lever: `leverdemo`, `spotify`
   - Ashby: `linear`, `ramp`
   - Workable: `seedlegals`
   - SmartRecruiters: `Ubisoft`
3. `scan` fetches and deduplicates jobs by fingerprint.
4. `score` fills the match table. The dashboard's **Run scan** scores newly
   discovered jobs automatically; the CLI keeps `scan` and `score` separate.
5. Open a job → **Tailor resume** → **Fill application**.

### Already using resume-forge?

Import the verified profile, project bank, evidence matrix, network, and tracker
instead of re-parsing a PDF:

```bash
jobforge import-resume-forge \
  --profile  /path/to/profile.yml \
  --projects /path/to/projects.yml \
  --evidence /path/to/evidence_matrix.yml \
  --network  /path/to/network.yml \
  --archetypes /path/to/role_archetypes.yml \
  --applications /path/to/applications.yml \
  --resume   /path/to/base_resume.pdf
```

See `docs/16-resume-forge.md` for the mapping and status conversion.

## Commands

```bash
jobforge app              # start the dashboard and open it
jobforge setup            # first-run config + browser
jobforge doctor           # check paths, Typst, and browser
jobforge init
jobforge import-profile --file resume.pdf
jobforge import-resume-forge --profile profile.yml --projects projects.yml
jobforge scan
jobforge scan --source greenhouse --board stripe
jobforge scan --url https://jobs.lever.co/acme/123
jobforge score
jobforge jobs --min-score 60 --limit 20
jobforge tailor --job 42
jobforge apply --job 42
jobforge apply --job 42 --hold 600        # non-interactive: keep browser open 10 min
jobforge serve
jobforge email poll
jobforge code --company "Acme" --timeout 120   # redacted; add --reveal-code to print it
jobforge export
jobforge audit
python -m jobforge.audit.adapter_eval --all --json   # deterministic ATS fixture eval
python scripts/e2e_smoke.py --json                   # offline end-to-end smoke
python scripts/smoke_live_ats.py --json              # live, non-submitting ATS smoke
jobforge risk status                       # risky modes are off by default
jobforge queue build --limit 10            # ranked supervised queue
jobforge queue prepare
jobforge queue apply
jobforge status
```

`apply` opens a headed browser. Review the fields, solve any captcha yourself, and click Submit. Close the browser when done; the CLI records what was filled.

The `adapter_eval`, `e2e_smoke`, and `smoke_live_ats` gates are developer-only and require a source checkout. The live smoke fills the public Lever demo form with synthetic data and **never clicks Submit**.

Workday is supervised on purpose: if the page shows an account wall or password field and no application form, the adapter stops and hands the browser to you. It never creates an employer account or stores a password.

## Configuration

`config.yml` (gitignored) controls paths, sources, seeded preferences, LLM, IMAP, and the browser.

`scan.keywords` and `scan.exclude_keywords` gate job titles before insertion;
`scan.max_new` caps new rows per scan (deferred rows are picked up by later
scans). USAJobs is dormant until `USAJOBS_API_KEY` and `USAJOBS_EMAIL` are set.

Secrets are **environment variables only**:

```bash
export JOBFORGE_LLM_API_KEY=...        # optional, OpenAI-compatible endpoint
export JOBFORGE_LLM_BASE_URL=http://localhost:11434/v1
export JOBFORGE_LLM_MODEL=llama3.1:8b
export JOBFORGE_IMAP_PASSWORD=...      # optional, app password
```

The app works with no LLM: tailoring falls back to deterministic selection and a template cover letter. With a local Ollama endpoint, no profile data leaves the machine.

**Warm path:** copy `network.example.yml` to `network.yml` (gitignored) and list people you actually know. The job detail page then shows the warm score, the recommended channel (`referral_first`, `warm_intro`, `cold_with_warm_fallback`, `cold_only`), and a display-only fit adjustment. The stored match score is never overwritten.

**Answer bank:** edit overrides at `/answers` or in `data/answers.json`. Keys are case-insensitive substrings of the application question. Voluntary disclosures (veteran, disability, gender, race, orientation) are only filled from explicit profile values; otherwise the browser leaves them for you.

## Risk settings

All risky modes are off by default. `/settings/risk` and `jobforge risk status` show the exact warnings and the current usage.

```bash
jobforge risk enable --flag unattended_submit \
  --acknowledge "I UNDERSTAND THE RISK"
```

That enables a **single-application** unattended submit, capped at 10 per UTC day. The daily slot is reserved atomically before the browser opens. It still stops on a captcha. Captcha solving, bulk unattended submission, Indeed/LinkedIn automation, and employer account creation are deliberately unavailable. Full disclosure: `docs/13-risk-consent.md`.

## Queue and quality gate

The read-only `/queue` dashboard shows ranked items, preparation status, and quality notes. The CLI equivalents are `jobforge queue build`, `queue next`, `queue prepare`, and `queue apply`. The default apply path forces review mode and asks whether you submitted; `--auto` is only the risk-consented single-application path.

The quality gate is enforced wherever a browser can open: job detail disables Fill, the web apply route refuses a blocked job, the CLI runs a preflight, and the queue raises `QualityBlocked` before the browser starts. Blockers include below-threshold match, zero gate survival, duplicate-company, already-submitted status, missing resume, and a recorded captcha handoff. Details: `docs/12-quality.md`.

## How matching works

```
JD → requirements (must / nice) → skill coverage with partial credit
                                  × hard gates:
                                    years, degree, clearance, sponsorship,
                                    location, title level, salary floor
                                  × thin-JD penalty
= 0–100 fit score
```

- A direct skill is 1.0.
- An adjacent skill gets partial credit from `jobforge/matching/skill_graph.yml` (e.g. Go from C++ = 0.5).
- Unparseable prose requirements get neutral 0.6 credit; responsibilities are not counted.
- A clearance requirement is a near-knockout unless you are clearance-eligible (0.35 survival) or already cleared.
- A senior title against an entry-level target caps the gate at 0.6.
- A JD with no parseable skills is capped at half score and flagged.

Every score stores its reasons, gaps, and requirement table, so you can see why a job ranked where it did.

### Fit vs. Pursuit

- **Fit** = skill coverage × gate survival. It never moves for compensation or location convenience.
- **Pursuit** = Fit + NYC compensation-policy modifier + relocation cost-of-living modifier.

NYC uses a $130k target floor and a $105k exception floor; below $105k is a skip. A $105–130k role is only `exception_ok` when the channel is warm or callback odds clear 25%. Relocating to a high-COL metro with thin comp is penalized; a low-COL metro is rewarded. The match row stores both numbers and the comp verdict.

## Resume safety

- Tailoring selects and reorders **facts from your resume**. It does not invent employers, dates, metrics, or skills.
- If an LLM is enabled, its summary must pass a number-support check against the fact bank; otherwise the deterministic summary is used.
- The generated PDF is checked for one page, a text layer, contact details, section headers, images, and encoding damage.
- You still read the PDF before sending it. The check is a floor, not a guarantee.

## Project layout

```
jobforge/
  paths.py       source-checkout vs. installed app-home paths
  sources/       ATS discovery adapters + direct URL fetch
  matching/      resume parse, JD parse, skill graph, score
  tailoring/     fact selection, LLM grounding, Typst render, ATS check
  automation/    browser session, generic filler, ATS adapters, answer bank
  networking/    local referral graph, warm-path score, channel routing
  queue.py       supervised application queue (human submit by default)
  quality.py     pre-submit quality gate (score, blockers, warnings)
  risk.py        explicit opt-in risk policy, daily cap, consent record
  email/         IMAP poller, verification codes, reply classification, code bridge
  web/           FastAPI dashboard, answers editor, risk settings, Jinja templates
  audit/         parity and static security checks
  data/          packaged config example
pyproject.toml   installable package + `jobforge` entry point
scripts/         install.sh and offline e2e smoke
config.example.yml
network.example.yml
docs/
  03-architecture.md   module map, data flow, trust boundaries
  05-warm-path.md      referral graph and channel routing
  06-code-bridge.md    verification-code bridge
  07-answer-bank.md    reusable screening answers
  08-adapter-eval.md   deterministic ATS fixtures
  09-workday.md        Workday discovery and supervised adapter
  10-e2e.md            offline end-to-end smoke
  11-queue.md          supervised application queue
  12-quality.md        pre-submit quality gate
  13-risk-consent.md   explicit opt-in risk policy
  15-packaging.md      install, app home, release build
  16-resume-forge.md   import the personal verified profile/projects/tracker
  18-source-presets.md  reference direct-ATS board/scan preset
```

## Tests

```bash
make test        # pytest
make audit       # parity + security
make eval        # 6 fixture-driven ATS adapters
make e2e         # offline FastAPI + Typst smoke
```

The test suite covers DB CRUD/dedup/events, salary and HTML normalization, ATS detection, resume parsing, JD section parsing, skill extraction, partial credit, hard gates, title penalties, thin-JD caps, Typst render, blocking ATS checks, web page rendering, SSRF rejection, the security scan, warm-path routing, the verification-code bridge, the answers editor, Workday parsing/account-wall/pagination, the supervised queue, the quality gate, risk consent and the unattended-submit cap, re-tailor state preservation, grounded letters, upload targeting, auto-submit confirmation, packaging paths, the launcher, and the six-fixture adapter eval.

Deterministic gates:

```bash
make test    # pytest
make audit   # parity + static security
make eval    # offline ATS fixture eval; JSON; non-zero on drift
make e2e     # real FastAPI app + temp DB + Typst; JSON; non-zero on failure
make smoke   # live Lever demo fill; network; no submit; manual release gate
```

Dependencies are pinned in `requirements.lock` (regenerate with `make lock`).
GitHub Actions runs `test`, `audit`, `eval`, and `e2e` on every push and pull
request; the live smoke stays manual because it uses a public external form.

## Legal and platform notes

- Uses public ATS board APIs and public job pages only.
- Respects per-host rate limits with backoff.
- Does not call the paid service's authenticated API or copy its content.
- Automated form filling may still violate an employer's or ATS's terms. The human review/submit gate is deliberate; keep it.
- Never use this to misrepresent your identity or qualifications.

## License

MIT for the code in this repository. Third-party ATS data remains subject to the employer's terms. "Jobbie" is a trademark of its owner; this project is not affiliated with it.