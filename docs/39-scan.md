# Scan and deep fit

JobForge uses two scores. They answer different questions and must not be
confused.

| Score | Table | Question |
|---|---|---|
| **Scan** | `scan_matches` | Is this posting worth opening? |
| **Fit** | `matches` | How well does the full posting match the profile? |

`jobforge scan` fetches, dedupes, filters, and quick-scores. It does **not**
deep-score every row. `jobforge score` deep-scores selected jobs.

## Scan pipeline

```text
sources → normalize → dedupe → first-pass filter → quick scan → scan_matches
```

### First-pass filters

- posting older than `scan.max_age_days` (default 30)
- non-US location unless remote/US-optional
- internships unless the profile targets internships
- senior/lead titles
- non-SWE titles (firmware, hardware, SDET, QA-only, data analyst, IT support)
- excluded companies/keywords
- already-applied role at the same company
- recent rejection at the same company (default 60 days)

Unknown age or location is kept with lower confidence, not silently dropped.

### Quick-scan rubric

The quick score is deliberately cheap and explainable:

- level signal: new grad / entry / engineer 1 / associate / junior
- role relevance and archetype
- location and relocation preference
- freshness + competition profile + citizenship/clearance/ITAR advantage
- company/domain adjacency
- stack signal from title and snippet

Every row stores its component reasons, gaps, confidence, competition profile,
and age.

## Source registry

The grouped `sources:` config still works. A flat resume-forge registry can be
used instead:

```yaml
# config.yml
scan:
  sources_path: ~/Documents/my_resume/scan_sources.yml
  max_age_days: 30
  exclude_non_us: true
  exclude_internships: true
  exclude_senior: true
  recent_rejection_days: 60
```

The flat registry supports per-source `competition_profile` values:
`public_sector`, `cleared`, `direct_ats`, `community`, and `mass_aggregator`.
Known source types include Greenhouse, Lever, Ashby, Workable, Workday,
USAJobs, NYC Open Data, GitHub tables, GitHub pipe tables, Hacker News, and
manual-only sources. Manual sources are skipped; they are never scraped.

## Commands

```bash
jobforge scan                       # fetch + filter + quick-score
jobforge scan --no-filter           # keep every fetched row
jobforge scan --no-quick            # ingest only
jobforge jobs --sort scan           # scan-ranked list
jobforge jobs --min-scan 60         # scan threshold
jobforge score --top 20             # deep-score the top scan rows
jobforge score --job 5424           # deep-score one job
```

## MCP

`jobforge_search_jobs` is scan-ranked by default. Each job returns `scan`,
`scan_confidence`, `scan_band`, `competition_profile`, `age_days`,
`scan_reasons`, `fit`, and `pursuit`. `min_scan` filters on the quick score;
`min_fit` filters on the deep fit.

## Deep fit

Deep fit keeps the explainable skill-graph coverage and hard gates, but:

- location is a **risk/gap**, not a fit multiplier; relocation cost is a
  pursuit modifier;
- lead-only or unparsed descriptions are marked provisional and capped rather
  than presented as precise;
- scan confidence is visible, so a thin GitHub lead does not look equivalent to
  a rich direct-ATS posting.

Deep fit is intentionally not run on every scanned row. Scan first, then score
the jobs a human is actually considering.
