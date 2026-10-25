# Answer bank editor (L3)

`data/answers.json` is the substring-match override bank used by
`jobforge/automation/answers.py::AnswerBank` when the browser filler answers
screening questions. Before this lane it was hand-edited only. The editor adds:

- `GET /answers` — HTML page extending `base.html`
- `POST /answers` — add or update one override from a form, then redirect
- `GET /api/answers` — JSON object of current overrides
- `PUT /api/answers` — validate and atomically replace the whole object

Overrides are checked before the built-in profile rules **for ordinary
questions**. Legal attestations and voluntary disclosures (work authorization,
clearance, veteran status, disability, gender, sex, race, ethnicity, national
origin, age/DOB, military/marital status, sponsorship) ignore overrides, so a
short key such as `a` cannot silently answer a demographic form.
Work authorization is answered only when `profile.work_auth.authorized` is
explicitly set to a boolean or a clear yes/no/true/false value; clearance only
when `profile.work_auth.clearance` is non-empty. With an empty profile those
fields stay unresolved. Separators in field names are canonicalized before
matching (`work_authorization` becomes `work authorization`), so a short
override cannot slip past the sensitive gate. Sponsorship comes from
`preferences.needs_sponsorship` (default: no). Matching is case-insensitive
substring matching, so `notice period` covers several question phrasings.

## Files

| File | Purpose |
|---|---|
| `jobforge/web/answers_routes.py` | Router, validation, atomic read/write |
| `jobforge/web/templates/answers.html` | Page (autoescaped, no `\|safe`) |
| `jobforge/web/static/answers.js` | Client-side pre-check of the form |
| `tests/test_answers_ui.py` | Route and file-safety tests |

## Public interface

```python
from jobforge.web.answers_routes import create_answers_router

router = create_answers_router(config)  # config: jobforge.config.Config
app.include_router(router)
```

The router registers the four paths above without a prefix. It opens
`db.connect(config.db_path)` per request and calls `services.ensure_profile`,
so the database must already be initialized (`db.init_db`, which
`create_app` does on startup).

## Mounting in `app.py` (parent-owned)

At the top of `jobforge/web/app.py`:

```python
from .answers_routes import create_answers_router
```

Inside `create_app`, after `app = FastAPI(...)` and the static mount:

```python
app.include_router(create_answers_router(cfg))
```

That is the exact integration point. No other app change is needed; the
existing `/static` mount serves `answers.js`, and the router uses the same
`jobforge/web/templates` directory. The security middleware applies the Origin
check to `POST`/`PUT`/`PATCH`/`DELETE`, and when `server.access_token` is set it
protects every route except `/healthz` and `/auth` — including `GET /answers`
and `GET /api/answers`.

## Nav link (parent-owned `base.html`)

Add to the `<nav>` in `jobforge/web/templates/base.html`, next to
Applications:

```html
<a href="/answers" class="{{ 'active' if request.url.path == '/answers' else '' }}">Answers</a>
```

## Data and safety contract

- Path: `<config.data_dir>/answers.json`. The router never derives the path
  from user input.
- Atomic write: temp file in the same directory, flush + `fsync`,
  `os.replace`, then mode `0600`.
- Limits: 200 entries; key 1–120 characters; value 1–500 characters.
- Rejected in keys and values: `<`, `>`, and control characters.
- Rejected payloads: non-dict JSON, non-string keys/values, malformed JSON.
  All return HTTP 400 with a `detail` message.
- A missing or malformed `answers.json` reads as `{}`; the next successful
  write replaces it.
- Keys are stripped of surrounding whitespace. `AnswerBank` lowercases keys on
  load, so matching remains case-insensitive.
- No `|safe`; Jinja autoescape stays on. A hand-edited value containing HTML is
  rendered escaped.

## Tests

```bash
$JOBFORGE_REPO/.venv/bin/python -m pytest tests/test_answers_ui.py -q
```

Coverage: page render, POST add/update, GET JSON, PUT replace, all validation
rejections, malformed JSON, XSS-like value rejection, escaped page rendering,
traversal-like key containment, atomic file mode/existence, and malformed-file
fallback.
