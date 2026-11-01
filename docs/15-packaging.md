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
screenshots, browser profile), `output/` (profile-scoped tailored resumes under
`output/profile-<profile-id>/`), and `network.yml`.
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
# dist/jobforge-0.5.3-py3-none-any.whl
# dist/jobforge-0.5.3.tar.gz
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

## Public release process (maintainer)

The public repo is `Taf0711/jobforge`; the private development repo is
`Taf0711/jobforge-dev`. Do not push internal plans, review artifacts, worktree
notes, or the third-party teardown to the public repo.

```bash
# 1. Export a clean tree (excludes internal docs and rewrites local paths)
scripts/export_public.sh /tmp/jobforge-public

# 2. Verify the export
cd /tmp/jobforge-public
.venv/bin/python -m pytest -q   # use the dev venv if needed
uv build
unzip -l dist/*.whl | grep 'jobforge/data'

# 3. Publish it
git add -A && git commit -m "Release vX.Y.Z"
git push origin main
git tag -a vX.Y.Z -m "JobForge X.Y.Z"
git push origin vX.Y.Z
gh release create vX.Y.Z dist/*.whl dist/*.tar.gz
```

Keep the private repo as the source of truth; the public repo is a release
export.

## npm installer release

The `npx jobforge` wizard is the separate npm package in `installer/`. It is
published independently from the Python wheel.

The first publish is a one-time manual bootstrap. npm Trusted Publishing
cannot create a brand-new package, and bypass-2FA tokens are not used here.

```bash
cd installer
npm publish --access public
# enter the 6-digit npm 2FA code when prompted
```

After the first version exists, configure trusted publishing once on npm:

```bash
npm trust github jobforge \
  --file publish-npm.yml \
  --repo Taf0711/jobforge \
  --allow-publish \
  --yes
npm trust list jobforge
```

Or use the UI: npmjs.com → `jobforge` → **Settings → Trusted Publisher →
GitHub Actions** with user `Taf0711`, repository `jobforge`, workflow
`publish-npm.yml`, environment blank, and `npm publish` allowed.

Then `.github/workflows/publish-npm.yml` publishes future `v*` tags with GitHub
OIDC. It uses `id-token: write`, requires npm 11.15+, and needs **no
`NPM_TOKEN`**. The trusted publisher config must complete a successful publish
within two days or it expires.

The npm installer and the Python package must carry the same version. The
installer pins its default runtime spec to `git+...@v${VERSION}`, so
`jobforge@0.5.3` installs the `v0.5.3` Python tag rather than floating on
`main`.

`installer/package.json` and `pyproject.toml` versions must both equal the tag
without the leading `v`, and `installer/package.json` `repository.url` must
remain `git+https://github.com/Taf0711/jobforge.git`.

### Runtime updates

`npx jobforge` checks the installed uv-managed runtime version before applying
the install plan. If it is older than the npm installer, the wizard offers to
run:

```bash
uv tool install --force --from git+https://github.com/Taf0711/jobforge@vX.Y.Z jobforge
```

`--update` forces the update and `--no-update` skips it. Local checkouts,
`JOBFORGE_BIN`, and `JOBFORGE_PYTHON` are never replaced automatically. Set
`JOBFORGE_INSTALL_SPEC` to override the install source.

Manual local publish after bootstrap:

```bash
scripts/publish_npm.sh --otp 123456
```
