"""Generic supervised form filler plus the adapter contract."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import Locator, Page

from ..config import BrowserConfig
from ..models import Job, Preferences, Profile
from .answers import Answer, AnswerBank
from .browser import BrowserSession
from .detect import ats_for_url, captcha_present, submit_button

SKIP_INPUT_TYPES = {"hidden", "submit", "button", "reset", "image", "file", "password"}
SKIP_NAME_PARTS = ("search", "captcha", "password", "otp", "code")
MANUAL_ONLY_HOSTS = ("indeed.com", "linkedin.com")


def is_manual_only_url(url: str) -> bool:
    """Indeed and LinkedIn are manual-only by policy."""
    host = (urlparse(url or "").hostname or "").lower().rstrip(".")
    return any(host == suffix or host.endswith("." + suffix) for suffix in MANUAL_ONLY_HOSTS)


@dataclass
class FieldReport:
    label: str
    value: str
    category: str
    status: str  # filled | unresolved | skipped


@dataclass
class FillReport:
    ats: str = "generic"
    url: str = ""
    fields: list[FieldReport] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    captcha: str = ""
    screenshot: str = ""
    resume_uploaded: bool = False
    submitted: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ats": self.ats,
            "url": self.url,
            "fields": [f.__dict__ for f in self.fields],
            "unresolved": self.unresolved,
            "captcha": self.captcha,
            "screenshot": self.screenshot,
            "resume_uploaded": self.resume_uploaded,
            "submitted": self.submitted,
            "notes": self.notes,
        }


def _visible(locator: Locator) -> bool:
    try:
        return locator.count() > 0 and locator.is_visible()
    except Exception:
        return False


def _label_for(page: Page, locator: Locator) -> str:
    try:
        aria = locator.get_attribute("aria-label")
        if aria:
            return aria.strip()
        el_id = locator.get_attribute("id")
        if el_id:
            lab = page.locator(f'label[for="{el_id}"]')
            if lab.count():
                text = lab.first.inner_text(timeout=1000).strip()
                if text:
                    return text
        text = locator.evaluate("el => el.closest('label')?.innerText || ''")
        if text and text.strip():
            return text.strip()
        text = locator.evaluate("el => el.parentElement?.innerText || ''")
        if text and 2 < len(text.strip()) < 240:
            return text.strip()
        return (locator.get_attribute("name") or locator.get_attribute("placeholder") or "").strip()
    except Exception:
        return ""


def _fill_text(locator: Locator, value: str) -> bool:
    try:
        locator.fill(str(value))
        return True
    except Exception:
        return False


def _fill_select(locator: Locator, value: str) -> bool:
    value_l = value.strip().lower()
    try:
        options = locator.locator("option").all()
        labels: list[str] = []
        for opt in options:
            text = (opt.inner_text() or "").strip()
            if text:
                labels.append(text)
        for text in labels:
            if text.lower() == value_l:
                locator.select_option(label=text)
                return True
        for text in labels:
            if value_l and (value_l in text.lower() or text.lower() in value_l):
                locator.select_option(label=text)
                return True
        for text in labels:
            if text.lower() in {"yes", "no"} and value_l in {"yes", "no"} and text.lower() == value_l:
                locator.select_option(label=text)
                return True
    except Exception:
        return False
    return False


def upload_resume(page: Page, resume_path: str) -> bool:
    if not resume_path or not Path(resume_path).exists():
        return False
    try:
        inputs = page.locator('input[type="file"]').all()
    except Exception:
        return False
    for inp in inputs:
        try:
            inp.set_input_files(resume_path)
            return True
        except Exception:
            continue
    return False


def _option_selected(locator: Locator, value: str) -> bool:
    try:
        locator.check()
        return True
    except Exception:
        return False


def _group_question(page: Page, radio: Locator) -> str:
    """Find the question, not the option label, for a radio group."""
    option_label = _label_for(page, radio)
    try:
        text = radio.evaluate(
            """el => {
              const fs = el.closest('fieldset');
              if (fs && fs.querySelector('legend')) return fs.querySelector('legend').innerText;
              const rg = el.closest('[role="radiogroup"]');
              if (rg && rg.getAttribute('aria-label')) return rg.getAttribute('aria-label');
              let p = el.parentElement;
              while (p && p !== document.body) {
                const prev = p.previousElementSibling;
                if (prev && prev.innerText && prev.innerText.trim().length > 2 && prev.innerText.trim().length < 240) {
                  return prev.innerText;
                }
                p = p.parentElement;
              }
              return '';
            }"""
        )
        text = (text or "").strip()
        if text and option_label and text == option_label.strip():
            return ""
        return text
    except Exception:
        return ""


def _fill_radio_groups(
    page: Page,
    profile: Profile,
    prefs: Preferences,
    bank: AnswerBank,
    report: FillReport,
) -> None:
    try:
        radios = page.locator('input[type="radio"]').all()
    except Exception:
        return
    seen_groups: set[str] = set()
    for index, radio in enumerate(radios):
        if not _visible(radio):
            continue
        name = radio.get_attribute("name") or f"__group_{index}"
        if name in seen_groups:
            continue
        seen_groups.add(name)
        question = _group_question(page, radio)
        if not question:
            continue
        answer = bank.answer(question, profile, prefs)
        if not answer.value:
            report.unresolved.append(question[:200])
            continue
        # Find the radio in this group whose label matches the answer.
        group = page.locator(f'input[type="radio"][name="{name}"]').all() if not name.startswith("__group_") else [radio]
        matched = False
        for option in group:
            option_label = _label_for(page, option)
            if answer.value.lower() in option_label.lower() or option_label.lower() in answer.value.lower():
                if _option_selected(option, answer.value):
                    report.fields.append(FieldReport(question[:200], answer.value, answer.category, "filled"))
                    matched = True
                    break
        if not matched:
            report.unresolved.append(f"{question[:180]} (answer: {answer.value})")


def fill_form(
    page: Page,
    job: Job,
    profile: Profile,
    prefs: Preferences,
    resume_path: str,
    bank: AnswerBank,
    ats: str = "generic",
) -> FillReport:
    report = FillReport(ats=ats, url=page.url)
    report.resume_uploaded = upload_resume(page, resume_path)
    if resume_path and not report.resume_uploaded:
        report.notes.append("No file input found for the resume; upload it manually")

    try:
        controls = page.locator("input, textarea, select").all()
    except Exception:
        controls = []

    for control in controls[:160]:
        if not _visible(control):
            continue
        try:
            tag = control.evaluate("el => el.tagName.toLowerCase()")
            input_type = (control.get_attribute("type") or "text").lower()
        except Exception:
            continue
        if tag == "input" and input_type in SKIP_INPUT_TYPES | {"radio", "checkbox"}:
            continue
        name = (control.get_attribute("name") or "").lower()
        if any(part in name for part in SKIP_NAME_PARTS):
            continue
        label = _label_for(page, control)
        if not label or len(label) > 300:
            continue
        answer = bank.answer(label, profile, prefs)
        if not answer.value:
            report.unresolved.append(label[:200])
            continue
        if tag == "select":
            ok = _fill_select(control, answer.value)
        else:
            ok = _fill_text(control, answer.value)
        if ok:
            report.fields.append(FieldReport(label[:200], answer.value, answer.category, "filled"))
        else:
            report.unresolved.append(f"{label[:180]} (answer: {answer.value})")

    _fill_radio_groups(page, profile, prefs, bank, report)
    return report


class ATSAdapter:
    name = "generic"
    patterns: tuple[str, ...] = ()

    def matches(self, url: str) -> bool:
        return any(pattern in url.lower() for pattern in self.patterns)

    def fill(
        self,
        page: Page,
        job: Job,
        profile: Profile,
        prefs: Preferences,
        resume_path: str,
        bank: AnswerBank,
    ) -> FillReport:
        return fill_form(page, job, profile, prefs, resume_path, bank, ats=self.name)


def get_adapter(url: str) -> ATSAdapter:
    from .ashby import AshbyAdapter
    from .greenhouse import GreenhouseAdapter
    from .lever import LeverAdapter
    from .workable import WorkableAdapter
    from .workday import WorkdayAdapter

    for adapter in (LeverAdapter(), GreenhouseAdapter(), AshbyAdapter(), WorkableAdapter(), WorkdayAdapter()):
        if adapter.matches(url):
            return adapter
    return ATSAdapter()


def run_application(
    job: Job,
    profile: Profile,
    prefs: Preferences,
    config: BrowserConfig,
    resume_path: str,
    *,
    answer_bank_path: str | Path | None = None,
    allow_submit: bool = False,
    hold_seconds: int = 0,
    screenshot_dir: str | Path | None = None,
) -> FillReport:
    url = job.apply_url or job.url
    if not url:
        raise ValueError("Job has no apply URL")
    if is_manual_only_url(url):
        raise ValueError(
            "Indeed and LinkedIn are manual-only in JobForge. Open the posting and apply yourself."
        )
    ats = ats_for_url(url)
    adapter = get_adapter(url)
    bank = AnswerBank(answer_bank_path)

    with BrowserSession(config) as session:
        page = session.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=config.timeout_ms)
        page.wait_for_timeout(2500)
        report = adapter.fill(page, job, profile, prefs, resume_path, bank)
        report.captcha = captcha_present(page)

        out_dir = Path(screenshot_dir) if screenshot_dir else Path("data/screenshots")
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shot = out_dir / f"{job.id or 'job'}-{stamp}.png"
        try:
            page.screenshot(path=str(shot), full_page=True)
            report.screenshot = str(shot)
        except Exception:
            report.notes.append("Screenshot failed")

        if allow_submit and not report.captcha:
            button = submit_button(page)
            if button:
                try:
                    button.click()
                    page.wait_for_timeout(3500)
                    report.submitted = True
                    final = out_dir / f"{job.id or 'job'}-{stamp}-submitted.png"
                    page.screenshot(path=str(final), full_page=True)
                    report.screenshot = str(final)
                except Exception as exc:
                    report.notes.append(f"Submit click failed: {exc}")
            else:
                report.notes.append("No submit button found")

        if hold_seconds > 0:
            page.wait_for_timeout(hold_seconds * 1000)
        elif not allow_submit:
            try:
                input("\nReview the browser. Press Enter to close it (submit is manual)...")
            except EOFError:
                page.wait_for_timeout(60_000)

    return report
