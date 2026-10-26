"""Persistent Chromium session for supervised form filling."""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import BrowserContext, Error as PlaywrightError, Page, Playwright, sync_playwright

from ..config import BrowserConfig


def _launch_options(config: BrowserConfig) -> dict:
    options: dict = {
        "user_data_dir": config.user_data_dir,
        "headless": config.headless,
        "slow_mo": config.slow_mo,
        "viewport": {"width": 1440, "height": 1000},
        "args": ["--disable-dev-shm-usage"],
    }
    if config.channel:
        options["channel"] = config.channel
    return options


class BrowserSession:
    def __init__(self, config: BrowserConfig) -> None:
        self.config = config
        self._playwright: Playwright | None = None
        self.context: BrowserContext | None = None

    def __enter__(self) -> "BrowserSession":
        user_data = Path(self.config.user_data_dir)
        user_data.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        try:
            self.context = self._playwright.chromium.launch_persistent_context(
                **_launch_options(self.config)
            )
        except PlaywrightError as exc:
            # A wheel install may not have downloaded Playwright's Chromium.
            # Fall back to the user's Chrome instead of failing outright.
            if self.config.channel or "executable doesn't exist" not in str(exc).lower():
                raise
            try:
                self.context = self._playwright.chromium.launch_persistent_context(
                    **{**_launch_options(self.config), "channel": "chrome"}
                )
            except PlaywrightError as chrome_exc:
                self._playwright.stop()
                self._playwright = None
                raise RuntimeError(
                    "No browser available. Run `jobforge setup`, or install Chrome, "
                    "or set browser.channel to a Playwright browser channel."
                ) from chrome_exc
        self.context.set_default_timeout(self.config.timeout_ms)
        return self

    def new_page(self) -> Page:
        if not self.context:
            raise RuntimeError("Browser session is not started")
        # Always use a fresh tab. The persistent profile may restore the user's
        # previous tabs; navigating pages[0] would replace their work.
        return self.context.new_page()

    def close(self) -> None:
        if self.context:
            self.context.close()
            self.context = None
        if self._playwright:
            self._playwright.stop()
            self._playwright = None

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        self.close()
