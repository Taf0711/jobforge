# GitHub connections and project context

JobForge can connect to a GitHub account to turn repositories into **reviewable
resume project drafts**. The connection is local-first: the token goes only to
`api.github.com`, repository context is stored in the local SQLite database, and
nothing enters the resume until the user submits the project form.

This is not an OAuth social login. It uses a personal access token so the app
does not need to bundle a client secret or run a callback server. The connection
table is provider-generic, so additional read-only connections can use the same
shape later.

## User flow

1. Open **Connections** in the dashboard.
2. Paste a GitHub token, or click **Use GITHUB_TOKEN from the environment** when
   the variable is set.
3. JobForge validates the token with `GET /user`, then syncs up to 300
   owner repositories through `GET /user/repos`.
4. Open a repository. JobForge fetches its language breakdown and README once,
   then suggests a tech line, tags, and a few bullets from an overview/features
   section.
5. Edit the project name, tech line, bullets, and tags. No field is committed
   until **Add to project bank** is clicked.
6. When confirmed, JobForge updates the active profile's project bank and fact
   bank, optionally adds detected canonical skills, and rescores jobs.
7. The normal resume renderer selects the three most JD-relevant live projects.
   The GitHub source label does not change selection or truthfulness rules.

## Token creation

Use a **fine-grained personal access token** where possible:

- Repository access: public repositories only, or select the private
  repositories you want to use.
- Repository permissions: **Contents: read** (README files) and **Metadata:
  read** (repository list; usually implicit).
- No write permissions, no organization administration, no user administration.

A classic token with `public_repo` works for public repositories. A classic
token with `repo` also exposes private repositories; use it only if needed.

The token is sent in an `Authorization: Bearer` header to the fixed API root. It
is never placed in a URL, a Jinja template, an export, or an error message.

## Storage

A token pasted into the dashboard is stored in the OS keychain when a keychain
backend is available. If no backend is available, JobForge writes
`data/connections/github.token` atomically with mode `0600` and tells the user in
the success message. Use FileVault/BitLocker/full-disk encryption as the final
backstop.

The token can instead be supplied through the environment:

```bash
export GITHUB_TOKEN=github_pat_...
# or a JobForge-specific variable
export JOBFORGE_GITHUB_TOKEN=github_pat_...
```

An environment token is not copied to the keychain or the file. The connection
record stores only its source (`env`). `JOBFORGE_DISABLE_KEYRING=1` forces the
0600 fallback for headless smoke runs.

## What is stored

| Table | Contents |
| `connections` | provider, account id/login/name, reported scopes, token backend, timestamps |
| `github_repos` | repo id/full name, description, languages, topics, stars, fork/private/archived flags, last push, README excerpt (64 KB cap), suggested tech line/tags/bullets |
| `profiles.data_json.projects` | only projects the user explicitly imported |
| `profiles.data_json.facts` | project bullets as fact-bank entries with `source_id = github-<repo_id>` |

The repository cache is for display and review. Disconnecting removes the token,
the connection row, and cached repository metadata. Projects already imported
into the profile stay there; the user can keep or edit them after reconnecting.

## Security controls

- **Fixed destination.** All requests go to `https://api.github.com`; there is no
  user-supplied URL or redirect following. `owner` and repository names are
  validated before path construction.
- **Bounded responses.** API responses are capped at 2 MB; READMEs at 64 KB;
  sync is capped at 300 repositories and three pages.
- **Read-only scope guidance.** The UI asks for read-only token access. The code
  never calls a write endpoint.
- **No token output.** Tokens are not returned in JSON, rendered in HTML,
  written to the database, or included in exception text.
- **Keychain first.** OS keychain integration is through `keyring`; file fallback
  is atomic, 0600, and disclosed.
- **Untrusted README.** README text is rendered escaped in a `<pre>` block. Raw
  README text is never sent to an LLM, never interpreted as instructions, and
  never inserted into a resume automatically. Approved project bullets become
  ordinary fact-bank entries; if the optional cover-letter LLM is enabled, they
  are sent only as part of that same approved fact bank.
- **Human review gate.** Suggested bullets are drafts from the repository owner's
  README. The project form is the approval boundary; the fact bank is only
  updated after that form is submitted.
- **Rate limits.** GitHub 401/403/429 responses become user-readable errors; the
  app does not retry in a loop. Sync is manual.
- **CSRF and access token.** Existing Origin checks and the optional
  `server.access_token` gate cover the connection routes like every other write.

## GitHub job lists are a different feature

The **Connections → GitHub** flow imports your own repositories as resume
projects. It does not add jobs. To use public job-list repos such as SimplifyJobs,
configure `sources.github_jobs.repos` or choose the **Community GitHub job
lists** setup preset; see `docs/22-github-job-lists.md`.

## Feature flag

The whole surface is gated by `features.github_connections` (default on). Turn
it off in **Settings → Feature flags**, `config.yml`, or
`JOBFORGE_FEATURE_GITHUB_CONNECTIONS=0` to hide the page, routes, and project-edit
links. Existing connection data and imported projects are kept.

## Limitations

- Only repositories owned by the authenticated account are listed. Organization
  and collaborator repositories are not included yet.
- README suggestions only pull from recognized overview/features sections and
  skip install/setup/usage sections. Repositories with other README structures
  get an empty bullet draft; the user can type their own.
- Project editing is available from the GitHub review page while connected.
- There is no background sync, no webhook, and no write-back to GitHub.
- A private repository's metadata and README remain in the local DB until the
  user disconnects or deletes the data; full-disk encryption is the at-rest
  control.

## Tests

`tests/test_github_connection.py` covers:

- keychain-disabled 0600 fallback, permissions, delete, and environment tokens;
- README cleanup around literal angle brackets, multiline list continuation,
  and installation-section exclusion;
- repo normalization, project import/update/removal, fact replacement, and
  detected-skill updates;
- the full web flow with a fake GitHub API: connect, sync, browse, review,
  import, profile display, remove, and disconnect;
- the token never appearing in the SQLite file, the rendered page, or the
  data export.

The live sync check is manual and uses the account's own token; it is not part of
CI.
