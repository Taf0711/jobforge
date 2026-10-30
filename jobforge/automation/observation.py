"""Read-only page normalizer for the decision contract.

The normalizer turns a rendered application page into a typed
``decision_model.Observation``. It never fills, uploads, clicks, or submits.
The browser adapters remain the only executors in phase 1.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from playwright.sync_api import Locator, Page

from ..models import Job, Preferences, Profile
from .answers import AnswerBank
from .cognition import classify_question
from .decision_model import (
    ButtonObservation,
    FieldCategory,
    FieldKind,
    FieldObservation,
    Observation,
    PageKind,
    ValueSource,
)
from .detect import captcha_present

MAX_CONTROLS = 400

SUCCESS_PHRASES = (
    "thank you for applying",
    "application submitted",
    "application received",
    "we received your application",
    "we have received your application",
    "submission complete",
    "successfully submitted",
)
CLOSED_PHRASES = (
    "no longer accepting applications",
    "no longer accepting",
    "position has been filled",
    "this job has expired",
    "applications are closed",
    "job is closed",
)
ACCOUNT_WALL_PHRASES = (
    "create an account to apply",
    "create an account",
    "sign in to apply",
    "log in to apply",
    "login to apply",
    "register to apply",
    "sign in to continue",
)
LOGIN_PATH_TOKENS = ("/login", "/signin", "/sign-in", "/register", "/create-account")

CATEGORY_MAP: dict[str, FieldCategory] = {
    "first_name": FieldCategory.IDENTITY,
    "last_name": FieldCategory.IDENTITY,
    "full_name": FieldCategory.IDENTITY,
    "name": FieldCategory.IDENTITY,
    "email": FieldCategory.CONTACT,
    "phone": FieldCategory.CONTACT,
    "linkedin": FieldCategory.CONTACT,
    "github": FieldCategory.CONTACT,
    "portfolio": FieldCategory.CONTACT,
    "location": FieldCategory.LOCATION,
    "relocation": FieldCategory.LOCATION,
    "work_authorization": FieldCategory.WORK_AUTHORIZATION,
    "sponsorship": FieldCategory.SPONSORSHIP,
    "education": FieldCategory.EDUCATION,
    "current_company": FieldCategory.EXPERIENCE,
    "current_title": FieldCategory.EXPERIENCE,
    "experience": FieldCategory.EXPERIENCE,
    "motivation": FieldCategory.MOTIVATION,
    "salary": FieldCategory.SALARY,
    "clearance": FieldCategory.CLEARANCE,
    "veteran": FieldCategory.DEMOGRAPHIC,
    "disability": FieldCategory.DEMOGRAPHIC,
    "gender": FieldCategory.DEMOGRAPHIC,
    "race": FieldCategory.DEMOGRAPHIC,
    "orientation": FieldCategory.DEMOGRAPHIC,
    "consent": FieldCategory.CONSENT,
    "source": FieldCategory.UNKNOWN,
    "start_date": FieldCategory.UNKNOWN,
    "file": FieldCategory.FILE,
}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _clean_label(text: str) -> str:
    text = re.sub(r"\s*\((?:required|optional)[^)]*\)\s*$", "", text or "", flags=re.I)
    text = re.sub(r"\s*\*\s*$", "", text)
    return _clean(text)


def _visible(locator: Locator) -> bool:
    try:
        return locator.count() > 0 and locator.is_visible()
    except Exception:
        return False


def _text(locator: Locator) -> str:
    try:
        return _clean(locator.inner_text(timeout=1000))
    except Exception:
        return ""


def _attr(locator: Locator, name: str) -> str:
    try:
        return str(locator.get_attribute(name) or "").strip()
    except Exception:
        return ""


def _has_attr(locator: Locator, name: str) -> bool:
    try:
        return locator.get_attribute(name) is not None
    except Exception:
        return False


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}-{digest}"


def posted_age_days(posted_at: str | None, *, now: datetime | None = None) -> int | None:
    """Return whole days since the posting date, or None when unparseable."""
    if not posted_at:
        return None
    raw = str(posted_at).strip().replace("Z", "+00:00")
    try:
        posted = datetime.fromisoformat(raw)
    except ValueError:
        try:
            posted = datetime.strptime(raw[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return max(0, (current - posted).days)


def _label_for(page: Page, locator: Locator) -> str:
    aria = _attr(locator, "aria-label")
    if aria:
        return _clean_label(aria)
    labelledby = _attr(locator, "aria-labelledby")
    if labelledby:
        parts = []
        for token in labelledby.split():
            try:
                node = page.locator(f"#{token}")
                if node.count():
                    parts.append(_text(node.first))
            except Exception:
                continue
        if any(parts):
            return _clean_label(" ".join(parts))
    element_id = _attr(locator, "id")
    if element_id:
        try:
            label = page.locator(f'label[for="{element_id}"]').first
            if label.count():
                text = _text(label)
                if text:
                    return _clean_label(text)
        except Exception:
            pass
    try:
        text = locator.evaluate("el => el.closest('label')?.innerText || ''")
        if text and _clean(text):
            return _clean_label(str(text))
    except Exception:
        pass
    try:
        text = locator.evaluate("el => el.parentElement?.innerText || ''")
        cleaned = _clean(str(text or ""))
        if 2 < len(cleaned) < 240:
            return _clean_label(cleaned)
    except Exception:
        pass
    return _clean_label(_attr(locator, "name") or _attr(locator, "placeholder"))


def _group_question(page: Page, radio: Locator) -> str:
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
        text = _clean(str(text or ""))
        if text and option_label and text == option_label:
            return ""
        return _clean_label(text)
    except Exception:
        return ""


def _required(locator: Locator, label: str) -> bool:
    if _has_attr(locator, "required"):
        return True
    if _attr(locator, "aria-required").lower() == "true":
        return True
    if label.rstrip().endswith("*"):
        return True
    try:
        marker = locator.evaluate(
            """el => {
              const field = el.closest('[data-field-path]');
              const label = field ? field.querySelector('label') : null;
              if (!label) return false;
              return label.className.includes('required') || label.innerText.includes('*');
            }"""
        )
        return bool(marker)
    except Exception:
        return False


def _enabled(locator: Locator) -> bool:
    try:
        if locator.is_disabled():
            return False
    except Exception:
        pass
    return _attr(locator, "aria-disabled").lower() != "true"


def _kind_for(tag: str, input_type: str, role: str) -> FieldKind:
    if role == "combobox":
        return FieldKind.COMBOBOX
    if tag == "select":
        return FieldKind.SELECT
    if tag == "textarea":
        return FieldKind.TEXTAREA
    return {
        "email": FieldKind.EMAIL,
        "tel": FieldKind.TEL,
        "url": FieldKind.URL,
        "number": FieldKind.NUMBER,
        "date": FieldKind.DATE,
        "radio": FieldKind.RADIO,
        "checkbox": FieldKind.CHECKBOX,
        "file": FieldKind.FILE,
    }.get(input_type, FieldKind.TEXT)


def _infer_category(label: str) -> FieldCategory:
    lowered = label.lower()
    if re.search(r"\b(resume|cv|curriculum vitae)\b", lowered):
        return FieldCategory.FILE
    if re.search(r"\bcover letter\b", lowered):
        return FieldCategory.FILE
    if re.search(r"\b(e-?mail)\b", lowered):
        return FieldCategory.CONTACT
    if re.search(r"\b(phone|mobile|telephone)\b", lowered):
        return FieldCategory.CONTACT
    if re.search(r"\b(linkedin|github|portfolio|website)\b", lowered):
        return FieldCategory.CONTACT
    if re.search(r"\b(first|last|full|your) name\b|^name$", lowered):
        return FieldCategory.IDENTITY
    if re.search(r"\b(location|city|relocat|where.*based)\b", lowered):
        return FieldCategory.LOCATION
    if re.search(r"\b(sponsor|visa)\b", lowered):
        return FieldCategory.SPONSORSHIP
    if re.search(r"\b(authoriz|authoris|eligible to work|right to work)\b", lowered):
        return FieldCategory.WORK_AUTHORIZATION
    if re.search(r"\b(school|university|degree|graduat|education)\b", lowered):
        return FieldCategory.EDUCATION
    if re.search(r"\b(company|employer|current title|job title)\b", lowered):
        return FieldCategory.EXPERIENCE
    if re.search(r"\b(why|motivat|tell us|about yourself|interest)\b", lowered):
        return FieldCategory.MOTIVATION
    if re.search(r"\b(veteran|disabilit|gender|race|ethnic|orientation)\b", lowered):
        return FieldCategory.DEMOGRAPHIC
    if re.search(r"\b(salary|compensation|desired pay|pay expectation)\b", lowered):
        return FieldCategory.SALARY
    if re.search(r"\b(clearance|polygraph)\b", lowered):
        return FieldCategory.CLEARANCE
    if re.search(r"\b(consent|agree|terms|background check|attest)\b", lowered):
        return FieldCategory.CONSENT
    return FieldCategory.UNKNOWN


def _answer_for(
    label: str,
    field_kind: FieldKind,
    profile: Profile,
    prefs: Preferences,
    bank: AnswerBank,
    resume_path: str,
    cover_letter_path: str,
) -> tuple[FieldCategory, bool, str, ValueSource]:
    if field_kind is FieldKind.FILE:
        lowered = label.lower()
        if "autofill" in lowered:
            # Ashby has an optional "Autofill from resume" uploader next to the
            # actual resume field. It is not the resume attachment.
            return FieldCategory.FILE, False, "", ValueSource.NONE
        if "cover" in lowered:
            return FieldCategory.FILE, bool(cover_letter_path), cover_letter_path, ValueSource.HUMAN
        if "resume" in lowered or "curriculum" in lowered or re.search(r"\bcv\b", lowered):
            return FieldCategory.FILE, bool(resume_path), resume_path, ValueSource.RESUME
        # Do not attach the resume to an unnamed or transcript-only upload.
        return FieldCategory.FILE, False, "", ValueSource.NONE

    decision = classify_question(label, profile, prefs, bank)
    answer = bank.answer(label, profile, prefs)
    category = CATEGORY_MAP.get(decision.category)
    if category is None:
        category = _infer_category(label)
    if answer.value:
        source = ValueSource.ANSWER_BANK if answer.category == "override" else ValueSource.PROFILE
        return category, True, answer.value, source
    return category, False, "", ValueSource.NONE


def _field_value(locator: Locator, tag: str, kind: FieldKind) -> str:
    try:
        if kind is FieldKind.CHECKBOX:
            return "true" if locator.is_checked() else ""
        if kind is FieldKind.FILE:
            files = locator.evaluate("el => el.files ? el.files.length : 0")
            return "uploaded" if int(files or 0) > 0 else ""
        if kind is FieldKind.RADIO:
            return "true" if locator.is_checked() else ""
        value = str(locator.input_value() or "").strip()
        if value.lower() in {"select...", "select", "choose...", "choose", "--", "-"}:
            return ""
        return value
    except Exception:
        return ""


def _options(locator: Locator, tag: str) -> tuple[str, ...]:
    if tag != "select":
        return ()
    try:
        return tuple(
            _clean(option.inner_text())
            for option in locator.locator("option").all()
            if _clean(option.inner_text())
        )
    except Exception:
        return ()


def _field_from_control(
    page: Page,
    locator: Locator,
    profile: Profile,
    prefs: Preferences,
    bank: AnswerBank,
    resume_path: str,
    cover_letter_path: str,
) -> FieldObservation | None:
    try:
        tag = str(locator.evaluate("el => el.tagName.toLowerCase()") or "")
        input_type = (_attr(locator, "type") or "text").lower()
        role = _attr(locator, "role").lower()
    except Exception:
        return None
    if tag == "input" and input_type in {"hidden", "submit", "button", "reset", "image", "password"}:
        return None
    if tag == "input" and input_type == "radio":
        return None
    if not _visible(locator) or not _enabled(locator):
        return None
    kind = _kind_for(tag, input_type, role)
    label = _label_for(page, locator)
    if not label or len(label) > 300:
        return None
    category, grounded, answer_value, source = _answer_for(
        label, kind, profile, prefs, bank, resume_path, cover_letter_path
    )
    field_id = _attr(locator, "id") or _attr(locator, "name") or _stable_id("field", label)
    return FieldObservation(
        field_id=field_id,
        label=label,
        kind=kind,
        required=_required(locator, label),
        visible=True,
        enabled=True,
        options=_options(locator, tag),
        value=_field_value(locator, tag, kind),
        category=category,
        sensitive=category in {
            FieldCategory.DEMOGRAPHIC,
            FieldCategory.SALARY,
            FieldCategory.CLEARANCE,
            FieldCategory.CONSENT,
        },
        has_grounded_answer=grounded,
        answer_value=answer_value,
        value_source=source,
        error="invalid" if _attr(locator, "aria-invalid").lower() == "true" else "",
    )


def _radio_fields(
    page: Page,
    profile: Profile,
    prefs: Preferences,
    bank: AnswerBank,
) -> list[FieldObservation]:
    out: list[FieldObservation] = []
    seen: set[str] = set()
    try:
        radios = page.locator('input[type="radio"]').all()
    except Exception:
        return out
    for radio in radios:
        if not _visible(radio) or not _enabled(radio):
            continue
        name = _attr(radio, "name") or _attr(radio, "id")
        if not name or name in seen:
            continue
        seen.add(name)
        question = _group_question(page, radio) or _label_for(page, radio)
        if not question:
            continue
        try:
            group = (
                page.locator(f'input[type="radio"][name="{name}"]').all()
                if _attr(radio, "name")
                else [radio]
            )
        except Exception:
            group = [radio]
        options = tuple(_label_for(page, item) for item in group if _label_for(page, item))
        selected = ""
        for item in group:
            try:
                if item.is_checked():
                    selected = _label_for(page, item)
                    break
            except Exception:
                continue
        category, grounded, answer_value, source = _answer_for(
            question, FieldKind.RADIO, profile, prefs, bank, "", ""
        )
        out.append(
            FieldObservation(
                field_id=name,
                label=question,
                kind=FieldKind.RADIO,
                required=_required(radio, question),
                visible=True,
                enabled=True,
                options=options,
                value=selected,
                category=category,
                sensitive=category in {
                    FieldCategory.DEMOGRAPHIC,
                    FieldCategory.SALARY,
                    FieldCategory.CLEARANCE,
                    FieldCategory.CONSENT,
                },
                has_grounded_answer=grounded,
                answer_value=answer_value,
                value_source=source,
            )
        )
    return out


def _combobox_fields(
    page: Page,
    profile: Profile,
    prefs: Preferences,
    bank: AnswerBank,
) -> list[FieldObservation]:
    out: list[FieldObservation] = []
    try:
        combos = page.locator('[role="combobox"]').all()
    except Exception:
        return out
    for combo in combos:
        if not _visible(combo) or not _enabled(combo):
            continue
        # Prefer the field-path label over the input placeholder. Ashby's
        # location combobox has placeholder "Start typing..." and a real
        # "Location" label in the enclosing [data-field-path].
        try:
            label = _clean_label(
                str(
                    combo.evaluate(
                        """el => {
                          const field = el.closest('[data-field-path]');
                          const label = field ? field.querySelector('label') : null;
                          return label ? label.innerText : '';
                        }"""
                    )
                    or ""
                )
            )
        except Exception:
            label = ""
        label = label or _label_for(page, combo)
        if not label:
            continue
        category, grounded, answer_value, source = _answer_for(
            label, FieldKind.COMBOBOX, profile, prefs, bank, "", ""
        )
        try:
            value = str(combo.input_value() or "").strip()
        except Exception:
            value = ""
        out.append(
            FieldObservation(
                field_id=_attr(combo, "id") or _attr(combo, "name") or _stable_id("combo", label),
                label=label,
                kind=FieldKind.COMBOBOX,
                required=_required(combo, label),
                visible=True,
                enabled=True,
                value=value,
                category=category,
                sensitive=category in {
                    FieldCategory.DEMOGRAPHIC,
                    FieldCategory.SALARY,
                    FieldCategory.CLEARANCE,
                    FieldCategory.CONSENT,
                },
                has_grounded_answer=grounded,
                answer_value=answer_value,
                value_source=source,
            )
        )
    return out


def _buttons(page: Page) -> tuple[ButtonObservation, ...]:
    out: list[ButtonObservation] = []
    seen: set[str] = set()
    try:
        locators = page.locator("button, input[type=submit], input[type=button], [role=button]").all()
    except Exception:
        return ()
    for index, item in enumerate(locators):
        if not _visible(item) or not _enabled(item):
            continue
        label = _text(item) or _attr(item, "value") or _attr(item, "aria-label")
        label = _clean(label)
        if not label:
            continue
        key = f"{label.lower()}:{index}"
        if key in seen:
            continue
        seen.add(key)
        lowered = label.lower()
        is_submit = (
            _attr(item, "type").lower() == "submit"
            or "submit" in lowered
            or "send application" in lowered
        )
        is_next = any(token in lowered for token in ("next", "continue", "proceed"))
        is_back = any(token in lowered for token in ("back", "previous"))
        out.append(
            ButtonObservation(
                button_id=_attr(item, "id") or _attr(item, "name") or f"button-{index}",
                label=label,
                enabled=True,
                is_submit=is_submit and not is_next,
                is_next=is_next,
                is_back=is_back,
            )
        )
    return tuple(out)


def _page_kind(
    page: Page,
    ats: str,
    fields: tuple[FieldObservation, ...],
    buttons: tuple[ButtonObservation, ...],
) -> PageKind:
    if captcha_present(page):
        return PageKind.CAPTCHA
    try:
        body = page.locator("body").inner_text(timeout=2000).lower()
    except Exception:
        body = ""
    url = (page.url or "").lower()
    if any(phrase in body for phrase in ACCOUNT_WALL_PHRASES) or any(
        token in url for token in LOGIN_PATH_TOKENS
    ):
        return PageKind.ACCOUNT_WALL
    if any(phrase in body for phrase in SUCCESS_PHRASES):
        return PageKind.SUCCESS
    if any(phrase in body for phrase in CLOSED_PHRASES):
        return PageKind.CLOSED
    if fields or any(button.is_submit for button in buttons):
        return PageKind.APPLICATION_FORM
    if "apply" in body and ats:
        return PageKind.JOB_DETAIL
    return PageKind.UNKNOWN


def observe_page(
    page: Page,
    *,
    job: Job | None = None,
    profile: Profile,
    prefs: Preferences,
    bank: AnswerBank,
    ats: str = "",
    resume_path: str = "",
    cover_letter_path: str = "",
    quality_ready: bool = True,
    risk_allowed: bool = True,
    job_age_days: int | None = None,
    application_id: str = "",
    step: int = 0,
    attempt: int = 0,
) -> Observation:
    """Build a redacted, read-only observation from the current page."""
    try:
        controls = page.locator("input, textarea, select").all()
    except Exception:
        controls = []
    if len(controls) > MAX_CONTROLS:
        controls = controls[:MAX_CONTROLS]

    fields: list[FieldObservation] = []
    seen: set[str] = set()
    for control in controls:
        # Comboboxes are collected separately so an input[role=combobox] is not
        # emitted twice.
        if _attr(control, "role").lower() == "combobox":
            continue
        item = _field_from_control(
            page, control, profile, prefs, bank, resume_path, cover_letter_path
        )
        if item is None:
            continue
        key = f"{item.field_id.lower()}:{item.kind.value}"
        if key in seen:
            continue
        seen.add(key)
        fields.append(item)
    fields.extend(_radio_fields(page, profile, prefs, bank))
    fields.extend(_combobox_fields(page, profile, prefs, bank))
    buttons = _buttons(page)
    page_kind = _page_kind(page, ats, tuple(fields), buttons)
    age = job_age_days
    if age is None and job is not None:
        age = posted_age_days(job.posted_at)
    return Observation(
        page_kind=page_kind,
        ats=ats,
        url=page.url,
        step=step,
        fields=tuple(fields),
        buttons=buttons,
        captcha=page_kind is PageKind.CAPTCHA,
        account_wall=page_kind is PageKind.ACCOUNT_WALL,
        quality_ready=quality_ready,
        risk_allowed=risk_allowed,
        job_age_days=age,
        application_id=application_id,
        attempt=attempt,
        confidence=0.95,
    )
