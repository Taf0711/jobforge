from __future__ import annotations

from playwright.sync_api import Page

from ..models import Job, Preferences, Profile
from .answers import AnswerBank
from .base import ATSAdapter, FillReport


class LeverAdapter(ATSAdapter):
    name = "lever"
    patterns = ("lever.co",)

    def fill(self, page: Page, job: Job, profile: Profile, prefs: Preferences, resume_path: str, bank: AnswerBank) -> FillReport:
        try:
            page.wait_for_selector("form, input[name='name'], input[type='file']", timeout=12_000)
        except Exception:
            pass
        return super().fill(page, job, profile, prefs, resume_path, bank)
