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
  --archetypes /path/to/role_archetypes.yml \
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
| `role_archetypes.yml` | `data/role_archetypes.yml`; tailoring uses the detected archetype's `strongest_projects` priority |
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

A profile can carry 13 projects. When `role_archetypes.yml` is present,
`tailor_resume` detects the closest archetype from JD signals and boosts its
`strongest_projects` list; remaining slots are filled by project tags,
`best_for` labels, tech lines, and bullets scored against the JD title, skills,
and a small domain map. Profiles with more than three projects use
`template_compact.typ` (resume-forge-style margins, 9pt bullets, 8pt skills).
The result is checked by the ATS gate and should stay on one page.

## Scoring gates folded in

The JD parser and scorer now use the resume-forge gate model for:

- US-citizenship requirements
- ITAR eligibility
- active security clearance vs. clearance eligibility
- minimum GPA vs. `scoring_inputs.gpa_for_scoring`
- degree floor and years-of-experience survival
- "What We Require" / "Technologies We Use" JD headings
- NYC compensation policy ($130k target / $105k exception / below-floor skip)
- relocation cost-of-living modifiers

The resulting **Pursuit** score is Fit + compensation + location. Fit is never
rewritten by those modifiers.

## Not yet imported

These resume-forge assets are candidates for a later pass:

- callback-odds / ROI / interview-pass model
- start-date gate
- `ats_check.py` and `humanizer_check.py` as stricter post-render gates
- `retrospect.py` funnel/outcome calibration
- per-application email rotation
- `job_fetch.py` source adapters (iCIMS, Oracle, Breezy, USAJobs, NYC Open Data)
