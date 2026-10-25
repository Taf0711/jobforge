# Warm-path referral routing

Warm-path routing answers two questions before an application is prepared:

1. **Do I know anyone at this company?**
2. **How should that change my channel and priority?**

It is local-only, read-only over a small YAML/JSON file, and it **does not
change the existing fit-score math**. The referral signal is a separate
display and routing input.

## Data file

Copy the sample and replace the fictional people with your own contacts:

```bash
cp network.example.yml data/network.yml   # data/ is gitignored
```

```yaml
connections:
  - name: "Dana Whitfield"
    company: "Northwind Robotics, Inc."
    role: "Engineering Manager"
    relationship: "former_colleague"
    strength: "strong"
    last_contact: "2026-09-30"
    notes: "Worked together on the search team."
```

| Field | Required | Values / meaning |
|---|---|---|
| `name` | yes | Display name. |
| `company` | yes | Employer. Compared after normalization. |
| `role` | no | Free text. `Hiring Manager` and `Recruiter` roles earn bonuses. |
| `relationship` | yes | `alumni`, `former_colleague`, `friend`, `recruiter`, `hiring_manager`, `other`. |
| `strength` | yes | `strong` (1.0), `medium` (0.6), `weak` (0.3). |
| `last_contact` | no | ISO date (`2026-09-30`) or a datetime. Missing means no recency bonus. |
| `notes` | no | Free text, private to the local file. |

YAML is parsed with `yaml.safe_load`; a `.json` path uses `json.loads`. A
missing file, invalid file, or malformed entry produces an empty/partial graph
plus a message in `NetworkGraph.warnings` — it never raises.

## Public interface

```python
from jobforge.networking import (
    NetworkGraph,
    WarmPath,
    normalize_company,
    channel,
    modify_fit,
)

graph = NetworkGraph("data/network.yml")
warm = graph.warm_path("Acme, Inc.")        # WarmPath
route = channel(78, warm)                   # channel string
adjusted, delta, reason = modify_fit(78, warm)
```

### `normalize_company(name) -> str`

Lowercases, replaces punctuation with spaces, collapses whitespace, and strips
trailing corporate suffixes: `inc`, `llc`, `ltd`, `corp`, `corporation`, `co`,
`company`, `group`, `holdings`, `technologies`, `technology`, `software`,
`systems`, `solutions`, `labs`.

```python
normalize_company("Acme, Inc.")               # "acme"
normalize_company("Acme Technologies LLC")    # "acme"
```

### `WarmPath`

| Field | Type | Meaning |
|---|---|---|
| `company` | `str` | Normalized company key. |
| `score` | `int` | 0–100 warm score for the strongest matching contact. |
| `contacts` | `list[dict]` | Matching contacts, serializable for the web layer. |
| `reason` | `str` | Human-readable explanation. |
| `action` | `str` | Warm-only recommendation; combine with fit using `channel`. |

### Warm score

For each matching contact:

```
value = strength_weight
      + role_bonus
      + recency_bonus
value = min(1.0, value)
```

| Component | Value |
|---|---|
| `strength=strong` | 1.0 |
| `strength=medium` | 0.6 |
| `strength=weak` | 0.3 |
| role/relationship is hiring manager | +0.15 |
| role/relationship is recruiter | +0.10 |
| relationship is alumni | +0.05 |
| `last_contact` within 90 days | +0.10 |
| `last_contact` within 365 days | +0.05 |
| older or missing | +0.00 |

Bonuses are not stacked across role and relationship: the best applicable
role bonus is used. The company score is `round(100 * max(value))`; contacts
do not sum. A strong hiring manager seen last week is 100, not 125.

`contacts_for(company)` returns all matches sorted by strength (strong first)
then most recent contact. `warm_path(company, today=None)` accepts an optional
date for deterministic tests; it defaults to `date.today()`.

When there are no contacts, `warm_path` returns `score=0`, `contacts=[]`,
`action="cold_only"`, and a reason such as `No known contacts at acme`.

### `channel(fit_score, warm_path) -> str`

| Condition | Channel |
|---|---|
| warm score 0, or fit < 40 | `cold_only` |
| fit ≥ 70 and warm score ≥ 70 | `referral_first` |
| fit ≥ 50 and warm score ≥ 40 | `warm_intro` |
| fit ≥ 40 and warm score > 0 | `cold_with_warm_fallback` |
| otherwise | `cold_only` |

The 40-point fit floor is deliberate: a referral cannot rescue a role that is
a poor match, and social capital should not be spent there.

### `modify_fit(fit_score, warm_path) -> (adjusted, delta, reason)`

```python
adjusted = min(100, fit + 0.15 * warm.score)
delta    = adjusted - fit          # capped at +15
```

This is **display-only**. A weak 30-point path adds 4.5 to a 35 fit (39.5), so
a weak tie cannot turn a sub-40 role into a high-priority one. Callers must
keep the stored `MatchResult.score` unchanged; show `adjusted` as a separate
"with warm path" annotation if useful.

## Integration without changing scoring math

The parent owns the score and web wiring. A minimal read-only integration:

```python
from dataclasses import asdict
from pathlib import Path

from jobforge.networking import NetworkGraph, channel, modify_fit

network = NetworkGraph(Path(config.data_dir) / "network.yml")
warm = network.warm_path(job.company)
route = channel(match.score, warm)
adjusted, delta, why = modify_fit(match.score, warm)

context["warm_path"] = asdict(warm)
context["warm_route"] = route
context["warm_adjusted_fit"] = adjusted
context["warm_warnings"] = network.warnings
```

**Implemented:** `services.load_network()` and `services.warm_summary()` attach
the warm result to job detail, and the dashboard shows a `Warm` column. The job
detail panel and the dashboard badge hide `cold_only`, as specified below.
Covered by `test_answers_router_and_warm_panel` (badge shown for
`referral_first`) and `test_cold_only_warm_path_is_hidden` (panel and badge
hidden for `cold_only`).

- Badge values: `referral_first` in green, `warm_intro` in blue,
  `cold_with_warm_fallback` neutral, `cold_only` hidden.
- Do not write `adjusted` into `matches.score`, the database, or the ranking
  query. If a ranking change is desired later, it belongs in the parent's
  score layer with an explicit flag and its own test.
- A missing `data/network.yml` is a normal first-run state: show no badge.
  Optionally surface `network.warnings` on the settings page.
- The result is JSON-serializable (`contacts` dates are ISO strings).

## Example output

Using `network.example.yml` with `today=2026-10-24`:

```python
>>> graph = NetworkGraph("network.example.yml")
>>> warm = graph.warm_path("Acme Analytics", date(2026, 10, 24))
>>> warm
WarmPath(
    company='acme analytics',
    score=75,
    contacts=[{
        'name': 'Priya Raman',
        'company': 'Acme Analytics LLC',
        'role': 'Technical Recruiter',
        'relationship': 'recruiter',
        'strength': 'medium',
        'last_contact': '2026-05-12',
        'notes': 'Met at a meetup; asked me to send roles I am excited about.',
    }],
    reason='1 contact(s) at acme analytics; best contact: Priya Raman (medium recruiter), last contact 2026-05-12',
    action='referral_first',
)
>>> channel(80, warm)
'referral_first'
>>> channel(60, warm)
'warm_intro'
>>> modify_fit(70, warm)
(81.25, 11.25, 'Warm path score 75 adds +11.2 fit points')
```

## Limitations

- `last_contact` is self-reported and never refreshed automatically. A stale
  `strong` contact still scores 100 if there are no other signals.
- Company matching is exact after suffix normalization. Subsidiaries, former
  names, and rebrands are different companies unless you add an alias field.
- No graph traversal: a friend-of-a-friend is not a warm path.
- Only role keywords `hiring manager`, `recruiter`, and relationship `alumni`
  earn bonuses; other relationship types get strength only.
- The network file is plaintext. Keep it in `data/` (gitignored), do not commit
  real contact details, and do not send it to an LLM.
- The score is a prioritization hint, not evidence that a contact is still at
  the company or willing to refer you. Verify before reaching out.
- No outreach is automated. The user sends every message.
