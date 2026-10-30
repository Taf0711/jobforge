# Feature flags and scoring v2

JobForge uses local feature flags for features that need a safe rollback or an
A/B comparison. Flags are stored in `config.yml` and can be overridden by
environment variables. The Settings page exposes them under **Feature flags**.

## Flags

| Flag | Default | What it controls |
| `setup_wizard` | on | `/setup`, the first-run redirect, the setup readiness banner, and the Setup nav link |
| `github_connections` | on | `/connections`, GitHub repository sync, reviewed project import, and the Connections nav link |
| `scoring_v2` | on | Calibrated scoring: evidence-weighted skill credit, no positive credit for unparsed prose, and the parsing/gate fixes listed below |
| `agent` | on | `/agent`, `/providers`, the optional model link on setup's final page, and the nav entries. Turning it off keeps threads, provider metadata, and keys; it hides the surfaces |
| `agent_integrations` | on | Enables registered read-only `jobforge tools` connections. Setup still requires an explicit profile and connection; disabling it makes every external query fail closed |

`setup_wizard`, `github_connections`, and `agent` default on. `scoring_v2`
defaults on and applies the calibrated scorer. Turning a flag off hides or
disables that surface without deleting data. `agent` is independent of provider
setup: hiding the agent does not delete provider credentials or conversations.

## Configuration

```yaml
features:
  setup_wizard: true
  github_connections: true
  scoring_v2: true
  agent: true
  agent_integrations: true
```

Environment overrides take precedence:

```bash
export JOBFORGE_FEATURE_SETUP_WIZARD=0
export JOBFORGE_FEATURE_GITHUB_CONNECTIONS=0
export JOBFORGE_FEATURE_SCORING_V2=0
export JOBFORGE_FEATURE_AGENT=0
export JOBFORGE_FEATURE_AGENT_INTEGRATIONS=0
```

The Settings form writes the same `features` block atomically to `config.yml`
with `0600` permissions. Existing matches are not rewritten when a flag changes;
run **Score** (or `jobforge score`) to apply a scoring change. Provider keys are
not part of the feature block and are never written to `config.yml`.

## Scoring v2

`scoring_v2` is on by default. It fixes several scoring paths that could produce
misleading Fit scores:

- Requirement extraction recognizes common headings (`What We Look For`,
  `What You'll Bring`, `Minimum Requirements`, etc.), normalizes curly
  apostrophes and markdown emphasis, and excludes salary, benefits, job IDs,
  headcount, telecommuting, and legal boilerplate from requirements.
- Years, degree, GPA, clearance, citizenship, and ITAR gates read the
  requirement/nice-to-have sections instead of company bios. A company-age line
  such as "For 30+ years, we've delivered..." is not a candidate requirement.
- A years range uses its **floor**, not its ceiling: `2–12+ years` means `2+`.
- Title gating recognizes `Sr.`/`Snr` as senior titles.
- Remote detection no longer treats a company's "remote-first" blurb as a remote
  role. Role-level phrase detection and source flags still win. A remote role
  scoped to a non-US region (for example `Remote - India` or `Remote - Brazil`)
  is penalized for a US/Remote preference unless the candidate explicitly targets
  that location.
- Skill coverage applies the evidence map to direct skills: experience-backed
  skills are 1.0, project-backed skills are 0.8, and skills that exist only in the
  profile skill list are 0.4. Adjacent/family credit is multiplied by evidence as
  before.
- Unparseable prose gets **0.0** coverage credit under v2 and remains visible in
  the requirement table as `matched_by: unparsed`. The legacy neutral 0.6 credit
  is available by setting `scoring_v2: false`.
- The thin-JD cap now ignores skills found only in responsibilities/context;
  a JD with no parseable must/nice skill is capped and flagged.

### What changed numerically

The v2 calibration is stricter. On the local 553-job corpus, the old scores had
71 jobs at Fit >= 50 and 32 at Fit >= 65; v2 had 21 and 19 respectively. The
ranking changed materially because inflated prose credit and false 12/18/30-year
gates were removed. This is why the flag exists: turn it off to restore the
legacy calibration while comparing results.

### What v2 does not claim

- Unparsed prose is not proof of a skill gap; it is simply not counted as
  positive evidence.
- Remote scope uses a curated marker list, not a full country database.
- The human still reads the PDF and the job posting.

## Verification

- `tests/test_features.py` covers defaults, YAML/env overrides, route/nav gating,
  and config writes.
- `tests/test_scoring_v2.py` covers heading recognition, boilerplate exclusion,
  year-range floors, company-age exclusion, thin-JD context, `Sr.` titles,
  evidence/prose calibration, remote scope, and the philosophy-degree guard.
- The rest of the suite runs with v2 enabled. Set the environment flag to `0` to
  exercise the legacy path manually.
