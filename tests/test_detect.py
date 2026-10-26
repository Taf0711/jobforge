from __future__ import annotations

from playwright.sync_api import sync_playwright

from jobforge.automation.detect import captcha_present, submit_button


def test_cloudflare_interstitial_text_is_detected():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content("<body><p>Just a moment...</p></body>")
        assert captcha_present(page) == "just a moment"
        browser.close()


def test_generic_apply_button_is_not_treated_as_submit():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        apply_only = browser.new_page()
        apply_only.set_content("<button>Apply</button>")
        assert submit_button(apply_only) is None

        submit = browser.new_page()
        submit.set_content('<button type="submit">Submit application</button>')
        assert submit_button(submit) is not None
        browser.close()
