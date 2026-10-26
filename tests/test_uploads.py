from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

from jobforge.automation.base import upload_cover_letter, upload_resume


def test_resume_and_cover_letter_upload_to_their_labelled_inputs(tmp_path: Path):
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 resume")
    cover = tmp_path / "cover.txt"
    cover.write_text("cover", encoding="utf-8")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            """
            <label for="resume">Resume</label><input id="resume" type="file">
            <label for="cover">Cover letter</label><input id="cover" type="file">
            """
        )

        assert upload_resume(page, str(resume)) is True
        assert upload_cover_letter(page, str(cover)) is True
        assert page.locator("#resume").evaluate("el => el.files.length") == 1
        assert page.locator("#cover").evaluate("el => el.files.length") == 1
        browser.close()


def test_resume_falls_back_only_for_a_single_unlabelled_file_input(tmp_path: Path):
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 resume")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        single = browser.new_page()
        single.set_content('<input id="file" type="file">')
        assert upload_resume(single, str(resume)) is True

        ambiguous = browser.new_page()
        ambiguous.set_content(
            '<input id="one" type="file"><input id="two" type="file">'
        )
        assert upload_resume(ambiguous, str(resume)) is False
        browser.close()
