from __future__ import annotations

from pathlib import Path

import pytest

from jobforge.audit.decision_compare import compare_all
from jobforge.audit.adapter_eval import demo_preferences, demo_profile

ATS_DIR = Path(__file__).resolve().parent / "fixtures" / "ats"
PAGES_DIR = Path(__file__).resolve().parent / "fixtures" / "decision_pages"


def _require_chromium() -> None:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Playwright is not installed: {exc}")
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Chromium is not available: {exc}")


def test_six_ats_fixtures_agree_and_are_safe(tmp_path: Path):
    _require_chromium()
    shadow = tmp_path / "compare.jsonl"
    results = compare_all(
        ATS_DIR,
        shadow_out=shadow,
        profile=demo_profile(),
        prefs=demo_preferences(),
        work_dir=tmp_path,
    )
    assert len(results) == 6
    assert all(result.safe for result in results)
    assert all(result.agrees for result in results)
    assert all(result.expected == "submit" for result in results)
    raw = shadow.read_text(encoding="utf-8")
    assert "ada@example.com" not in raw
    assert "<redacted>" in raw


def test_n1_required_why_requests_review_before_submit(tmp_path: Path):
    _require_chromium()
    results = compare_all(
        PAGES_DIR,
        shadow_out=tmp_path / "n1.jsonl",
        profile=demo_profile(),
        prefs=demo_preferences(),
        work_dir=tmp_path,
    )
    assert len(results) == 1
    result = results[0]
    assert result.fixture == "ashby_n1"
    assert result.expected == "review"
    assert result.action == "request_review"
    assert result.target == "_systemfield_why"
    assert result.agrees is True
    assert result.safe is True
    assert any("Why N1?" in item for item in result.required_missing)
