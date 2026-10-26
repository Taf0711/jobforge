from __future__ import annotations

from playwright.sync_api import Page

from ..sources.direct import detect_ats

CAPTCHA_SELECTORS = [
    'iframe[src*="recaptcha"]',
    'iframe[src*="hcaptcha"]',
    'iframe[src*="turnstile"]',
    '[class*="captcha"]',
    '[id*="captcha"]',
]
CAPTCHA_TEXT = (
    "verify you are human",
    "i'm not a robot",
    "i am not a robot",
    "complete the captcha",
    "security check",
    "just a moment",
    "checking your browser",
    "enable javascript and cookies",
    "attention required",
)


def captcha_present(page: Page) -> str:
    for selector in CAPTCHA_SELECTORS:
        try:
            locator = page.locator(selector).first
            if locator.count() and locator.is_visible():
                return selector
        except Exception:
            continue
    body = ""
    try:
        body = page.locator("body").inner_text(timeout=2000).lower()
    except Exception:
        pass
    for phrase in CAPTCHA_TEXT:
        if phrase in body:
            return phrase
    return ""


def submit_button(page: Page):
    candidates = [
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Submit application")',
        'button:has-text("Send application")',
        'button:has-text("Submit")',
    ]
    for selector in candidates:
        try:
            loc = page.locator(selector).first
            if loc.count() and loc.is_visible():
                return loc
        except Exception:
            continue
    return None


def ats_for_url(url: str) -> str:
    return detect_ats(url)
