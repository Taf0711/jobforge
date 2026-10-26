from __future__ import annotations

from pathlib import Path

import pytest

from jobforge.automation import base as automation_base
from jobforge.automation.base import FieldReport, FillReport, run_application
from jobforge.config import BrowserConfig
from jobforge.models import Job, Preferences, Profile


class FakeButton:
    def __init__(self, page: "FakePage") -> None:
        self.page = page
        self.clicks = 0

    def click(self) -> None:
        self.clicks += 1
        if self.page.success_text:
            self.page.body = self.page.success_text


class FakePage:
    url = "https://jobs.example/apply"

    def __init__(self, tmp_path: Path, success_text: str = "") -> None:
        self.tmp_path = tmp_path
        self.success_text = success_text
        self.body = success_text

    def goto(self, *args, **kwargs) -> None:
        return None

    def wait_for_timeout(self, milliseconds: int) -> None:
        return None

    def inner_text(self, selector: str) -> str:
        return self.body

    def screenshot(self, path: str, full_page: bool = True) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"png")


class FakeAdapter:
    name = "fake"

    def __init__(self, *, resume_uploaded: bool = True, email_filled: bool = True) -> None:
        self.resume_uploaded = resume_uploaded
        self.email_filled = email_filled

    def fill(self, *args, cover_letter_path=None, **kwargs) -> FillReport:
        fields = (
            [FieldReport("Email", "jane@example.com", "email", "filled")]
            if self.email_filled
            else []
        )
        return FillReport(
            ats="fake",
            resume_uploaded=self.resume_uploaded,
            fields=fields,
        )


@pytest.fixture
def fake_browser(monkeypatch, tmp_path):
    page = FakePage(tmp_path)
    button = FakeButton(page)

    class FakeSession:
        def __init__(self, config):
            self.page = page

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def new_page(self):
            return page

    monkeypatch.setattr(automation_base, "BrowserSession", FakeSession)
    monkeypatch.setattr(automation_base, "submit_button", lambda page: button)
    return page, button


def _job() -> Job:
    return Job(
        id=1,
        source="test",
        source_id="1",
        company="Acme",
        title="Engineer",
        apply_url="https://jobs.example/apply",
    )


def _run(
    monkeypatch,
    tmp_path,
    *,
    allow_submit: bool,
    captcha: str,
    authorized: bool = True,
    adapter: FakeAdapter | None = None,
):
    monkeypatch.setattr(automation_base, "captcha_present", lambda page: captcha)
    monkeypatch.setattr(
        automation_base, "get_adapter", lambda url: adapter or FakeAdapter()
    )
    return run_application(
        _job(),
        Profile(id=1),
        Preferences(),
        BrowserConfig(headless=True),
        "resume.pdf",
        allow_submit=allow_submit,
        submission_authorized=authorized,
        hold_seconds=1,
        screenshot_dir=tmp_path,
    )


def test_default_review_mode_never_clicks_submit(fake_browser, monkeypatch, tmp_path):
    _, button = fake_browser
    report = _run(monkeypatch, tmp_path, allow_submit=False, captcha="")
    assert report.submitted is False
    assert button.clicks == 0


def test_direct_allow_submit_requires_service_authorization(fake_browser, monkeypatch, tmp_path):
    _, button = fake_browser
    with pytest.raises(ValueError, match="risk policy"):
        _run(
            monkeypatch,
            tmp_path,
            allow_submit=True,
            captcha="",
            authorized=False,
        )
    assert button.clicks == 0


def test_captcha_blocks_explicit_allow_submit(fake_browser, monkeypatch, tmp_path):
    _, button = fake_browser
    report = _run(monkeypatch, tmp_path, allow_submit=True, captcha="captcha")
    assert report.submitted is False
    assert button.clicks == 0


def test_explicit_allow_submit_requires_confirmation(fake_browser, monkeypatch, tmp_path):
    page, button = fake_browser
    page.success_text = "Thank you for applying."
    report = _run(monkeypatch, tmp_path, allow_submit=True, captcha="")
    assert report.submitted is True
    assert button.clicks == 1


def test_submit_without_confirmation_is_not_marked_submitted(
    fake_browser, monkeypatch, tmp_path
):
    page, button = fake_browser
    page.success_text = ""
    report = _run(monkeypatch, tmp_path, allow_submit=True, captcha="")
    assert report.submitted is False
    assert button.clicks == 1
    assert any("no confirmation" in note for note in report.notes)


def test_missing_resume_blocks_auto_submit(fake_browser, monkeypatch, tmp_path):
    _, button = fake_browser
    report = _run(
        monkeypatch,
        tmp_path,
        allow_submit=True,
        captcha="",
        adapter=FakeAdapter(resume_uploaded=False),
    )
    assert report.submitted is False
    assert button.clicks == 0
    assert any("resume" in note.lower() for note in report.notes)


def test_missing_email_blocks_auto_submit(fake_browser, monkeypatch, tmp_path):
    _, button = fake_browser
    report = _run(
        monkeypatch,
        tmp_path,
        allow_submit=True,
        captcha="",
        adapter=FakeAdapter(email_filled=False),
    )
    assert report.submitted is False
    assert button.clicks == 0
    assert any("email" in note.lower() for note in report.notes)
