from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

from jobforge.automation.answers import AnswerBank
from jobforge.automation.base import fill_form
from jobforge.models import Job, Preferences, Profile


def test_fill_form_second_pass_and_required_checkbox(tmp_path: Path):
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 resume")
    profile = Profile(
        first_name="Ada",
        last_name="Lovelace",
        email="ada@example.com",
        phone="+1 555 0100",
        location="New York, NY",
        work_auth={"authorized": True},
    )
    job = Job(source="test", source_id="1", company="Acme", title="Engineer")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            """
            <form>
              <label for="name">Full name</label><input id="name">
              <label for="email">Email</label><input id="email" type="email">
              <label for="phone">Phone</label><input id="phone" type="tel">
              <label for="resume">Resume</label><input id="resume" type="file">
              <label for="work_auth">Are you authorized to work in the United States?</label>
              <select id="work_auth" onchange="
                document.getElementById('visa-wrap').style.display =
                  this.value === 'Yes' ? 'block' : 'none';
              ">
                <option>Select...</option>
                <option>Yes</option>
                <option>No</option>
              </select>
              <div id="visa-wrap" style="display:none">
                <label for="visa">Visa type</label><input id="visa">
              </div>
              <label for="agree">I agree to the terms</label>
              <input id="agree" type="checkbox" required>
            </form>
            """
        )

        report = fill_form(
            page,
            job,
            profile,
            Preferences(),
            str(resume),
            AnswerBank(),
        )

        assert report.resume_uploaded is True
        assert any("Email" in field.label for field in report.fields)
        assert any("Visa type" in item for item in report.unresolved)
        assert any("required checkbox" in item.lower() for item in report.unresolved)
        browser.close()
