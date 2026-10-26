"""Deterministic offline regression tests for the ATS form fillers."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from jobforge.audit.adapter_eval import (
    EvalSpec,
    demo_preferences,
    demo_profile,
    evaluate,
    evaluate_all,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "ats"
REPO_ROOT = Path(__file__).resolve().parent.parent


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


def test_all_ats_fixtures_fill_and_hold(tmp_path):
    _require_chromium()
    results = evaluate_all(
        FIXTURE_DIR, demo_profile(), demo_preferences(), tmp_path
    )

    assert len(results) == 6
    assert {result.ats for result in results} == {
        "lever",
        "greenhouse",
        "ashby",
        "workable",
        "smartrecruiters",
        "workday",
    }
    for result in results:
        assert result.ok, result.to_dict()
        assert result.error == ""
        assert result.submitted is False
        assert result.captcha == ""
        joined = " | ".join(result.unresolved).lower()
        assert "why do you want to work here?" in joined
        assert "gender" in joined or "veteran" in joined


def test_wrong_expected_value_fails(tmp_path):
    _require_chromium()
    spec = EvalSpec(
        name="lever-wrong-email",
        ats="lever",
        url_hint="https://jobs.lever.co/acme/123/apply",
        fixture_path=str(FIXTURE_DIR / "lever.html"),
        expected_filled={"Email": "not-the-profile@example.com"},
        expected_unresolved=["Why do you want to work here?", "Gender"],
    )

    result = evaluate(spec, demo_profile(), demo_preferences(), tmp_path)

    assert result.ok is False
    assert result.error == ""
    assert any(
        check["name"].startswith("field:") and not check["passed"]
        for check in result.checks
    )


def test_captcha_fixture_fails_the_gate(tmp_path):
    _require_chromium()
    source = (FIXTURE_DIR / "lever.html").read_text(encoding="utf-8")
    fixture = tmp_path / "lever.html"
    fixture.write_text(
        source.replace(
            "</body>",
            '<div class="g-recaptcha" style="display:block;width:20px;height:20px">captcha</div></body>',
        ),
        encoding="utf-8",
    )
    spec = EvalSpec(
        name="lever-captcha",
        ats="lever",
        url_hint="https://jobs.lever.co/acme/123/apply",
        fixture_path=str(fixture),
        expected_filled={},
        expected_unresolved=[],
    )

    result = evaluate(spec, demo_profile(), demo_preferences(), tmp_path)

    assert result.ok is False
    assert result.captcha != ""
    assert any(
        check["name"] == "captcha" and not check["passed"]
        for check in result.checks
    )


def test_cli_prints_one_json_document():
    _require_chromium()
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "jobforge.audit.adapter_eval",
            "--all",
            "--json",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert len(payload["results"]) == 6
    assert all(result["submitted"] is False for result in payload["results"])
