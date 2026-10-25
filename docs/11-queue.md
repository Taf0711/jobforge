# Supervised application queue

`jobforge/queue.py` is a local JSON queue that ranks scored jobs, prepares one
tailored resume at a time, and opens one application for human review. It is a
coordination layer over `services.tailor_job` and `services.apply_job`; it does
not add a second browser path or a new submission mechanism.

## Safety contract

- **The human submits.** `apply_next` runs the service layer in review mode even
  if `browser.allow_submit` is accidentally set. It passes a config copy with
  `allow_submit=False`.
- **No inferred submissions.** Holding the browser open, returning from the
  apply call, or leaving the item untouched never changes the status to
  `submitted`. Only `apply_job(...)["submitted"] is True` or an explicit human
  confirmation (`confirm()` returning truthy) records a submission.
- **No captcha bypass.** If the service reports a captcha, the application stays
  `prepared`. The human solves it and clicks Submit.
- **No Indeed/LinkedIn automation.** The queue works on jobs already discovered
  by the public-ATS sources. It adds no source or adapter for those sites.
- **No account creation or password storage.** The queue stores no credentials.
- **Local file only.** The queue file is written atomically (same-directory
  temp file, `fsync`, `os.replace`) with mode `0600`.

## Statuses

| Status | Meaning |
|---|---|
| `pending` | Queued, not yet tailored. `prepare_next` takes the first one. |
| `prepared` | Tailored PDF exists and the item is waiting for human review. `apply_next` takes the first one. |
| `submitted` | The service reported a submit or the human confirmed it. |
| `skipped` | The human decided not to apply. |
| `failed` | Tailoring, the browser flow, or the apply service raised; `error` has the message. |

Items are processed in queue order (score descending at build time). The queue
does not have an auto-advance loop; the caller chooses when to prepare or apply
the next item.

## Public interface

```python
from jobforge.queue import Queue, QueueItem

queue = Queue(config.data_dir / "queue.json")

added: list[QueueItem] = queue.build(
    conn, profile, prefs, limit=10, min_score=None
)
items: list[QueueItem] = queue.list_items(status=None)
nxt: QueueItem | None = queue.next_item()          # first pending
counts: dict[str, int] = queue.summary()

prepared: QueueItem = queue.prepare_next(config, conn)
applied: QueueItem = queue.apply_next(
    config, conn, hold_seconds=600, confirm=None
)

queue.mark_prepared(job_id, "/path/to/resume.pdf")
queue.mark_submitted(job_id)
queue.mark_skipped(job_id)
queue.mark_failed(job_id, "error text")
```

### `QueueItem`

Required fields are `job_id`, `profile_id`, `status`, `score`, `company`,
`title`, `resume_path`, `error`, `created_at`, and `updated_at`. The dataclass
also carries an optional `note` string for human-review outcomes such as
"browser held open for 600s; submission not inferred". `status` must be one of
the five statuses above; loading an unknown status raises `ValueError`.

### `Queue.build(conn, profile, prefs, limit=10, min_score=None)`

- Calls `db.list_jobs(..., profile_id=profile.id, min_score=min_score,
  one_per_company=prefs.one_application_per_company)` and walks pages until
  `limit` items are added or the result set is exhausted.
- Ranking comes from `db.list_jobs`: score descending, then recency.
- Excludes any row with an `application_id` (an application already exists) and
  any job already in the queue in any status.
- When `prefs.one_application_per_company` is true, queues at most one job per
  company. The database filter still handles companies that already have an
  application.
- `min_score=None` means no score threshold. A CLI that wants the saved
  preference should pass `prefs.min_match` explicitly.
- Appends `pending` items and returns only the newly added items. A second build
  with the same inputs returns `[]` and does not rewrite the file.
- Raises `ValueError` for an unsaved profile (`profile.id is None`).

### `Queue.prepare_next(config, conn)`

Takes the first `pending` item and calls
`services.tailor_job(config, conn, job_id)`. On success it marks the item
`prepared` with the returned `pdf` path, then evaluates the full quality report.
If a blocker remains, the item stays `prepared` and `note` records the blockers;
the failure is not hidden and `apply_next` will refuse to open a browser. A
missing PDF path or an exception marks the item `failed` with the error and
returns that item. It does not raise the service exception. Raises `LookupError`
when nothing is pending.

### `Queue.apply_next(config, conn, *, hold_seconds=0, confirm=None, auto_submit=False)`

Takes the first `prepared` item, runs the quality preflight (match score, gate
survival, duplicate company, already-submitted status, captcha), and raises
`quality.QualityBlocked` if any blocker fails. The browser is never opened for a
blocked item. It then calls the service layer with review mode enforced:

1. `apply_job` reports `submitted=True` → status `submitted`, note
   "submission reported by apply_job".
2. `submitted=False` and `confirm` is callable → call `confirm()`. Truthy marks
   `submitted` with "human confirmed submission"; falsy leaves it `prepared`
   with "human reported no submission".
3. `submitted=False`, no `confirm`, and `hold_seconds > 0` → stays `prepared`
   with a note that the browser was held open and submission was not inferred.
4. `submitted=False`, no `confirm`, `hold_seconds == 0` → stays `prepared`.

An exception from `apply_job` marks the item `failed` with the error and returns
it. A malformed report also marks it `failed`. Raises `LookupError` when nothing
is prepared. A confirmation-prompt exception keeps the item `prepared` and
records the prompt error in `note`.

### `summary()`

Returns:

```json
{"total": 4, "pending": 2, "prepared": 1, "submitted": 1, "skipped": 0, "failed": 0}
```

The keys are stable for dashboard and CLI display.

## Queue file

`Queue(path)` loads the file on construction and writes on every mutation.
Default integration path: `config.data_dir / "queue.json"` (`data/queue.json`).

```json
{
  "version": 1,
  "items": [
    {
      "job_id": 42,
      "profile_id": 1,
      "status": "pending",
      "score": 87.5,
      "company": "Acme",
      "title": "Backend Engineer",
      "resume_path": "",
      "error": "",
      "created_at": "2026-10-24T12:00:00+00:00",
      "updated_at": "2026-10-24T12:00:00+00:00",
      "note": ""
    }
  ]
}
```

A missing file is an empty queue. Malformed JSON or an unknown status raises
instead of being silently replaced. The loader also accepts a bare item list for
manual recovery, but new writes always use the object form above.

## CLI integration contract

The shipped CLI surface is:

```bash
python -m jobforge queue build [--limit 10] [--min-score 60]
python -m jobforge queue list [--status pending]
python -m jobforge queue summary
python -m jobforge queue next
python -m jobforge queue prepare
python -m jobforge queue apply [--hold 600] [--confirm] [--auto]
python -m jobforge queue skip --job 42
```

- `next` prints the next actionable item (prepared first, then pending) without
  changing state.
- `prepare` tailors the first pending item and records any quality blockers in
  `note`.
- `apply` opens the first prepared item. Without `--auto`, the service runs in
  forced review mode. The human is asked `Did you submit this application?`
  when `--hold` is `0` or `--confirm` is passed; `--confirm` is the documented
  way to ask after a long hold. A non-interactive hold without `--confirm`
  leaves the item `prepared` and records "submission not inferred".
- `apply --auto` is the risk-consented single-application path. It requires the
  persisted policy and an available daily slot; the slot is reserved atomically
  before the browser opens. A captcha still stops the run.

Quality blocking is visible in two places: `jobforge apply` prints the failed
blockers and exits non-zero before opening a browser, and the queue raises
`QualityBlocked` with the same list. `jobforge queue apply` prints each blocker
on its own line.

## Test command and result

```bash
cd $JOBFORGE_WORKTREE/wave3-queue && \
  $JOBFORGE_REPO/.venv/bin/python -m pytest tests/test_queue.py -q
```

Result: `14 passed, 7 warnings` (about 0.5s; timing varies between runs). The
warnings are PyMuPDF/SWIG
deprecation warnings from the test environment, not queue failures.

Coverage: ranked build, one-per-company filtering, existing-application
exclusion, queue dedupe, atomic `0600` persistence, prepare success/failure,
quality-note on prepare, service-reported submission, malformed `submitted`
rejection, forced review mode, quality-blocked apply, confirm true/false, hold
without confirm, apply exception, skip/fail transitions, summary, empty queue,
and invalid statuses. Every test uses a temporary `Config`/SQLite database and
monkeypatches `services.tailor_job` / `services.apply_job`; no browser, network,
Typst, captcha, or real ATS is involved.
