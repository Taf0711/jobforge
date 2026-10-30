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
    cover_letter_uploaded: bool = False
    submitted: bool = False
    required_missing: list[str] = field(default_factory=list)
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
            "cover_letter_uploaded": self.cover_letter_uploaded,
            "submitted": self.submitted,
            "required_missing": self.required_missing,
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


def _file_meta(page: Page, inp: Locator) -> str:
    return " ".join(
        filter(
            None,
            [
                _label_for(page, inp),
                inp.get_attribute("name") or "",
                inp.get_attribute("id") or "",
                inp.get_attribute("accept") or "",
            ],
        )
    ).lower()


def _upload_to_matching(page: Page, path: str, include: tuple[str, ...]) -> bool:
    if not path or not Path(path).exists():
        return False
    try:
        inputs = page.locator('input[type="file"]').all()
    except Exception:
        return False
    scored: list[tuple[int, int, Locator]] = []
    for index, inp in enumerate(inputs):
        try:
            label = _label_for(page, inp).lower()
            name = (inp.get_attribute("name") or "").lower()
            ident = (inp.get_attribute("id") or "").lower()
            accept = (inp.get_attribute("accept") or "").lower()
        except Exception:
            continue
        score = 0
        for token in include:
            if token in name or token in ident:
                score += 4
            if token in label:
                score += 3
            if token in accept:
                score += 1
        if score:
            scored.append((score, index, inp))
    scored.sort(key=lambda item: (-item[0], item[1]))
    candidates = [item[2] for item in scored]
    # Never guess between multiple unnamed upload slots; only fall back when
    # there is exactly one file input on the page.
    if not candidates and len(inputs) == 1:
        candidates = inputs
    for inp in candidates:
        try:
            inp.set_input_files(path)
            files = inp.evaluate("el => el.files ? el.files.length : 0")
            if int(files or 0) > 0:
                return True
        except Exception:
            continue
    return False


def upload_resume(page: Page, resume_path: str) -> bool:
    return _upload_to_matching(page, resume_path, ("resume", "cv", "curriculum"))


def upload_cover_letter(page: Page, cover_letter_path: str) -> bool:
    return _upload_to_matching(
        page, cover_letter_path, ("cover letter", "cover_letter", "coverletter")
    )


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


def _report_required_controls(page: Page, report: FillReport) -> None:
    try:
        required = page.locator(
            'input[required], textarea[required], select[required]'
        ).all()
    except Exception:
        return
    for control in required:
        if not _visible(control):
            continue
        try:
            tag = control.evaluate("el => el.tagName.toLowerCase()")
            input_type = (control.get_attribute("type") or "text").lower()
            value = control.input_value() if tag != "select" else control.input_value()
        except Exception:
            continue
        label = _label_for(page, control) or "required field"
        if tag == "input" and input_type == "file":
            try:
                files = control.evaluate("el => el.files ? el.files.length : 0")
            except Exception:
                files = 0
            if not files:
                message = f"{label[:180]} (required file not uploaded)"
                report.unresolved.append(message)
                report.required_missing.append(message)
        elif tag == "input" and input_type in {"checkbox", "radio"}:
            try:
                if not control.is_checked():
                    message = f"{label[:180]} (required checkbox; human must confirm)"
                    report.unresolved.append(message)
                    report.required_missing.append(message)
            except Exception:
                continue
        elif not str(value or "").strip():
            message = f"{label[:180]} (required field left empty)"
            report.unresolved.append(message)
            report.required_missing.append(message)

    try:
        combos = page.locator('[role="combobox"]').all()
    except Exception:
        combos = []
    for combo in combos:
        if not _visible(combo):
            continue
        try:
            if str(combo.input_value() or "").strip():
                continue
            required = combo.evaluate(
                """el => {
                  const field = el.closest('[data-field-path]');
                  const label = field ? field.querySelector('label') : null;
                  if (!label) return false;
                  return label.className.includes('required') || label.innerText.includes('*');
                }"""
            )
            if not required:
                continue
            label = _label_for(page, combo) or _combobox_question(combo) or "required combobox"
            message = f"{label[:180]} (required combobox left empty)"
            report.unresolved.append(message)
            report.required_missing.append(message)
        except Exception:
            continue


def _combobox_question(combo: Locator) -> str:
    try:
        return str(
            combo.evaluate(
                """el => {
                  const field = el.closest('[data-field-path]');
                  const label = field ? field.querySelector('label') : null;
                  return label ? label.innerText : (el.getAttribute('aria-label') || el.getAttribute('placeholder') || '');
                }"""
            )
            or ""
        ).strip()
    except Exception:
        return ""


def _fill_comboboxes(
    page: Page,
    profile: Profile,
    prefs: Preferences,
    bank: AnswerBank,
    report: FillReport,
) -> None:
    try:
        combos = page.locator('[role="combobox"]').all()
    except Exception:
        return
    for combo in combos:
        if not _visible(combo):
            continue
        question = _combobox_question(combo) or _label_for(page, combo)
        if not question or len(question) > 240:
            continue
        answer = bank.answer(question, profile, prefs)
        if not answer.value:
            report.unresolved.append(question[:200])
            continue
        try:
            combo.click()
            try:
                combo.fill(answer.value)
            except Exception:
                page.keyboard.type(answer.value)
            page.wait_for_timeout(500)
            options = page.locator('[role="option"]').all()
            matched = None
            needle = answer.value.split(",")[0].strip().lower()
            for option in options:
                text = option.inner_text().strip().lower()
                if needle and (needle in text or text in needle):
                    matched = option
                    break
            if matched is None and options:
                matched = options[0]
            if matched is None:
                try:
                    combo.fill("")
                except Exception:
                    pass
                report.unresolved.append(f"{question[:180]} (no combobox option)")
                continue
            matched.click()
            page.wait_for_timeout(200)
            report.fields.append(
                FieldReport(question[:200], matched.inner_text().strip(), answer.category, "filled")
            )
        except Exception:
            report.unresolved.append(f"{question[:180]} (combobox fill failed)")


def fill_form(
    page: Page,
    job: Job,
    profile: Profile,
    prefs: Preferences,
    resume_path: str,
    bank: AnswerBank,
    ats: str = "generic",
    cover_letter_path: str | None = None,
) -> FillReport:
    report = FillReport(ats=ats, url=page.url)
    report.resume_uploaded = upload_resume(page, resume_path)
    if resume_path and not report.resume_uploaded:
        report.notes.append("No matching resume upload input found; attach it manually")
    if cover_letter_path:
        report.cover_letter_uploaded = upload_cover_letter(page, cover_letter_path)
        if not report.cover_letter_uploaded:
            report.notes.append("No matching cover-letter upload input found; attach it manually")

    filled_labels: set[str] = set()
    # Two passes catch fields revealed by answers on the first pass (e.g. "Yes"
    # exposes a follow-up field).
    for _ in range(2):
        try:
            controls = page.locator("input, textarea, select").all()
        except Exception:
            controls = []
        if len(controls) > 400:
            report.notes.append(
                f"Form has {len(controls)} controls; reviewing the first 400 for safety"
            )
        for control in controls[:400]:
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
            if not label or len(label) > 300 or label in filled_labels:
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
                filled_labels.add(label)
                report.fields.append(
                    FieldReport(label[:200], answer.value, answer.category, "filled")
                )
            else:
                report.unresolved.append(f"{label[:180]} (answer: {answer.value})")
        _fill_radio_groups(page, profile, prefs, bank, report)
        if len(filled_labels) == 0:
            break

    _fill_comboboxes(page, profile, prefs, bank, report)
    _report_required_controls(page, report)
    report.unresolved = list(dict.fromkeys(report.unresolved))
    seen_fields: set[tuple[str, str]] = set()
    unique_fields: list[FieldReport] = []
    for field in report.fields:
        key = (field.label, field.value)
        if key in seen_fields:
            continue
        seen_fields.add(key)
        unique_fields.append(field)
    report.fields = unique_fields
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
        cover_letter_path: str | None = None,
    ) -> FillReport:
        return fill_form(
            page,
            job,
            profile,
            prefs,
            resume_path,
            bank,
            ats=self.name,
            cover_letter_path=cover_letter_path,
        )


def get_adapter(url: str) -> ATSAdapter:
    from .ashby import AshbyAdapter
    from .greenhouse import GreenhouseAdapter
    from .lever import LeverAdapter
    from .smartrecruiters import SmartRecruitersAdapter
    from .workable import WorkableAdapter
    from .workday import WorkdayAdapter

    for adapter in (
        LeverAdapter(),
        GreenhouseAdapter(),
        AshbyAdapter(),
        WorkableAdapter(),
        SmartRecruitersAdapter(),
        WorkdayAdapter(),
    ):
        if adapter.matches(url):
            return adapter
    return ATSAdapter()


SUCCESS_PHRASES = (
    "thank you for applying",
    "application submitted",
    "application received",
    "we received your application",
    "we have received your application",
    "submission complete",
    "successfully submitted",
)


def _submission_confirmed(page: Page, before_url: str) -> bool:
    """Conservative post-click check; absence of confirmation is not success."""
    try:
        body = page.inner_text("body").lower()
    except Exception:
        body = ""
    if any(phrase in body for phrase in SUCCESS_PHRASES):
        return True
    try:
        after_url = (page.url or "").lower()
    except Exception:
        after_url = ""
    if after_url and after_url != (before_url or "").lower():
        return any(
            token in after_url
            for token in ("thank", "confirm", "submitted", "success", "complete")
        )
    return False


def run_application(
    job: Job,
    profile: Profile,
    prefs: Preferences,
    config: BrowserConfig,
    resume_path: str,
    *,
    cover_letter_path: str | None = None,
    answer_bank_path: str | Path | None = None,
    allow_submit: bool = False,
    submission_authorized: bool = False,
    review_only: bool = False,
    hold_seconds: int = 0,
    non_interactive: bool = False,
    screenshot_dir: str | Path | None = None,
    decision_shadow_path: str | Path | None = None,
    decision_run_id: int | None = None,
    decision_job_id: int | None = None,
    decision_application_id: str = "",
) -> FillReport:
    if review_only and (allow_submit or submission_authorized):
        raise ValueError("Review-only mode cannot submit or authorize submission")
    if allow_submit and not submission_authorized:
        raise ValueError(
            "Unattended submit must go through services.apply_job and the persisted risk policy"
        )
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
        report = adapter.fill(
            page, job, profile, prefs, resume_path, bank, cover_letter_path=cover_letter_path
        )
        report.captcha = captcha_present(page)

        # Phase 1: observe and record only. This cannot fill, click, or submit.
        if decision_shadow_path:
            try:
                from .decision_model import decide
                from .decision_shadow import record_shadow
                from .observation import observe_page

                observation = observe_page(
                    page,
                    job=job,
                    profile=profile,
                    prefs=prefs,
                    bank=bank,
                    ats=ats,
                    resume_path=resume_path,
                    cover_letter_path=cover_letter_path or "",
                    quality_ready=True,
                    risk_allowed=True,
                    application_id=decision_application_id,
                    step=0,
                    attempt=0,
                )
                record_shadow(
                    decision_shadow_path,
                    observation,
                    decide(observation),
                    run_id=decision_run_id,
                    job_id=decision_job_id,
                    application_id=decision_application_id,
                )
            except Exception as exc:  # noqa: BLE001 - shadow must never break apply
                report.notes.append(
                    f"Decision shadow unavailable: {type(exc).__name__}: {exc}"
                )

        out_dir = Path(screenshot_dir) if screenshot_dir else Path("data/screenshots")
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shot = out_dir / f"{job.id or 'job'}-{stamp}.png"
        try:
            page.screenshot(path=str(shot), full_page=True)
            report.screenshot = str(shot)
        except Exception:
            report.notes.append("Screenshot failed")

        can_submit = allow_submit and not report.captcha
        if allow_submit and not report.resume_uploaded:
            report.notes.append("Auto-submit blocked: resume was not uploaded")
            can_submit = False
        if allow_submit and report.required_missing:
            detail = "; ".join(report.required_missing)[:500]
            report.notes.append(
                "Auto-submit blocked: required fields are still unresolved: " + detail
            )
            can_submit = False
        if allow_submit and not any(field.category == "email" for field in report.fields):
            report.notes.append("Auto-submit blocked: email field was not filled")
            can_submit = False
        if can_submit:
            button = submit_button(page)
            if button:
                try:
                    before_url = page.url
                    button.click()
                    page.wait_for_timeout(3500)
                    report.captcha = captcha_present(page) or report.captcha
                    if report.captcha:
                        report.notes.append(
                            "Captcha appeared after submit; submission not confirmed"
                        )
                    elif _submission_confirmed(page, before_url):
                        report.submitted = True
                        final = out_dir / f"{job.id or 'job'}-{stamp}-submitted.png"
                        page.screenshot(path=str(final), full_page=True)
                        report.screenshot = str(final)
                    else:
                        report.notes.append(
                            "Submit clicked but no confirmation detected; verify manually"
                        )
                except Exception as exc:
                    report.notes.append(f"Submit click failed: {exc}")
            else:
                report.notes.append("No submit button found")

        if hold_seconds > 0:
            page.wait_for_timeout(hold_seconds * 1000)
        elif not report.submitted and not non_interactive:
            try:
                input("\nReview the browser. Press Enter to close it (submit is manual)...")
            except EOFError:
                page.wait_for_timeout(60_000)

    return report
