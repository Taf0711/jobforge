# Workday discovery and supervised apply

JobForge reads public Workday postings from the tenant's CXS search endpoint
and fills a Workday application only up to the account wall. Workday remains a
human-authenticated, human-submitted ATS.

## Board identifiers

`parse_board(board)` returns `(tenant, wd, site)`.

| Input | Result |
|---|---|
| `acme|wd1|External` | `("acme", "wd1", "External")` |
| `acme|External` | `("acme", "wd1", "External")` (defaults to `wd1`) |
| `https://acme.wd1.myworkdayjobs.com/en-US/External` | `("acme", "wd1", "External")` |
| `https://acme.wd1.myworkdayjobs.com/External` | `("acme", "wd1", "External")` |

Tenant and data-center components are lower-cased; the site path segment keeps
its case because Workday paths are case-sensitive. Any other shape raises
`ValueError` with the accepted forms. The URL form only accepts hosts of the
shape `<tenant>.<wdN>.myworkdayjobs.com`.

## Discovery endpoint

```
POST https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
Content-Type: application/json
User-Agent: JobForge/0.1 (+local job search; contact: local-user)

{"appliedFacets": {}, "limit": page_size, "offset": offset, "searchText": ""}
```

The source uses `requests.post` directly with a 30-second timeout and two
retries on transport errors, HTTP 429, and 5xx responses. Tests monkeypatch
`requests.post` and `http_get_json`; they never touch the network.

### Pagination

`WorkdaySource.fetch(board, *, max_jobs=100, page_size=20, include_details=True)`
walks the public CXS list:

1. POST the first page at `offset=0`.
2. Advance `offset` by the number of postings returned.
3. Keep going while `offset < total` (when the response includes `total`) and
   fewer than `max_jobs` postings have been collected.
4. Stop early on an empty page, a short page (`len(jobPostings) < page_size`),
   a page that adds zero new unique postings (tenants that ignore `offset`), or
   when `max_jobs` is reached.
5. Drop duplicate `externalPath` values across pages before applying the
   `max_jobs` cap.

`page_size` controls both the request `limit` and the short-page stop test. The
default of 20 matches Workday's common public page size; `max_jobs=100` is the
per-board discovery budget used by the scan.

The response's `jobPostings` list is mapped as follows:

| Workday field | Job field |
|---|---|
| `title` | `title` |
| `externalPath` | `source_id` |
| `locationsText` | `location` |
| `postedOn` | `posted_at` when it is an absolute date |
| `bulletFields` | retained in `raw` |

`apply_url` and `url` are both:

```
https://{tenant}.{wd}.myworkdayjobs.com/en-US/{site}{externalPath}
```

`company` is the title-cased tenant, and `remote` uses the normal
location-text heuristic.

`postedOn` is often a relative label such as `Posted 3 Days Ago`. Those values
are stored as `None` rather than guessed; ISO dates, `MM/DD/YYYY`, and common
month-name dates are normalized to ISO-8601.

## Detail descriptions

When `include_details=True` (the default), each collected posting gets a second
request for its description:

```
GET https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{externalPath}
Accept: application/json
```

This uses the shared `http_get_json` helper, so it reuses the per-host rate
limiter and the 5 MB response cap. The source reads the first non-empty string
from:

1. `jobPostingInfo.jobDescription`
2. `jobPostingInfo.externalDescription`
3. top-level `jobDescription`
4. top-level `description`

The value is converted from HTML to text with `html_to_text` and stored in
`Job.description`.

Detail failures are non-fatal: if a detail request raises `SourceError`, the
list-derived `Job` is kept with an empty description and the rest of the board
is returned. A detail request is never made when `include_details=False`.

Detail response shapes vary by tenant and Workday version. Some boards expose
only `jobPostingInfo.externalDescription`, omit descriptions from the CXS
detail payload, or return HTML that does not map cleanly to a single text block.
Those postings remain usable with an empty description rather than failing the
scan.

## Rate and size limits

The CXS list POST uses the same per-host rate limiter as the other sources,
streams the response with a 5 MB cap, closes every response on success and
failure, and honors numeric or HTTP-date `Retry-After` values on HTTP 429.
Detail GETs inherit the limiter and byte cap from `http_get_json`. A detail
failure (including an HTTP-date `Retry-After` or another non-`SourceError`
parse error) is non-fatal: the list-derived job is kept with an empty
description. Pagination is bounded by `max_jobs` and the zero-new-page stop, so
a board cannot issue unbounded list requests.

## Supervised adapter

`WorkdayAdapter` matches `myworkdayjobs.com` and `myworkdaysite.com`.

1. Strong account-wall copy is `Create Account` or `Already have an account`.
   A bare `Sign In` link is navigation chrome and is not sufficient on its own.
2. A visible password input always counts as a wall, even when a file input is
   present. Strong copy counts only when there is no file input and fewer than
   two visible form fields.
3. When a wall is detected, it returns a `FillReport` with `ats="workday"`, no
   filled fields, no resume upload, and the note
   `Workday account wall: human must authenticate`.
4. Otherwise it delegates to the generic `ATSAdapter.fill` path with
   `ats="workday"`. Common factual fields are filled and the resume is
   uploaded by the existing generic filler.

A file input or a multi-field first step means the application form is
reachable, so sign-in/registration header links do not block filling. Password
inputs are always skipped by the generic filler; this adapter never creates an
account, never types or stores a password, never solves a captcha, and never
clicks Submit. The browser stays open for the human to authenticate, review,
and submit.

## Intentionally unsupported

- Workday account creation.
- Password entry or storage.
- Captcha solving or stealth patches.
- Automatic submission.
- Reading data behind authentication.
- Multi-page Workday application wizards; the generic filler handles one page
  and reports unresolved fields.

## Integration

Parent-owned wiring (do not edit these files from this lane):

1. `jobforge/sources/registry.py`:

   ```python
   from .workday import WorkdaySource

   SOURCES = {
       # ...
       "workday": WorkdaySource(),
   }
   ```

2. `jobforge/automation/base.py` in `get_adapter`:

   ```python
   from .workday import WorkdayAdapter

   for adapter in (
       LeverAdapter(),
       GreenhouseAdapter(),
       AshbyAdapter(),
       WorkableAdapter(),
       WorkdayAdapter(),
   ):
       # ...
   ```

3. `config.example.yml` / user `config.yml`:

   ```yaml
   sources:
     workday:
       boards:
         - "acme|wd1|External"
         - "https://initech.wd5.myworkdayjobs.com/en-US/Careers"
   ```

`jobforge/sources/direct.py` already detects both Workday hosts, so pasted
Workday job URLs retain `ats="workday"`.

## Tests

```bash
cd $JOBFORGE_WORKTREE/lane-workday
$JOBFORGE_REPO/.venv/bin/python -m pytest tests/test_workday.py -q
```

Result: `17 passed`. Discovery tests use a fixture and a patched
`requests.post`; adapter tests use Playwright against inline HTML and skip
cleanly if Chromium is unavailable.
