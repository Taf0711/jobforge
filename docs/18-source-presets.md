# Source presets

The checkout's `config.example.yml` is intentionally small: a new user should
choose target companies, not inherit a 30-board firehose. This page is the
reference preset used in the v0.4.1 local verification. Copy the block into
`config.yml` and trim it to your search.

```yaml
scan:
  keywords:
    - software engineer
    - software development engineer
    - software developer
    - swe
    - backend
    - back end
    - frontend
    - front end
    - full stack
    - full-stack
    - data engineer
    - platform engineer
    - infrastructure engineer
    - machine learning engineer
    - ai engineer
    - member of technical staff
    - new grad
    - entry level
    - junior
  exclude_keywords: [senior, staff, principal, "lead ", manager, director, "head of", "vp "]
  max_new: 500

sources:
  greenhouse:
    boards:
      - discord
      - stripe
      - cloudflare
      - datadog
      - block
      - robinhood
      - brex
      - mercury
      - chime
      - affirm
      - adyen
      - coinbase
      - sofi
      - toast
      - thoughtworks
      - anthropic
      - scaleai
      - databricks
  lever:
    boards: [metlife]
  ashby:
    boards:
      - linear
      - baseten
      - perplexity
      - cohere
      - fireworks
      - modal
      - decagon
      - sierra
      - elevenlabs
      - browserbase
      - cognition
  workable:
    boards: [seedlegals]
  workday:
    boards:
      - "citi|wd5|2"
      - "capitalone|wd12|Capital_One"
  nyc_opendata:
    query: "software"
    limit: 200
    enabled: true
  usajobs:
    keyword: "software engineer"
    limit: 50
    enabled: false
  smartrecruiters:
    companies: []
  github_jobs:
    repos:
      - SimplifyJobs/New-Grad-Positions
      - SimplifyJobs/Summer2026-Internships
```

## Notes

- `scan.max_new` caps inserted jobs per scan. Already-seen fingerprints do not
  consume the cap, so deferred rows are picked up by later scans.
- `scan.keywords` and `scan.exclude_keywords` gate titles before insertion. They
  do not affect scoring; a matching senior title that slips through still faces
  the title gate in the scorer.
- The reference set produced ~6,700 fetched rows and ~470 title-gated new jobs in
  the v0.4.1 local run. The first scan also deep-scored the inserted jobs; later
  scans only score unscored rows.
- Greenhouse boards with `content=true` can exceed 5 MB. The v0.4.1 cap is
  32 MB; the old 5 MB cap silently dropped Stripe, Cloudflare, Datadog,
  Anthropic, and Databricks.
- `ashby:cursor` was removed: its API returned 126 postings, but the public job
  pages were 404 and the descriptions were unrelated boilerplate. Discovery is
  not verification; open the posting before tailoring.
- USAJobs is dormant until `USAJOBS_API_KEY` and `USAJOBS_EMAIL` are exported.
  NYC Open Data is keyless. Both insert canonical public-sector links.
- Aggregator destinations are never used as the apply URL. The GitHub job-list
  source reads public tables from repos such as SimplifyJobs, but imports only
  rows that contain a direct employer/ATS link; rows with only a Simplify,
  Jobright, LinkedIn, or Indeed link are skipped. HN Who-is-Hiring is not
  imported because it lacks a canonical employer URL.
- GitHub job-list rows are lead-only: the README has no full JD, so they usually
  score below full ATS postings. Open the employer posting before tailoring.
  See `docs/22-github-job-lists.md`.
