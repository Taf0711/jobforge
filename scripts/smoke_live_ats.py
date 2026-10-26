#!/usr/bin/env python3
"""Live, non-submitting ATS smoke test.

Fills the public Lever demo application with synthetic data, asserts that the
resume uploads and the contact fields fill, and never clicks Submit. This is a
manual release gate, not part of the offline test suite.

Usage:
  python scripts/smoke_live_ats.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

import pymupdf
import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobforge.automation.base import run_application
from jobforge.config import BrowserConfig
from jobforge.models import Job, Preferences, Profile


def _demo_job() -> Job:
    response = requests.get(
        "https://api.lever.co/v0/postings/leverdemo?mode=json", timeout=30
    )
    response.raise_for_status()
    postings = response.json()
    if not isinstance(postings, list) or not postings:
        raise RuntimeError("Lever demo board returned no postings")
    posting = postings[0]
    apply_url = posting.get("applyUrl") or f"{posting.get('hostedUrl', '')}/apply"
    return Job(
        source="lever",
        source_id=str(posting.get("id") or "demo"),
        company="Lever Demo",
        title=str(posting.get("text") or "Demo Role"),
        apply_url=apply_url,
        description="Public Lever demo application fixture.",
    )


def _resume(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text(
        (50, 60),
        "Ada Lovelace\nada@example.com\n+1 555 0100\nSoftware Engineer",
        fontsize=12,
    )
    doc.save(path)
    doc.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--url",
        help="Optional real ATS job URL; default is the public Lever demo board",
    )
    parser.add_argument(
        "--expect-account-wall",
        action="store_true",
        help="Pass only when Workday stops at the account wall without submitting",
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="jobforge-live-smoke-") as raw:
        tmp = Path(raw)
        resume = tmp / "resume.pdf"
        _resume(resume)
        if args.url:
            job = Job(
                source="live-smoke",
                source_id="live-smoke",
                company="Live ATS Smoke",
                title="Live ATS Smoke",
                apply_url=args.url,
                description="Live ATS smoke target.",
            )
        else:
            job = _demo_job()
        profile = Profile(
            first_name="Ada",
            last_name="Lovelace",
            email="ada@example.com",
            phone="+1 555 0100",
            location="London, UK",
            links={"github": "https://github.com/ada"},
            experience=[{"title": "Software Engineer", "company": "Analytical Engines"}],
        )
        report = run_application(
            job,
            profile,
            Preferences(),
            BrowserConfig(headless=True, user_data_dir=str(tmp / "browser")),
            str(resume),
            allow_submit=False,
            hold_seconds=1,
            screenshot_dir=tmp / "screenshots",
        )
        email_filled = any(
            field.category == "email" or "email" in field.label.lower()
            for field in report.fields
        )
        account_wall = any(
            "account wall" in note.lower() for note in report.notes
        )
        checks = {
            "url": job.apply_url,
            "resume_uploaded": report.resume_uploaded,
            "email_filled": email_filled,
            "account_wall": account_wall,
            "submitted": report.submitted,
            "captcha": report.captcha,
            "fields_filled": len(report.fields),
            "notes": report.notes,
            "unresolved": report.unresolved,
        }
        if args.expect_account_wall:
            ok = account_wall and report.submitted is False and not report.captcha
        else:
            ok = (
                report.resume_uploaded
                and email_filled
                and report.submitted is False
                and not report.captcha
            )
        payload = {"ok": ok, "checks": checks}
        print(json.dumps(payload, indent=2) if args.json else payload)
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
