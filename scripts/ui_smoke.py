#!/usr/bin/env python3
"""Browser contract smoke for the local JobForge dashboard.

Checks the live server at --base-url for the workbench shell, responsive
overflow, console errors, labeled filters, and the absence of the old
auto-open More menu. Run after starting `jobforge serve`.
"""

from __future__ import annotations

import argparse
import json
import sys

from playwright.sync_api import sync_playwright

PAGES = [
    "/",
    "/jobs",
    "/queue",
    "/applications",
    "/operations",
    "/agents",
    "/profiles",
    "/settings",
    "/runs",
    "/inbox",
    "/answers",
    "/autopilot",
    "/setup",
    "/providers",
    "/agent",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    checks: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for width, height, label in ((1440, 1000, "desktop"), (390, 844, "mobile")):
            for path in PAGES:
                page = browser.new_page(
                    viewport={"width": width, "height": height},
                    is_mobile=width < 500,
                    has_touch=width < 500,
                )
                errors: list[str] = []
                page.on(
                    "console",
                    lambda message, errors=errors: errors.append(message.text)
                    if message.type == "error"
                    else None,
                )
                response = page.goto(
                    args.base_url + path, wait_until="networkidle", timeout=30_000
                )
                info = page.evaluate(
                    """() => ({
                        overflow: document.documentElement.scrollWidth > window.innerWidth + 1,
                        oldMore: !!document.querySelector('.nav-more'),
                        drawerOpen: document.body.classList.contains('drawer-open'),
                        hasStore: !!document.querySelector('.live-dot'),
                        filtersLabeled: [...document.querySelectorAll('.filter-field input, .filter-field select')]
                            .every((el) => !!(el.id && document.querySelector('label[for="' + CSS.escape(el.id) + '"]'))),
                    })"""
                )
                checks.append(
                    {
                        "page": path,
                        "viewport": label,
                        "status": response.status if response else 0,
                        "console_errors": errors[:3],
                        "overflow": info["overflow"],
                        "old_more_menu": info["oldMore"],
                        "drawer_open": info["drawerOpen"],
                        "store_badge": info["hasStore"],
                        "filters_labeled": info["filtersLabeled"],
                    }
                )
                page.close()
        browser.close()

    ok = all(
        item["status"] == 200
        and not item["console_errors"]
        and not item["overflow"]
        and not item["old_more_menu"]
        and not item["drawer_open"]
        and item["store_badge"]
        and item["filters_labeled"]
        for item in checks
    )
    result = {"ok": ok, "checks": checks}
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for item in checks:
            status = "ok" if (
                item["status"] == 200
                and not item["console_errors"]
                and not item["overflow"]
                and not item["old_more_menu"]
                and not item["drawer_open"]
                and item["store_badge"]
                and item["filters_labeled"]
            ) else "FAIL"
            print(f"{status:4} {item['viewport']:6} {item['page']} {item['status']}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
