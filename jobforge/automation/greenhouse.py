from __future__ import annotations

from playwright.sync_api import Page

from ..models import Job, Preferences, Profile
from .answers import AnswerBank
from .base import ATSAdapter, FillReport


class GreenhouseAdapter(ATSAdapter):
    name = "greenhouse"
    patterns = ("greenhouse.io",)

    def fill(self, page: Page, job: Job, profile: Profile, prefs: Preferences, resume_path: str, bank: AnswerBank, cover_letter_path: str | None = None) -> FillReport:
        try:
            page.wait_for_selector("form, #application_form, input[type='file']", timeout=12_000)
        except Exception:
            pass
        report = super().fill(
            page, job, profile, prefs, resume_path, bank, cover_letter_path=cover_letter_path
        )
        if not report.resume_uploaded:
            report.notes.append("Greenhouse: resume input not found; attach it manually")
        return report
