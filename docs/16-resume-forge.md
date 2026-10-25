# Importing resume-forge data

resume-forge is the personal, verified resume system. JobForge can import its
curated data instead of re-parsing a PDF heuristically. The importer is local:
no resume-forge data is bundled in JobForge, sent anywhere, or written to the
public repository.

## Command

```bash
jobforge import-resume-forge \
  --profile  /path/to/profile.yml \
  --projects /path/to/projects.yml \
  --evidence /path/to/evidence_matrix.yml \
  --network  /path/to/network.yml \
  --applications /path/to/applications.yml \
  --resume   /path/to/Tafseer_H_Resume.pdf \
  --email    you@example.com
```

Only `--profile` is required. `--resume` is a base PDF; when supplied, its
curated bullets replace long planning notes in `profile.yml`'s `bullet_facts`.
`--email` overrides the candidate email in the generated resume header.
`--network` writes `data/network.yml` unless the file already exists; pass
`--force-network` to replace it. `--applications` imports historical tracker
rows and skips `Not Started` rows unless `--include-unstarted` is passed.

## What maps

| resume-forge | JobForge |
| `candidate` | `Profile` name, email, phone, location, links |
| `education` | `Profile.education` |
| `authorization` + `scoring_inputs` | `Profile.work_auth` (citizenship, sponsorship, ITAR, clearance eligibility, GPA) |
| `experience[].bullet_facts` | experience entries + fact bank |
| `experience[].evidence_tags` | fact skills and profile skills (known skills only) |
| `projects.yml` | project bank with `tags`, `best_for`, `tech_line`, and project facts |
| `evidence_matrix.yml` skills | profile skill inventory |
| `network.yml` | `data/network.yml`, converted to JobForge's relationship/strength schema |
| `applications.yml` | jobs + application rows with mapped statuses |

## Application status mapping

| resume-forge | JobForge |
| `Not Started` | `draft` |
| `Resume Ready` | `prepared` |
| `Applied` / `OA` | `submitted` |
| `Phone Screen` / `Interview` | `interview` |
| `Offer` | `offer` |
| `Rejected` | `rejected` |
| `Withdrawn` | `withdrawn` |

The original stage, channel, email, notes, and resume-forge id are kept in the
application notes.

## Project selection and one-page output

A profile can carry 13 projects. `tailor_resume` scores project tags,
`best_for` labels, tech lines, and bullets against the job title, JD skills, and
a small domain map, then renders the top three. Profiles with more than three
projects use `template_compact.typ` (resume-forge-style margins, 9pt bullets,
8pt skills). The result is checked by the ATS gate and should stay on one page.

## Not yet imported

These resume-forge assets are candidates for a later pass:

- `role_archetypes.yml` for explicit archetype-to-project selection
- `scoring_model.py` GPA/citizenship/ITAR/start-date/comp-policy gates and ROI
- `ats_check.py` and `humanizer_check.py` as stricter post-render gates
- `retrospect.py` funnel/outcome calibration
- per-application email rotation
- `job_fetch.py` source adapters (iCIMS, Oracle, Breezy, USAJobs, NYC Open Data)
