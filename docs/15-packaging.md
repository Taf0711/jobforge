# Packaging and installation

JobForge is an installable Python package with a `jobforge` console entry point.
It is not a single-file binary: Typst and a browser remain external, by design.

## Install from a checkout

```bash
uv tool install --from /path/to/jobforge jobforge
jobforge setup
jobforge
```

`jobforge setup` creates the per-user app home, copies `config.yml` from the
packaged example, initializes SQLite, checks Typst, and installs Playwright
Chromium. If Chromium cannot be installed, the browser layer falls back to the
system Google Chrome installation.

## Install from GitHub

```bash
uv tool install --from git+https://github.com/Taf0711/jobforge jobforge
jobforge setup
jobforge
```

Requires Python 3.11+ and Typst 0.14+. `uv` installs Python itself if needed.

## App home

Installed wheels never write into site-packages:

| Platform | Default |
| macOS | `~/Library/Application Support/JobForge` |
| Linux | `~/.local/share/jobforge` |
| Windows | `%APPDATA%\JobForge` |
| override | `JOBFORGE_HOME` |

The app home contains `config.yml`, `data/` (SQLite, queue, risk policy,
screenshots, browser profile), `output/` (tailored resumes), and `network.yml`.
A source checkout keeps the historical repo-root layout for development.

## Commands

```bash
jobforge app                # start the dashboard and open the browser
jobforge app --no-browser   # start without opening a browser
jobforge setup              # first-run setup (idempotent)
jobforge setup --skip-browser
jobforge doctor             # print paths and dependency status
jobforge serve              # server only, no browser
```

## Build artifacts

```bash
uv build
# dist/jobforge-0.1.0-py3-none-any.whl
# dist/jobforge-0.1.0.tar.gz
```

The wheel packages:

- `jobforge/matching/skill_graph.yml`
- `jobforge/tailoring/template.typ`
- `jobforge/web/templates/*.html`
- `jobforge/web/static/*`
- `jobforge/data/config.example.yml`

The sdist additionally includes the README, license, tests, and example configs.

## What is deliberately not bundled

- **Typst** — install via Homebrew, winget, or the upstream release. `jobforge
  setup` detects it and prints the right command.
- **Playwright Chromium** — installed by `jobforge setup`; the browser layer
  falls back to system Chrome if the bundled browser is absent.
- **A Python runtime** — `uv tool` manages an isolated venv. A standalone
  PyInstaller build is possible later but adds 300–500 MB and macOS
  signing/notarization without improving the supervised-browser workflow.

## Public distribution checklist

Before publishing a repository:

1. Keep `config.yml`, `network.yml`, `data/`, `output/`, and `.env` untracked.
2. Do not commit SQLite files, screenshots, tailored resumes, or browser profiles.
3. Keep internal worktrees, review artifacts, and absolute local paths out of the
   public tree (or sanitize them).
4. Run `pytest`, `jobforge audit`, the adapter eval, and the e2e smoke.
5. Rebuild the wheel and test `uv tool install --from <repo> jobforge` in a clean
   tool directory before sharing the link.
