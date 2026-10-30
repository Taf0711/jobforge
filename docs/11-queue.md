# Supervised queue

`jobforge/queue.py` ranks scored jobs, prepares one resume at a time, and opens one application for human review. It adds no new browser or submit path.

## Safety

- Review mode is forced even if `browser.allow_submit` is set.
- A submission is recorded only when `apply_job` reports it or a human confirms it.
- A captcha leaves the item `prepared`; the human solves it and submits.
- No Indeed/LinkedIn, account creation, or password storage.
- Queue file writes are atomic and `0600`.

## Profile binding

Each item stores the `profile_id` captured at build time. Prepare, apply, and autopilot pass that profile explicitly to the service layer. Switching the active profile does not retarget queued work. A missing profile fails the item.

Artifacts are written under `output/profile-<profile-id>/...`.

## Statuses

| Status | Meaning |
|---|---|
| `pending` | Queued, not tailored |
| `prepared` | PDF exists, waiting for review |
| `submitted` | Service-reported or human-confirmed submission |
| `skipped` | Human declined |
| `failed` | Prepare or apply failed; `error` has the reason |

Items run in queue order (score descending at build time). There is no auto-advance loop.

## API

```python
from jobforge.queue import Queue

queue = Queue(config.data_dir / "queue.json")
queue.build(conn, profile, prefs, limit=10, min_score=None)
queue.list_items(status=None)
queue.next_item()
queue.summary()

queue.prepare_next(config, conn)
queue.apply_next(config, conn, hold_seconds=600, confirm=None)
queue.mark_prepared(job_id, "/path/resume.pdf")
queue.mark_submitted(job_id)
queue.mark_skipped(job_id)
queue.mark_failed(job_id, "error")
```

`build` excludes existing applications, existing queue items, and (when enabled) extra jobs at the same company. `min_score=None` disables the threshold.

`prepare_next` tailors the first pending item for its recorded profile and records quality blockers in `note`.

`apply_next` runs the quality preflight, forces review mode, and:

1. reports `submitted=True` → `submitted`, syncs the application row;
2. human confirms → `submitted`, syncs the application row;
3. human declines → `prepared`;
4. hold without confirm → `prepared`, submission not inferred.

## CLI

```bash
jobforge queue build --limit 10
jobforge queue list --status pending
jobforge queue prepare
jobforge queue apply --hold 600 --confirm
jobforge queue apply --auto
```

`--auto` is the risk-consented single-application path; it still stops at captcha or a quality blocker.

Quality blocking is visible in the CLI and queue notes. `queue apply` refuses a blocked item before opening a browser.
