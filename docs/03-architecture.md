# JobForge — Architecture

JobForge is a local-first, single-user replacement for the useful parts of Jobbie. It runs on your machine, stores data in SQLite, uses your own browser, and never submits an application without a human click.

## 1. Principles

1. **Local first.** No account, no cloud, no telemetry.
2. **Explainable.** Every score has reasons and gaps; every answer has a source fact.
3. **Truthful.** Tailoring may reorder and rephrase verified facts; it may not invent.
4. **Supervised.** The default loop fills the form and stops. The human reviews and submits.
5. **Public data only.** ATS board APIs and public job pages; no Jobbie API, no scraping behind auth.
6. **No captcha arms race.** Detect, pause, hand control to the human.
7. **Composable.** CLI and HTTP API share the same service layer.

## 2. Module map

```
jobforge/
  config.py          config.yml + paths + env
  db.py              SQLite schema, migrations, repositories
  models.py          dataclasses, enums, serialization
  sources/           ATS discovery adapters
    base.py          HTTP, HTML→text, normalization, fingerprint
    greenhouse.py    board API
    lever.py         postings API
    ashby.py         posting API
    workable.py      widget API
    smartrecruiters.py
    direct.py        paste-a-URL fetch + ATS detection
    registry.py      source registry
  matching/
    resume.py        PDF/DOCX/TXT → structured profile
    jd.py            JD → requirements
    skills.py        skill graph + partial credit
    score.py         gates × coverage, reasons, gaps
  tailoring/
    tailor.py        truthful reorder + Typst render + compile
    llm.py           OpenAI-compatible / Ollama client (optional)
    ats_check.py     PDF text-layer and layout checks
    template.typ     one clean resume template
  automation/
    browser.py       persistent Chromium context
    base.py          adapter contract, answer bank, review gate
    greenhouse.py    fill logic
    lever.py
    ashby.py
    workable.py
    detect.py        ATS detection + captcha detection
  email/
    imap.py          IMAP poll, verification-code extraction, reply classification
  web/
    app.py           FastAPI app + routes
    templates/       Jinja templates
    static/          CSS/JS
  audit/
    parity.py        feature-matrix coverage check
    security.py      secret/SSRF/prompt-injection checks
  cli.py             argparse CLI
  __main__.py        python -m jobforge
```

## 3. Data flow

### 3.1 Discover

```
config.yml (boards)
   → sources.<ats>.fetch(board)
   → normalize to Job
   → fingerprint = sha256(company|title|location|url)
   → upsert jobs
```

### 3.2 Score

```
resume PDF → extract_text → parse_resume → Profile
job.description → parse_jd → requirements
Profile.skills + skill_graph → coverage(partial credit)
hard gates (location, work auth, years, degree, clearance)
fit = coverage × gate_survival × 100
store match(reasons, gaps)
```

### 3.3 Tailor

```
Profile fact bank + Job requirements + Match gaps
   → deterministic reorder/select (always)
   → optional LLM rewrite constrained to fact IDs (if configured)
   → fact-diff audit: reject any sentence not supported by a fact
   → render template.typ
   → typst compile → output/<company>/<role>.pdf
   → ats_check
```

### 3.4 Apply

```
Job.apply_url → detect ATS
   → launch persistent Chromium (headed)
   → adapter.fill(page, profile, answers, resume_path)
   → screenshot + fill report
   → human reviews in the open browser
   → human clicks Submit (or adapter.submit only with --allow-submit)
   → record application + answers + screenshot
```

### 3.5 Track

```
IMAP poll → messages → verification codes / replies
   → classify (interview, offer, rejection, confirmation, other)
   → append event + update application status
```

## 4. Trust boundaries and controls

| Risk | Control |
|---|---|
| SSRF via pasted URL / resume link | `http(s)` only; resolve DNS; block loopback, link-local, private, multicast; max redirects; max bytes |
| Prompt injection in JD | JD is delimited as untrusted data; system prompt forbids following JD instructions; output must cite fact IDs |
| Hallucinated resume content | Fact bank + sentence-level support check; reject unsupported claims |
| XSS in dashboard | Jinja autoescape; no `|safe` on user data; CSP header |
| SQL injection | Parameterized queries only |
| Path traversal in uploads | `Path.name`, UUID filenames, output inside data dir |
| Secret leakage | `.env` gitignored; `config.yml` holds no secrets; keyring optional |
| Browser credential theft | No password storage; user logs in manually; persistent profile dir is local and gitignored |
| Unattended spam | Submit is human-only; `--allow-submit` is explicit and audited |
| Captcha bypass | None; pause and hand off |
| Rate-limit abuse | Per-host token bucket; backoff on 429 |
| Data loss | SQLite WAL; `jobforge export`; `jobforge backup` |

## 5. Run modes

- `jobforge serve` — dashboard at `127.0.0.1:8765` (default).
- `jobforge scan --source greenhouse --board stripe` — discovery.
- `jobforge score` — rescore unscored jobs.
- `jobforge tailor --job 42` — resume PDF.
- `jobforge apply --job 42` — fill + review.
- `jobforge email poll` — IMAP.
- `jobforge audit` — parity + security checks.

## 6. Non-goals

Cloud hosting, multi-tenant auth, payments, mobile apps, employer marketplace, captcha solving, Indeed/LinkedIn automation, mass unattended submission.

## 7. Why this shape

Jobbie's difficulty is operational, not architectural. A local tool that uses public ATS APIs, the user's own browser, and a human review gate removes the three most expensive and riskiest layers (remote browser fleet, captcha vendor, inbox domain) while keeping the value: fresh roles, a defensible match score, a tailored resume, and a filled form.
