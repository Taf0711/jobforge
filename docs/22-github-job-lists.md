# GitHub job-list sources

Some public repositories maintain large tables of entry-level, internship, and
new-grad roles (for example `SimplifyJobs/New-Grad-Positions` and
`SimplifyJobs/Summer2026-Internships`). JobForge can use those tables for
discovery without making the aggregator the application destination.

## Configuration

```yaml
sources:
  github_jobs:
    repos:
      - SimplifyJobs/New-Grad-Positions
      - SimplifyJobs/Summer2026-Internships
```

Or choose **Community GitHub job lists** in the Setup wizard, or add
`Owner/Repo` entries in **Setup → Add custom boards → GitHub job-list repos**.

Run only this source:

```bash
jobforge scan --source github_jobs
```

The normal web scan and `jobforge scan` also include it when configured.

## Direct-link rule

A row is imported only when it contains a direct `http(s)` employer or ATS URL.
The Simplify redirect link, `swelist.com`, LinkedIn, Indeed, Jobright, and other
aggregator destinations are discarded. If a row has only an aggregator link, it
is skipped. This keeps the existing quality gate and human review path intact:
the job detail page opens the employer posting, not an aggregator interstitial.

Tracking parameters such as `utm_source=Simplify` and `ref=Simplify` are removed
from the stored URL.

## Supported tables

- HTML tables in the README (`Company`, `Role`, `Location`, `Application`, `Age`)
- Markdown pipe tables with the same common column names

The parser handles grouped-company rows (`↳`), strips leading status emoji, and
deduplicates by company/title/location/direct URL. It caps each repository at
1,500 parsed rows per scan.

## Lead-only limitation

These repositories do not contain the full job description. JobForge stores the
role, company, location, direct apply URL, and listing age, so the scorer treats
them as thin-JD leads. They will usually rank below full ATS postings. The
dashboard has a **Community GitHub leads** panel linking to
`/jobs?source=github_jobs&min_score=0`; open the employer posting for the real
requirements before tailoring.

This is deliberately a discovery source, not a claim that the listing is still
open. The employer page is the source of truth.

## Tests

`tests/test_github_jobs_source.py` covers:

- HTML table rows and direct employer links
- grouped-company (`↳`) propagation
- aggregator-only and closed rows being skipped
- Markdown table parsing
- registration, repo configuration, setup preset, and source counting
