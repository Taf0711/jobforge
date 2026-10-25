# Quality gate

`jobforge/quality.py` is a deterministic, local pre-submit gate. It answers one
question before a browser is opened:

> Is this job worth applying to, and is the application actually ready?

It reads only the SQLite database and the filesystem. It does not call an LLM,
does not open a browser, and does not submit anything.

## Public interface

```python
from jobforge.quality import (
    QualityCheck,    # name, passed, severity, detail
    QualityReport,   # score, ready, checks
    evaluate,        # evaluate(conn, job, profile, prefs, application=None)
    is_ready,        # is_ready(report) -> bool
)
```

`evaluate()` accepts an optional `Application`. When it is omitted, the gate
loads the application for `(job.id, profile.id)` from the database. The
`conn` argument is a standard `sqlite3.Connection` opened with `jobforge.db`.

## Checks and exact severities

| Check name | Severity | Fails when |
|---|---|---|
| `match_score` | `blocker` | no match exists, or `match.score < prefs.min_match` |
| `match_gate` | `blocker` | no match exists, or `match.gate_survival <= 0` |
| `job_description` | `warning` | stripped description is shorter than 200 characters |
| `application_prepared` | `blocker` | no application exists (`detail == "not prepared"`) |
| `resume` | `blocker` | `application.resume_path` is empty or is not a file on disk |
| `duplicate_company` | `blocker` or `warning` | another non-withdrawn application exists at the same company. Blocker when `prefs.one_application_per_company` is true, warning otherwise |
| `application_status` | `blocker` | status is `submitted`, `interview`, or `offer` |
| `cover_letter` | `warning` | `application.cover_letter_path` is empty or is not a file on disk |
| `answers` | `warning` | `application.answers` is empty |
| `captcha` | `blocker` | `application.error_code == "captcha"` |

Passing checks never contribute to the score. Company comparison is
case-insensitive after trimming, and the current application is excluded so it
does not count as its own duplicate. Withdrawn applications never count.

## Score and readiness

```text
score = max(0, 100 - 25 * failed_blockers - 5 * failed_warnings)
ready = failed_blockers == 0
```

`is_ready(report)` is the intended read helper and simply returns
`report.ready`. A warning lowers the score but does not block. Any blocker
makes `ready` false.

## Job detail integration

The web job-detail route evaluates the full report and passes
`quality_ready = is_ready(report)`. The "Fill application" button is disabled
while it is false, with the blocker names visible in the report below. The
`POST /jobs/{id}/apply` route re-evaluates before spawning the browser and
redirects with an `Apply blocked` message instead of launching a doomed child
process. The CLI `jobforge apply` runs the preflight subset (below) before the
browser opens.

The gate is advisory at the UI layer in one sense: the human still clicks
Submit. It is enforcing at the action layer: a blocked job cannot open the
fill browser from the UI, CLI, or queue.

## Preflight vs. full report

`evaluate(..., require_prepared=False)` runs the pre-browser subset:

| Included in preflight | Excluded from preflight (requires a prepared application) |
| `match_score`, `match_gate`, `job_description` | `application_prepared`, `resume` |
| `duplicate_company`, `application_status`, `captcha` | `cover_letter`, `answers` |

Direct `jobforge apply` uses preflight so it can start from a base resume.
`jobforge queue prepare` runs the full report after tailoring; `queue apply`
uses preflight again immediately before opening the browser. `services.apply_job`
still rejects a missing resume file independently.

## Queue integration

The queue evaluates before opening a browser and returns the failed checks
instead of proceeding:

```python
from jobforge.quality import evaluate, QualityBlocked

report = evaluate(conn, job, profile, prefs, require_prepared=False)
if not report.ready:
    raise QualityBlocked(report)   # .report and .blocking
```

`QualityBlocked.blocking` is the list of failed blocker checks. The CLI prints
each one. A blocked item stays `prepared`; it is not silently marked `failed`
and it is not silently skipped.

Do not use the quality score to change `matches.score`. The stored match remains
the ranking source of truth. Do not let the gate auto-submit, auto-advance, or
solve a captcha: a `captcha` blocker is a handoff to the human.

## Test command

```bash
cd $JOBFORGE_WORKTREE/wave3-quality && \
  $JOBFORGE_REPO/.venv/bin/python -m pytest tests/test_quality.py -q
```

Coverage includes the ready path, below-threshold match, zero gate survival,
missing application and resume, duplicate-company blocker vs. warning,
re-apply statuses, missing cover letter/answers warnings, captcha blocker, the
score arithmetic, and the preflight subset (`require_prepared=False`). The queue
and web tests additionally prove that a blocked job does not launch the apply
subprocess.
