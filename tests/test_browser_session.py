from __future__ import annotations

from jobforge.automation.browser import BrowserSession
from jobforge.config import BrowserConfig


class FakeContext:
    def __init__(self) -> None:
        self.pages = [object()]
        self.created: list[object] = []

    def new_page(self):
        page = object()
        self.created.append(page)
        return page


def test_new_page_opens_a_fresh_tab_and_never_reuses_pages_zero():
    session = BrowserSession(BrowserConfig())
    context = FakeContext()
    session.context = context

    page = session.new_page()

    assert page in context.created
    assert page is not context.pages[0]
