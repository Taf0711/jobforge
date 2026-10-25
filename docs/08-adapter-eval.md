# ATS Adapter Eval

Offline, fixture-driven regression net for the generic form filler and the four
existing ATS adapters: Lever, Greenhouse, Ashby, and Workable.

The eval exists to catch selector drift. It renders a saved HTML form, runs the
real adapter through the real `fill_form` path, and asserts typed outcomes. It
does not use an LLM or an agentic judge for pass/fail.

## Command

```bash
# All four fixtures, one JSON document on stdout. Exit 0 iff every result is ok.
.venv/bin/python -m jobforge.audit.adapter_eval --all --json

# One fixture (adapter selected by fixture stem).
.venv/bin/python -m jobforge.audit.adapter_eval --fixture tests/fixtures/ats/lever.html --json

# Focused tests.
.venv/bin/python -m pytest tests/test_adapter_eval.py -q
```

The CLI always prints one JSON document:

```json
{"ok": true, "results": [{"ok": true, "ats": "lever", "checks": [], "fields": {}, "unresolved": [], "captcha": "", "submitted": false, "error": ""}]}
```

Exit code is `0` when `ok` is true and `1` otherwise. No screenshot is written,
no network request is made, and no submit control is clicked.

## Public interface

```python
EvalSpec(
    name: str,
    ats: str,
    url_hint: str,
    fixture_path: str,
    expected_filled: dict[str, str],   # label substring -> exact value
    expected_unresolved: list[str],    # label substrings that must stay empty
    expect_resume_uploaded: bool = True,
    timeout_ms: int = 5000,
    headless: bool = True,
)

EvalResult(
    ok: bool,
    ats: str,
    checks: list[{"name": str, "passed": bool, "detail": str}],
    fields: dict[str, str],
    unresolved: list[str],
    captcha: str,
    submitted: bool,
    error: str = "",
)

evaluate(spec, profile, prefs, work_dir) -> EvalResult
evaluate_all(spec_dir, profile, prefs, work_dir) -> list[EvalResult]
```

`evaluate` launches Chromium headless with a fixed 1280x900 viewport, calls
`page.set_content(fixture_html, wait_until="domcontentloaded")`, writes a
placeholder PDF into `work_dir`, selects the adapter with
`get_adapter(spec.url_hint)`, and calls `adapter.fill(..., AnswerBank(None))`.
It then evaluates these checks:

| Check | Assertion |
|---|---|
| `adapter` | `get_adapter(url_hint).name == spec.ats` |
| `field:<label>` | a filled field whose label contains the substring has the exact value |
| `unresolved:<label>` | the unresolved list contains the substring (case-insensitive) |
| `resume_uploaded` | matches `spec.expect_resume_uploaded` |
| `submitted` | `report.submitted is False` in the fill path; the separate `tests/test_submit_gate.py` covers `run_application` |
| `captcha` | `captcha_present(page)` is empty; a captcha-bearing fixture fails the gate |
| `no_exception` | fill completed without raising; the exception text is in `error` |

`ok` is the conjunction of all checks and the absence of an exception.

## Parameter matrix

| Parameter | Value | Why |
|---|---|---|
| Browser | Playwright Chromium, `headless=True` | Deterministic and fast; matches CI |
| Viewport | `1280x900` | Desktop layout; visible fields are stable |
| Page load | `set_content(..., wait_until="domcontentloaded")` | Fixtures are local static HTML; no network wait |
| Timeout | `timeout_ms=5000` default, applied to page default and `set_content` | Bounded failure instead of a hung gate |
| Input | `tests/fixtures/ats/{lever,greenhouse,ashby,workable}.html` | One fixture per existing adapter |
| Profile | `demo_profile()`: Ada Lovelace, no disclosures, work-authorized | Fixed values, including the empty sensitive fields |
| Preferences | `demo_preferences()` | No sponsorship; relocation allowed |
| Answer bank | `AnswerBank(None)` | No overrides can mask a filler regression |
| Resume | Placeholder `*.pdf` bytes in a temp dir | Exercises `set_input_files` without depending on a real PDF |
| Screenshot | Never | Keeps the eval fast and avoids artifact coupling |
| Submit | Never | The eval proves the fill stops before submission |

Every fixture contains: full name, email, phone, current location, current
company, LinkedIn URL, GitHub URL, a resume file input, the screening question
"Why do you want to work here?", a work-authorization control the answer bank
can resolve to Yes/No, and a voluntary disclosure (gender or veteran status)
that must remain unresolved.

The fixtures intentionally use different discovery surfaces:

| Fixture | Discovery style | Work authorization |
|---|---|---|
| `lever.html` | `label[for]` plus ids and names | select |
| `greenhouse.html` | wrapping labels, `label[for]`, and `aria-label` | wrapping-label select |
| `ashby.html` | generated `_systemfield_*` ids with `label[for]` | fieldset + radio group |
| `workable.html` | placeholders and `aria-label` (no text inputs with names) | select with `aria-label` |

## Fixture expectations

| Fixture | Expected filled | Expected unresolved |
|---|---|---|
| lever | 8 fields including work auth `Yes` | screening question, `Gender` |
| greenhouse | 8 fields including work auth `Yes` | screening question, `Veteran status` |
| ashby | 8 fields including work auth `Yes` | screening question, `Gender` |
| workable | 8 fields including work auth `Yes` | screening question, `Veteran status` |

Resume upload is expected on every fixture. `submitted` and `captcha` must be
falsey on every fixture. `tests/test_adapter_eval.py::test_captcha_fixture_fails_the_gate`
proves the captcha check is live by injecting a visible captcha into a fixture.

## Adding a fixture

1. Save the form as `tests/fixtures/ats/<ats>.html`. It must be static HTML with
   visible labels or attributes; do not include scripts or external assets.
2. If the adapter already exists, the file stem is the ATS name. For a new ATS,
   add the stem to `FIXTURE_TARGETS` in `jobforge/audit/adapter_eval.py` and to
   `DISCLOSURE_BY_ATS` if the voluntary disclosure differs.
3. Include the eight factual fields, the resume file input, a screening
   question, a resolvable work-authorization control, and an unresolved
   voluntary disclosure.
4. Run `pytest tests/test_adapter_eval.py -q`, then
   `python -m jobforge.audit.adapter_eval --all --json`.
5. If the new fixture represents an adapter that is not yet registered in
   `get_adapter`, add a temporary `EvalSpec`-based test; the shared adapter
   registry is parent-owned.

## Why no LLM or agentic judge

Pass/fail must be reproducible and cheap. An LLM judge introduces model
version, temperature, prompt, and sampling variance, which turns a selector
regression into a non-deterministic verdict. The assertions here are exact
label substrings and exact values. If a future qualitative check is useful
(e.g., "does the filled form read naturally"), it can be advisory only, with a
pinned model, temperature 0, an explicit rubric, and cited evidence.

## What a failure means

A failed fixture is one of two things:

1. **Selector drift**: the DOM shape changed and `_label_for`, `upload_resume`,
   or an adapter-specific probe no longer finds a field. The failing check names
   the label; compare `fields` and `unresolved` in the JSON result against the
   fixture.
2. **Filler regression**: the field is found but filled with the wrong value,
   a sensitive disclosure was guessed, the resume was not uploaded, or a
   captcha/submit appeared.

Triage order: open the failing fixture's `checks`, fix the adapter or the
expected value, and only then update the fixture. A fixture change without a
corresponding real-form check is a weakened regression net.
