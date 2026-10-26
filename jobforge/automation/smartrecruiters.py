from __future__ import annotations

from playwright.sync_api import Page

from ..models import Job, Preferences, Profile
from .answers import AnswerBank
from .base import ATSAdapter, FillReport


class SmartRecruitersAdapter(ATSAdapter):
    name = "smartrecruiters"
    patterns = ("smartrecruiters.com",)

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
        try:
            page.wait_for_selector("form, input[type='file']", timeout=12_000)
        except Exception:
            pass
        return super().fill(
            page, job, profile, prefs, resume_path, bank, cover_letter_path=cover_letter_path
        )
