from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobforge.automation.answers import AnswerBank
from jobforge.automation.decision_model import ActionKind, PageKind, decide
from jobforge.automation.observation import observe_page
from jobforge.models import Job, Preferences, Profile

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "ats"


def _profile() -> Profile:
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
        experience=[{"company": "Analytical Engines Ltd", "title": "Software Engineer"}],
        work_auth={"authorized": True},
        disclosures={},
    )


def _job(ats: str) -> Job:
    return Job(
        source=ats,
        source_id="1",
        company="Acme",
        title="Software Engineer",
        url=f"https://example.com/{ats}",
        apply_url=f"https://example.com/{ats}",
    )


def _playwright():
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Playwright is not installed: {exc}")
    try:
        manager = sync_playwright()
        playwright = manager.__enter__()
        browser = playwright.chromium.launch(headless=True)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Chromium is not available: {exc}")
    return manager, browser


def test_ashby_observation_maps_fields_and_review_gap(tmp_path: Path):
    manager, browser = _playwright()
    try:
        page = browser.new_page()
        page.set_content((FIXTURE_DIR / "ashby.html").read_text(encoding="utf-8"))
        resume = tmp_path / "resume.pdf"
        resume.write_bytes(b"%PDF-1.4 resume")
        observation = observe_page(
            page,
            job=_job("ashby"),
            profile=_profile(),
            prefs=Preferences(needs_sponsorship=False, open_to_relocation=True),
            bank=AnswerBank(None),
            ats="ashby",
            resume_path=str(resume),
        )

        assert observation.page_kind is PageKind.APPLICATION_FORM
        by_label = {item.label: item for item in observation.fields}
        assert by_label["Full name"].category.value == "identity"
        assert by_label["Email"].category.value == "contact"
        assert by_label["Current location"].category.value == "location"
        assert by_label["Resume"].category.value == "file"
        assert by_label["Resume"].has_grounded_answer is True
        assert by_label["Why do you want to work here?"].category.value == "motivation"
        assert by_label["Why do you want to work here?"].has_grounded_answer is False
        assert by_label["Gender"].sensitive is True
        assert any(button.is_submit for button in observation.buttons)

        decision = decide(observation)
        assert decision.action is not ActionKind.SUBMIT
        assert decision.action is ActionKind.FILL_TEXT
        assert decision.target == "_systemfield_name"

        redacted = observation.to_dict()
        assert redacted["fields"][0]["answer_value"] == "<redacted>"
        assert redacted["fields"][0]["value"] == ""
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_captcha_page_stops(tmp_path: Path):
    manager, browser = _playwright()
    try:
        page = browser.new_page()
        html = (FIXTURE_DIR / "ashby.html").read_text(encoding="utf-8").replace(
            "</body>",
            '<div class="g-recaptcha" style="display:block">captcha</div></body>',
        )
        page.set_content(html)
        observation = observe_page(
            page,
            job=_job("ashby"),
            profile=_profile(),
            prefs=Preferences(),
            bank=AnswerBank(None),
            ats="ashby",
        )
        assert observation.page_kind is PageKind.CAPTCHA
        assert decide(observation).action is ActionKind.STOP_CAPTCHA
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_account_wall_stops(tmp_path: Path):
    manager, browser = _playwright()
    try:
        page = browser.new_page()
        page.set_content(
            "<html><body><h1>Apply</h1><p>Create an account to apply for this job.</p></body></html>"
        )
        observation = observe_page(
            page,
            job=_job("workday"),
            profile=_profile(),
            prefs=Preferences(),
            bank=AnswerBank(None),
            ats="workday",
        )
        assert observation.page_kind is PageKind.ACCOUNT_WALL
        assert decide(observation).action is ActionKind.STOP_ACCOUNT_WALL
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_all_ats_fixtures_produce_a_typed_observation(tmp_path: Path):
    manager, browser = _playwright()
    try:
        resume = tmp_path / "resume.pdf"
        resume.write_bytes(b"%PDF-1.4 resume")
        stems = ("lever", "greenhouse", "ashby", "workable", "smartrecruiters", "workday")
        for stem in stems:
            page = browser.new_page()
            page.set_content((FIXTURE_DIR / f"{stem}.html").read_text(encoding="utf-8"))
            observation = observe_page(
                page,
                job=_job(stem),
                profile=_profile(),
                prefs=Preferences(needs_sponsorship=False, open_to_relocation=True),
                bank=AnswerBank(None),
                ats=stem,
                resume_path=str(resume),
            )
            assert observation.page_kind is PageKind.APPLICATION_FORM, stem
            assert observation.fields, stem
            assert observation.buttons, stem
            assert decide(observation).action is not ActionKind.SUBMIT, stem
            assert "ada@example.com" not in json.dumps(observation.to_dict()), stem
            page.close()
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_run_application_records_shadow_without_changing_submission(
    tmp_path: Path, monkeypatch
):
    from playwright.sync_api import sync_playwright

    from jobforge.automation import base as automation_base
    from jobforge.automation.base import FieldReport, FillReport, run_application
    from jobforge.automation.decision_shadow import read_shadow
    from jobforge.config import BrowserConfig

    fixture = FIXTURE_DIR / "ashby.html"

    class Session:
        def __init__(self, config):
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self._browser.close()
            self._pw.stop()

        def new_page(self):
            return self._browser.new_page()

    class FakeAdapter:
        name = "ashby"

        def fill(self, *args, **kwargs):
            return FillReport(
                ats="ashby",
                resume_uploaded=True,
                fields=[FieldReport("Email", "ada@example.com", "email", "filled")],
            )

    monkeypatch.setattr(automation_base, "BrowserSession", Session)
    monkeypatch.setattr(automation_base, "get_adapter", lambda url: FakeAdapter())
    shadow = tmp_path / "decision_shadow.jsonl"
    job = _job("ashby")
    job.apply_url = fixture.as_uri()

    report = run_application(
        job,
        _profile(),
        Preferences(needs_sponsorship=False, open_to_relocation=True),
        BrowserConfig(headless=True),
        str(tmp_path / "resume.pdf"),
        hold_seconds=1,
        non_interactive=True,
        screenshot_dir=tmp_path,
        decision_shadow_path=shadow,
        decision_run_id=77,
        decision_job_id=1,
    )

    assert report.submitted is False
    records = read_shadow(shadow)
    assert len(records) == 1
    assert records[0]["run_id"] == 77
    assert records[0]["decision"]["action"] != "submit"
    assert "ada@example.com" not in shadow.read_text(encoding="utf-8")


def test_select_placeholder_is_not_treated_as_filled(tmp_path: Path):
    manager, browser = _playwright()
    try:
        page = browser.new_page()
        page.set_content(
            """
            <form>
              <label for="size">Preferred team size</label>
              <select id="size" required>
                <option value="Select...">Select...</option>
                <option value="small">Small</option>
              </select>
              <button type="submit">Submit application</button>
            </form>
            """
        )
        observation = observe_page(
            page,
            job=_job("generic"),
            profile=_profile(),
            prefs=Preferences(),
            bank=AnswerBank(None),
            ats="generic",
        )
        field = next(item for item in observation.fields if item.field_id == "size")
        assert field.value == ""
        assert field.filled is False
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_autofill_uploader_is_not_treated_as_resume(tmp_path: Path):
    manager, browser = _playwright()
    try:
        page = browser.new_page()
        page.set_content(
            """
            <form>
              <label for="resume">Resume</label>
              <input id="resume" type="file">
              <div>
                Autofill from resume
                <input id="autofill" type="file">
              </div>
              <button type="submit">Submit application</button>
            </form>
            """
        )
        observation = observe_page(
            page,
            job=_job("ashby"),
            profile=_profile(),
            prefs=Preferences(),
            bank=AnswerBank(None),
            ats="ashby",
            resume_path=str(tmp_path / "resume.pdf"),
        )
        by_id = {item.field_id: item for item in observation.fields}
        assert by_id["resume"].has_grounded_answer is True
        assert by_id["resume"].value_source.value == "resume"
        assert by_id["autofill"].has_grounded_answer is False
        assert by_id["autofill"].value_source.value == "none"
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_combobox_prefers_field_path_label_over_placeholder(tmp_path: Path):
    manager, browser = _playwright()
    try:
        page = browser.new_page()
        page.set_content(
            """
            <form>
              <div data-field-path="_systemfield_location">
                <label class="required" for="loc">Location</label>
                <input id="loc" role="combobox" placeholder="Start typing...">
              </div>
              <button type="submit">Submit application</button>
            </form>
            """
        )
        observation = observe_page(
            page,
            job=_job("ashby"),
            profile=_profile(),
            prefs=Preferences(),
            bank=AnswerBank(None),
            ats="ashby",
        )
        field = next(item for item in observation.fields if item.field_id == "loc")
        assert field.label == "Location"
        assert field.category.value == "location"
        assert field.has_grounded_answer is True
        assert field.value_source.value == "profile"
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_stale_job_age_is_observed_and_stops():
    from datetime import datetime, timedelta, timezone

    manager, browser = _playwright()
    try:
        page = browser.new_page()
        page.set_content((FIXTURE_DIR / "ashby.html").read_text(encoding="utf-8"))
        job = _job("ashby")
        job.posted_at = (
            datetime.now(timezone.utc) - timedelta(days=60)
        ).isoformat()
        observation = observe_page(
            page,
            job=job,
            profile=_profile(),
            prefs=Preferences(),
            bank=AnswerBank(None),
            ats="ashby",
        )
        assert observation.job_age_days is not None
        assert observation.job_age_days >= 59
        assert decide(observation).action is ActionKind.STOP_STALE
    finally:
        browser.close()
        manager.__exit__(None, None, None)
