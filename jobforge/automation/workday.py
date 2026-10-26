"""Supervised Workday form filling.

Workday applications frequently sit behind an employer account. This adapter
never creates an account, never types a password, and never submits. A visible
password field or strong account-wall copy stops the filler and hands the
browser to the human; a multi-field first step with only header sign-in links
continues to fill.
"""

from __future__ import annotations

from playwright.sync_api import Page

from ..models import Job, Preferences, Profile
from .answers import AnswerBank
from .base import ATSAdapter, FillReport

ACCOUNT_WALL_NOTE = "Workday account wall: human must authenticate"
# Bare "Sign in" is navigation chrome on real Workday forms. Only strong
# account-wall copy, or a visible password field, should stop the filler.
_ACCOUNT_WALL_TEXT = ("create account", "already have an account")


class WorkdayAdapter(ATSAdapter):
    name = "workday"
    patterns = ("myworkdayjobs.com", "myworkdaysite.com")

    def fill(
        self,
        page: Page,
        job: Job,
        profile: Profile,
        prefs: Preferences,
        resume_path: str,
        bank: AnswerBank,
        cover_letter_path: str | None = None,
    ) -> FillReport:
        _open_application(page)
        if _account_wall(page):
            report = FillReport(ats=self.name, url=page.url)
            report.notes.append(ACCOUNT_WALL_NOTE)
            return report
        return super().fill(
            page, job, profile, prefs, resume_path, bank, cover_letter_path=cover_letter_path
        )


def _open_application(page: Page) -> None:
    """Workday job URLs land on the posting page, then a wizard chooser."""
    try:
        if page.locator('form, input[type="file"]').count() > 0:
            return
    except Exception:
        return
    apply_link = page.locator('[data-automation-id="adventureButton"]').first
    try:
        if apply_link.count():
            href = apply_link.get_attribute("href")
            if href:
                page.goto(href, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(3000)
            else:
                apply_link.click()
                page.wait_for_timeout(3000)
    except Exception:
        pass
    # The apply wizard opens with "Autofill with Resume" / "Apply Manually".
    for selector in (
        'a:has-text("Apply Manually")',
        'button:has-text("Apply Manually")',
        'a:has-text("Autofill with Resume")',
        'button:has-text("Autofill with Resume")',
    ):
        try:
            locator = page.locator(selector).first
            if locator.count() and locator.is_visible():
                locator.click()
                page.wait_for_timeout(4000)
                return
        except Exception:
            continue


def _visible_field_count(page: Page) -> int:
    selector = (
        'input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="reset"]):not([type="image"]):not([type="file"]), textarea, select'
    )
    try:
        return sum(1 for field in page.locator(selector).all() if field.is_visible())
    except Exception:
        return 0


def _account_wall(page: Page) -> bool:
    """True when the page shows an account gate instead of an application form."""
    body = ""
    try:
        body = page.locator("body").inner_text(timeout=2000).lower()
    except Exception:
        pass
    text_wall = any(phrase in body for phrase in _ACCOUNT_WALL_TEXT)

    password_visible = False
    try:
        password_visible = any(
            field.is_visible() for field in page.locator('input[type="password"]').all()
        )
    except Exception:
        pass
    if not (text_wall or password_visible):
        return False

    # A visible password field is always a handoff, even if the page also has
    # a file input. The adapter must never type a password.
    if password_visible:
        return True

    # A file input means the application form is reachable on this page; the
    # sign-in copy is then just navigation chrome.
    try:
        if page.locator('input[type="file"]').count() > 0:
            return False
    except Exception:
        pass

    # A real first-step form has multiple visible fields. Header chrome with
    # "Create account" does not make it a wall on its own.
    return _visible_field_count(page) < 2
