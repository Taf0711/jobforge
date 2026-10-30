# Workflow details

## Status first

`jobforge_status` returns the bound profile, connection scopes, app and interface
versions, and local readiness. Check it before any multi-step answer. Hosts may
prefix the tool name with the MCP server name; match the `jobforge_` suffix.

## Search and compare

- Start with `limit` 5–10. Increase only when the user asks for more.
- Keep the returned `dataset_revision` and `next_cursor` together.
- Sort order is stored fit descending with unscored jobs last, then job ID
  descending.
- A null fit means unscored, not zero.
- `jobforge_get_job` returns sanitized public URLs. Do not reconstruct a URL
  from an ID or fetch an arbitrary URL.

## Explain fit

Report:

1. stored fit, coverage, and gate survival;
2. reasons and gaps from the stored match;
3. requirement rows, preserving `matched_by` and `gap`;
4. model version and scored timestamp when present.

Do not turn a gap into a claim about the candidate. Do not add skills from the
job description to the candidate's evidence.

## Readiness

`jobforge_get_readiness` is a pre-browser quality snapshot. Report every blocker
and warning verbatim after redaction. `resume_state` may be `not_prepared`,
`missing`, or `attached`; `review_state` is not tracked in the read-only phase.
The next action is the returned factual next step, not permission to submit.

## Profile

`jobforge_get_profile` sections:

- `overview`: name, headline, location, and counts;
- `skills`: bounded skill list;
- `experience`: title, company, dates, location, and resume bullets;
- `projects`: title, company, technology, URL, and resume bullets.

Contact fields, disclosures, work authorization, private facts, and provider
context are intentionally absent. Keep any `[redacted email]` or
`[redacted phone]` markers.

## Applications

`jobforge_list_applications` returns tracker summaries only. It does not return
answers, notes, resume or cover-letter paths, screenshots, or email bodies. Use
`jobforge_get_readiness` for a job-specific readiness check.

## Final answer shape

For a shortlist, use:

```text
Company — Title
Fit: stored value or unscored
Why: one or two stored reasons
Gaps: stored gaps
Next: factual human action
```

Never state or imply that JobForge submitted an application.


## Memory

Call `jobforge_recall` after status when durable preferences could change the
answer. Save only a stable preference the user explicitly asks to retain. Do not
store contact details, secrets, answers, or temporary task state. Memory is
profile-scoped and survives new host sessions that use the same JobForge profile.

## Resume request and approval

1. Call `jobforge_get_readiness` for the job if you have not already.
2. Call `jobforge_request_resume` with a stable `idempotency_key`.
3. Show the returned `approval_path`; the human opens the JobForge dashboard.
4. Poll `jobforge_get_operation` at a human pace. Do not claim success while the
   state is `pending_approval`, `approved`, or `running`.
5. When `succeeded`, share the artifact URL and any warnings. When `failed`,
   report the safe error and the approval path; do not retry with a new key
   unless the human asks.
6. Asking to request a resume is not permission to submit an application.

## Dashboard

`jobforge_dashboard` returns the URL; `jobforge_open_dashboard` starts the local
dashboard if needed and asks the OS browser to open it. Report the actual URL and
whether it started. Do not automate the approval page.
