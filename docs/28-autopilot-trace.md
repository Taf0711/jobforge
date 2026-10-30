# Autopilot trace and live monitoring

Autopilot runs in a detached process, so the dashboard and CLI read two local
files instead of scraping the terminal.

## Files

| File | Purpose |
|---|---|
| `data/autopilot_state.json` | Atomic snapshot: status, stage, current job, counters, stopped reason, pid, timestamps |
| `data/autopilot_trace.jsonl` | Append-only ordered events: run start, scan, score, queue, job start, tailored, applying, submitted/blocked, finish |
| `data/autopilot_last.json` | Final digest for the last completed pass |
| `data/autopilot.log` | Combined stdout/stderr from the detached process |

All are written `0600` where supported. No provider key or message content is
written to the trace.

## Stages

`starting → scanning → scoring → queueing → tailoring → applying → done`
or `blocked` / `failed`.

The snapshot is updated on every transition. The trace records both transitions
and per-job results.

## Run limit

`autopilot run --limit N` bounds the number of attempted jobs. `prepared` and
`submitted` are separate counters and both increment for a job that is prepared
and then submitted; the limit is not their sum. One job consumes one attempt.

## Live UI

`/autopilot` includes a **Live trace** panel. It polls
`/api/autopilot/status` every two seconds while the run is active, then stops.
It shows:

- running/last status and stage
- current job, company, ATS
- prepared/submitted/skipped/failed counters
- the last 60 trace events
- pid and last update time

The endpoint returns the state, trace, last digest, queue summary, and whether
the recorded pid is still alive.

## CLI

```bash
jobforge autopilot status          # state + trace + digest + risk
jobforge autopilot trace --limit 80
jobforge autopilot watch --interval 2
```

`watch` prints each new event until the run is no longer active. `status` and
`trace` are safe to run while the worker is running.

## Tests

- `test_autopilot_writes_state_and_trace` asserts the state reaches `done`,
  counters are correct, and `run_started`/`job_started`/`tailored`/`run_finished`
  events exist.
- `test_autopilot_status_api_and_page` asserts `/api/autopilot/status` and the
  live panel render.
- `test_read_trace_offset_reads_past_the_recent_window` proves `watch` can drain
  events beyond the newest-500 window.
- e2e includes `/api/autopilot/status`.
