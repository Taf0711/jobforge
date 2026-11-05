from __future__ import annotations

from playwright.sync_api import Page

from ..models import Job, Preferences, Profile
from .answers import AnswerBank
from .base import ATSAdapter, FillReport


class LeverAdapter(ATSAdapter):
    name = "lever"
    patterns = ("lever.co",)

    def fill(self, page: Page, job: Job, profile: Profile, prefs: Preferences, resume_path: str, bank: AnswerBank, cover_letter_path: str | None = None) -> FillReport:
        try:
            page.wait_for_selector("form, input[name='name'], input[type='file']", timeout=12_000)
        except Exception:
            pass
        report = super().fill(
            page, job, profile, prefs, resume_path, bank, cover_letter_path=cover_letter_path
        )
        # Lever parses the uploaded resume asynchronously. The submit button can
        # look enabled while "Analyzing resume..." is still running; clicking
        # then does nothing. Wait for the body text to clear before returning.
        for _ in range(30):
            try:
                if "analyzing resume" not in page.inner_text("body").lower():
                    break
            except Exception:
                break
            page.wait_for_timeout(1000)
        return report
