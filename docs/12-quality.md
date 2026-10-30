# Quality gate

`jobforge/quality.py` answers one question before a browser opens: is this job worth applying to, and is the application ready? It reads SQLite and the filesystem only. It does not call an LLM, open a browser, or submit.

## Checks

| Check | Severity | Fails when |
|---|---|---|
| `match_score` | blocker | no match, or score < `prefs.min_match` |
| `match_gate` | blocker | no match, or gate survival ≤ 0 |
| `job_description` | warning | description shorter than 200 chars |
| `application_prepared` | blocker | no application row |
| `resume` | blocker | resume path empty or missing |
| `duplicate_company` | blocker/warning | another non-withdrawn application at the company |
| `application_status` | blocker | submitted, interview, or offer |
| `cover_letter` | warning | cover path empty or missing |
| `answers` | warning | no stored answers |
| `captcha` | blocker | `error_code == "captcha"` |

```text
score = max(0, 100 - 25 * failed_blockers - 5 * failed_warnings)
ready = failed_blockers == 0
```

Warnings lower the score but do not block.

## Integration

- Job detail renders the full report and disables Fill while blocked.
- `POST /jobs/{id}/apply` rechecks before spawning the browser.
- `jobforge apply` runs the preflight subset.
- `queue prepare` runs the full report after tailoring; `queue apply` runs preflight again.
- `services.apply_job` rejects a missing resume independently.

`evaluate(..., require_prepared=False)` skips `application_prepared`, `resume`, `cover_letter`, and `answers`. Direct apply uses that subset so it can start from a base resume.

The queue resolves the item’s recorded profile explicitly, so an active-profile switch does not change which match, application, resume, or answers are evaluated.

## Boundaries

A captcha is a handoff, not a failure to solve. A blocked item stays `prepared`; it is not silently marked failed or skipped. The gate never changes `matches.score`, auto-submits, or auto-advances.

The automated grounding checks in `tailoring/llm_resume.py` are a separate resume-content gate, not semantic proof.

Tests: `tests/test_quality.py`.
