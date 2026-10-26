"""Offline, fixture-driven evaluation for the ATS form-filling adapters.

Each fixture is a static HTML form saved from the shape of a real ATS form.
The eval renders it in headless Chromium, runs the production adapter, and
checks deterministic outcomes: expected values, unresolved sensitive fields,
resume upload, no captcha, and no submit.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..automation.answers import AnswerBank
from ..models import Job, Preferences, Profile

DEFAULT_VIEWPORT = {"width": 1280, "height": 900}
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "ats"

# Fixture stem -> (adapter name, URL used only to select the adapter).
FIXTURE_TARGETS: dict[str, tuple[str, str]] = {
    "lever": ("lever", "https://jobs.lever.co/acme/123/apply"),
    "greenhouse": ("greenhouse", "https://boards.greenhouse.io/acme/jobs/123"),
    "ashby": ("ashby", "https://jobs.ashbyhq.com/acme/123/apply"),
    "workable": ("workable", "https://apply.workable.com/acme/j/ABC123/"),
    "smartrecruiters": (
        "smartrecruiters",
        "https://jobs.smartrecruiters.com/Acme/12345",
    ),
    "workday": (
        "workday",
        "https://acme.wd1.myworkdayjobs.com/en-US/External/job/123",
    ),
}

# Each fixture carries one voluntary disclosure. The exact wording differs so
# the eval covers both gender and veteran fields.
DISCLOSURE_BY_ATS: dict[str, str] = {
    "lever": "Gender",
    "greenhouse": "Veteran status",
    "ashby": "Gender",
    "workable": "Veteran status",
    "smartrecruiters": "Gender",
    "workday": "Veteran status",
}

SCREENING_QUESTION = "Why do you want to work here?"
WORK_AUTH_LABEL = "authorized to work"


@dataclass
class EvalSpec:
    name: str
    ats: str
    url_hint: str
    fixture_path: str
    expected_filled: dict[str, str] = field(default_factory=dict)
    expected_unresolved: list[str] = field(default_factory=list)
    expect_resume_uploaded: bool = True
    timeout_ms: int = 5000
    headless: bool = True


@dataclass
class EvalResult:
    ok: bool
    ats: str
    checks: list[dict[str, Any]]
    fields: dict[str, str]
    unresolved: list[str]
    captcha: str
    submitted: bool
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "ats": self.ats,
            "checks": self.checks,
            "fields": self.fields,
            "unresolved": self.unresolved,
            "captcha": self.captcha,
            "submitted": self.submitted,
            "error": self.error,
        }


def demo_profile() -> Profile:
    """Stable profile used by the CLI and tests. No sensitive disclosures."""
    return Profile(
        first_name="Ada",
        last_name="Lovelace",
        email="ada@example.com",
        phone="+1 555 0100",
        location="London, UK",
        links={
            "linkedin": "https://www.linkedin.com/in/ada",
            "github": "https://github.com/ada",
        },
        experience=[
            {
                "company": "Analytical Engines Ltd",
                "title": "Software Engineer",
            }
        ],
        work_auth={"authorized": True, "clearance": "None"},
        disclosures={},
    )


def demo_preferences() -> Preferences:
    return Preferences(needs_sponsorship=False, open_to_relocation=True)


def spec_for_fixture(fixture_path: str | Path, profile: Profile) -> EvalSpec:
    """Build the canonical spec for a known fixture stem."""
    path = Path(fixture_path)
    ats, url_hint = FIXTURE_TARGETS.get(
        path.stem.lower(), (path.stem.lower(), "")
    )
    company = ""
    if profile.experience:
        company = str(profile.experience[0].get("company", "") or "")
    authorized = bool(profile.work_auth.get("authorized", True))
    expected_filled = {
        "Full name": profile.full_name,
        "Email": profile.email,
        "Phone": profile.phone,
        "Current location": profile.location,
        "Current company": company,
        "LinkedIn": profile.links.get("linkedin", ""),
        "GitHub": profile.links.get("github", ""),
        WORK_AUTH_LABEL: "Yes" if authorized else "No",
    }
    expected_filled = {
        label: value for label, value in expected_filled.items() if value
    }
    disclosure = DISCLOSURE_BY_ATS.get(ats, "Gender")
    return EvalSpec(
        name=f"{ats}-apply",
        ats=ats,
        url_hint=url_hint,
        fixture_path=str(path),
        expected_filled=expected_filled,
        expected_unresolved=[SCREENING_QUESTION, disclosure],
    )


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "detail": detail}


def _field_matches(
    report_fields: list[Any], label_substring: str, expected_value: str
) -> tuple[bool, str]:
    needle = label_substring.lower()
    seen = [
        (f.label, f.value)
        for f in report_fields
        if needle in (f.label or "").lower()
    ]
    for label, value in seen:
        if value == expected_value:
            return True, f"{label!r} = {value!r}"
    if seen:
        return False, f"expected {expected_value!r}; found {seen!r}"
    return False, f"no filled field label contained {label_substring!r}"


def _contains_substring(values: list[str], needle: str) -> bool:
    needle_l = needle.lower()
    return any(needle_l in (value or "").lower() for value in values)


def evaluate(
    spec: EvalSpec,
    profile: Profile,
    prefs: Preferences,
    work_dir: str | Path,
) -> EvalResult:
    """Render one fixture, run its adapter, and return deterministic checks."""
    checks: list[dict[str, Any]] = []
    fields: dict[str, str] = {}
    unresolved: list[str] = []
    captcha = ""
    submitted = False

    try:
        from playwright.sync_api import sync_playwright

        from ..automation.base import get_adapter
        from ..automation.detect import captcha_present

        fixture = Path(spec.fixture_path)
        html = fixture.read_text(encoding="utf-8")

        work_path = Path(work_dir)
        work_path.mkdir(parents=True, exist_ok=True)
        resume_path = work_path / f"resume-{spec.name}.pdf"
        resume_path.write_bytes(b"%PDF-1.4\n% adapter eval placeholder\n")

        job = Job(
            source=spec.ats,
            source_id=spec.name,
            company="Acme",
            title="Software Engineer",
            url=spec.url_hint,
            apply_url=spec.url_hint,
        )
        adapter = get_adapter(spec.url_hint)
        bank = AnswerBank(None)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=spec.headless)
            try:
                page = browser.new_page(viewport=DEFAULT_VIEWPORT)
                page.set_default_timeout(spec.timeout_ms)
                page.set_content(
                    html,
                    wait_until="domcontentloaded",
                    timeout=spec.timeout_ms,
                )
                report = adapter.fill(
                    page, job, profile, prefs, str(resume_path), bank
                )
                captcha = captcha_present(page)
                # run_application sets this in production; the eval path must set
                # it too or the captcha check would compare an unset field.
                report.captcha = captcha
                submitted = report.submitted
                fields = {f.label: f.value for f in report.fields}
                unresolved = list(report.unresolved)

                checks.append(
                    _check(
                        "adapter",
                        adapter.name == spec.ats,
                        f"selected {adapter.name!r}, expected {spec.ats!r}",
                    )
                )
                for label, expected in spec.expected_filled.items():
                    passed, detail = _field_matches(
                        report.fields, label, expected
                    )
                    checks.append(_check(f"field:{label}", passed, detail))
                for needle in spec.expected_unresolved:
                    passed = _contains_substring(report.unresolved, needle)
                    checks.append(
                        _check(
                            f"unresolved:{needle}",
                            passed,
                            f"unresolved={report.unresolved!r}",
                        )
                    )
                checks.append(
                    _check(
                        "resume_uploaded",
                        report.resume_uploaded == spec.expect_resume_uploaded,
                        f"resume_uploaded={report.resume_uploaded!r}, "
                        f"expected {spec.expect_resume_uploaded!r}",
                    )
                )
                checks.append(
                    _check(
                        "submitted",
                        report.submitted is False,
                        f"submitted={report.submitted!r}; eval never submits",
                    )
                )
                checks.append(
                    _check(
                        "captcha",
                        report.captcha == "",
                        f"captcha={report.captcha!r}",
                    )
                )
                checks.append(_check("no_exception", True, "fill completed"))
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 - surfaced as a typed eval failure
        error = f"{type(exc).__name__}: {exc}"
        checks.append(_check("no_exception", False, error))
        return EvalResult(
            ok=False,
            ats=spec.ats,
            checks=checks,
            fields=fields,
            unresolved=unresolved,
            captcha=captcha,
            submitted=submitted,
            error=error,
        )

    ok = all(check["passed"] for check in checks)
    return EvalResult(
        ok=ok,
        ats=spec.ats,
        checks=checks,
        fields=fields,
        unresolved=unresolved,
        captcha=captcha,
        submitted=submitted,
        error="",
    )


def evaluate_all(
    spec_dir: str | Path,
    profile: Profile,
    prefs: Preferences,
    work_dir: str | Path,
) -> list[EvalResult]:
    """Evaluate every HTML fixture in *spec_dir* in filename order."""
    paths = sorted(Path(spec_dir).glob("*.html"))
    return [
        evaluate(spec_for_fixture(path, profile), profile, prefs, work_dir)
        for path in paths
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the offline ATS adapter eval."
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--all", action="store_true", help="run all fixtures")
    target.add_argument(
        "--fixture", type=Path, help="run the spec for one fixture path"
    )
    parser.add_argument(
        "--fixture-dir",
        type=Path,
        default=DEFAULT_FIXTURE_DIR,
        help="fixture directory used by --all",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the typed JSON verdict (the default output format)",
    )
    args = parser.parse_args(argv)

    profile = demo_profile()
    prefs = demo_preferences()
    with tempfile.TemporaryDirectory(prefix="jobforge-adapter-eval-") as tmp:
        if args.all:
            results = evaluate_all(args.fixture_dir, profile, prefs, tmp)
        else:
            spec = spec_for_fixture(args.fixture, profile)
            results = [evaluate(spec, profile, prefs, tmp)]

    ok = bool(results) and all(result.ok for result in results)
    payload = {
        "ok": ok,
        "results": [result.to_dict() for result in results],
    }
    print(json.dumps(payload))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
