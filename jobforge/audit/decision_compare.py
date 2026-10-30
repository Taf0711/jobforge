"""Compare the decision policy against the current filler on frozen ATS pages.

This is an offline shadow run: it renders each fixture, runs the production
adapter exactly as the supervised flow does, observes the resulting page, and
records what the decision policy would do next. It never clicks Submit.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..automation.answers import AnswerBank
from ..automation.decision_model import (
    ActionKind,
    Decision,
    DecisionError,
    FieldObservation,
    Observation,
    PageKind,
    decide,
)
from ..automation.decision_shadow import record_shadow
from ..automation.observation import observe_page
from ..models import Job, Preferences, Profile
from .adapter_eval import DEFAULT_FIXTURE_DIR, FIXTURE_TARGETS, demo_preferences, demo_profile

DEFAULT_OUT = Path("data/decision_compare_shadow.jsonl")
DEFAULT_VIEWPORT = {"width": 1280, "height": 900}


@dataclass
class CompareResult:
    fixture: str
    ats: str
    page_kind: str
    filled: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    required_missing: list[str] = field(default_factory=list)
    expected: str = ""
    action: str = ""
    rule: str = ""
    target: str = ""
    agrees: bool = False
    safe: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "fixture": self.fixture,
            "ats": self.ats,
            "page_kind": self.page_kind,
            "filled": self.filled,
            "unresolved": self.unresolved,
            "required_missing": self.required_missing,
            "expected": self.expected,
            "action": self.action,
            "rule": self.rule,
            "target": self.target,
            "agrees": self.agrees,
            "safe": self.safe,
            "error": self.error,
        }


def _category(action: ActionKind) -> str:
    if action in {ActionKind.STOP_CAPTCHA, ActionKind.STOP_2FA}:
        return "stop"
    if action is ActionKind.STOP_ACCOUNT_WALL:
        return "account_wall"
    if action in {ActionKind.REQUEST_REVIEW, ActionKind.ASK_HUMAN}:
        return "review"
    if action is ActionKind.SUBMIT:
        return "submit"
    if action is ActionKind.CLICK_NEXT:
        return "next"
    if action is ActionKind.DONE:
        return "done"
    if action in {ActionKind.STOP_QUALITY_GATE, ActionKind.STOP_RISK_GATE}:
        return "gate"
    if action in {
        ActionKind.STOP_UNSUPPORTED,
        ActionKind.STOP_CLOSED,
    }:
        return "stop_other"
    if action in {
        ActionKind.FILL_TEXT,
        ActionKind.SELECT_OPTION,
        ActionKind.CHECK_BOX,
        ActionKind.UPLOAD_FILE,
        ActionKind.CLEAR_FIELD,
    }:
        return "fill"
    return "abstain"


def _expected(report: Any, observation: Observation) -> str:
    if report.captcha or observation.page_kind is PageKind.CAPTCHA:
        return "stop"
    if observation.page_kind is PageKind.ACCOUNT_WALL:
        return "account_wall"
    if report.required_missing:
        return "review"
    if observation.submit_button is not None:
        return "submit"
    if observation.next_button is not None:
        return "next"
    return "abstain"


def _target_field(observation: Observation, field_id: str) -> FieldObservation | None:
    for item in observation.fields:
        if item.field_id == field_id:
            return item
    return None


def _safety(decision: Decision, observation: Observation) -> tuple[bool, str]:
    if decision.action is ActionKind.SUBMIT:
        try:
            decision.validate_against(observation)
        except DecisionError as exc:
            return False, str(exc)
    if decision.action in {
        ActionKind.FILL_TEXT,
        ActionKind.SELECT_OPTION,
        ActionKind.CHECK_BOX,
        ActionKind.UPLOAD_FILE,
    }:
        target = _target_field(observation, decision.target)
        if target is None:
            return False, "fill target is not in the observation"
        if not target.grounded_for_fill:
            return False, "fill target has no grounded value"
    if decision.action is ActionKind.STOP_CAPTCHA and not observation.captcha:
        return False, "stop_captcha without a captcha observation"
    return True, ""


def compare_fixture(
    fixture_path: str | Path,
    *,
    profile: Profile,
    prefs: Preferences,
    work_dir: str | Path,
    shadow_out: str | Path | None = DEFAULT_OUT,
) -> CompareResult:
    source = Path(fixture_path)
    ats, url_hint = FIXTURE_TARGETS.get(
        source.stem.lower(), (source.stem.lower(), "https://example.com/apply")
    )
    result = CompareResult(fixture=source.stem, ats=ats, page_kind="")
    try:
        from playwright.sync_api import sync_playwright

        from ..automation.base import FieldReport, FillReport, get_adapter

        html = source.read_text(encoding="utf-8")
        work = Path(work_dir)
        work.mkdir(parents=True, exist_ok=True)
        resume = work / f"resume-{source.stem}.pdf"
        resume.write_bytes(b"%PDF-1.4\n% decision compare placeholder\n")
        job = Job(
            source=ats,
            source_id=source.stem,
            company="Acme",
            title="Software Engineer",
            url=url_hint,
            apply_url=url_hint,
        )
        adapter = get_adapter(url_hint)
        bank = AnswerBank(None)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport=DEFAULT_VIEWPORT)
                page.set_content(html, wait_until="domcontentloaded", timeout=5000)
                report = adapter.fill(page, job, profile, prefs, str(resume), bank)
                from ..automation.detect import captcha_present

                report.captcha = captcha_present(page)
                observation = observe_page(
                    page,
                    job=job,
                    profile=profile,
                    prefs=prefs,
                    bank=bank,
                    ats=ats,
                    resume_path=str(resume),
                    quality_ready=True,
                    risk_allowed=True,
                )
                decision = decide(observation)
                result.page_kind = observation.page_kind.value
                result.filled = [item.label for item in report.fields if item.status == "filled"]
                result.unresolved = list(report.unresolved)
                result.required_missing = list(report.required_missing)
                result.expected = _expected(report, observation)
                result.action = decision.action.value
                result.rule = decision.policy_rule
                result.target = decision.target
                result.agrees = _category(decision.action) == result.expected
                safe, reason = _safety(decision, observation)
                result.safe = safe
                if not safe:
                    result.error = reason
                if shadow_out:
                    record_shadow(
                        shadow_out,
                        observation,
                        decision,
                        source="fixture",
                        job_id=None,
                    )
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 - surfaced as a compare failure
        result.error = f"{type(exc).__name__}: {exc}"
        result.safe = False
    return result


def compare_all(
    fixture_dir: str | Path = DEFAULT_FIXTURE_DIR,
    *,
    shadow_out: str | Path | None = DEFAULT_OUT,
    profile: Profile | None = None,
    prefs: Preferences | None = None,
    work_dir: str | Path | None = None,
) -> list[CompareResult]:
    profile = profile or demo_profile()
    prefs = prefs or demo_preferences()
    with tempfile.TemporaryDirectory(prefix="jobforge-decision-compare-") as temp:
        work = Path(work_dir) if work_dir else Path(temp)
        results = []
        for path in sorted(Path(fixture_dir).glob("*.html")):
            results.append(
                compare_fixture(
                    path,
                    profile=profile,
                    prefs=prefs,
                    work_dir=work,
                    shadow_out=shadow_out,
                )
            )
        return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare the decision policy to the current filler.")
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURE_DIR)
    parser.add_argument(
        "--out",
        type=str,
        default=str(DEFAULT_OUT),
        help="shadow JSONL output; empty disables",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    out = Path(args.out) if args.out else None
    results = compare_all(args.fixtures, shadow_out=out)
    ok = bool(results) and all(result.safe for result in results)
    payload = {
        "ok": ok,
        "count": len(results),
        "agreements": sum(1 for result in results if result.agrees),
        "results": [result.to_dict() for result in results],
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        for result in results:
            status = "ok" if result.safe else "UNSAFE"
            agree = "agree" if result.agrees else "DIFF"
            print(
                f"{status:6} {agree:5} {result.fixture:16} "
                f"expected={result.expected:12} action={result.action:20} "
                f"target={result.target}"
            )
        print(f"{payload['agreements']}/{payload['count']} agreements; safe={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
