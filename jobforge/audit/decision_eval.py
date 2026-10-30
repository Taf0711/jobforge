"""Offline replay for the decision contract.

Each JSON case is a frozen observation plus the expected action and policy
rule. The harness is pure Python: no browser, provider, database, or network.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..automation.decision_model import (
    ActionKind,
    ButtonObservation,
    Decision,
    DecisionError,
    FieldCategory,
    FieldKind,
    FieldObservation,
    Observation,
    PageKind,
    ValueSource,
    decide,
)

DEFAULT_FIXTURE_DIR = Path("tests/fixtures/decisions")


@dataclass
class DecisionCase:
    name: str
    observation: Observation
    expected_action: ActionKind
    expected_rule: str = ""
    expected_target: str = ""
    expected_requires_human: bool | None = None
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "expected_action": self.expected_action.value,
            "expected_rule": self.expected_rule,
            "expected_target": self.expected_target,
            "source": self.source,
        }


@dataclass
class CaseResult:
    name: str
    ok: bool
    action: str
    rule: str
    target: str
    checks: list[dict[str, Any]]
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "action": self.action,
            "rule": self.rule,
            "target": self.target,
            "checks": self.checks,
            "error": self.error,
        }


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "detail": detail}


def _enum(value: Any, enum_type, field_name: str):
    try:
        return enum_type(str(value))
    except ValueError as exc:
        raise DecisionError(f"{field_name}: unknown value {value!r}") from exc


def _field_from_dict(data: dict[str, Any]) -> FieldObservation:
    return FieldObservation(
        field_id=str(data.get("field_id") or ""),
        label=str(data.get("label") or ""),
        kind=_enum(data.get("kind", "text"), FieldKind, "field.kind"),
        required=bool(data.get("required", False)),
        visible=bool(data.get("visible", True)),
        enabled=bool(data.get("enabled", True)),
        options=tuple(str(item) for item in data.get("options") or []),
        value=str(data.get("value") or ""),
        category=_enum(data.get("category", "unknown"), FieldCategory, "field.category"),
        sensitive=bool(data.get("sensitive", False)),
        has_grounded_answer=bool(data.get("has_grounded_answer", False)),
        answer_value=str(data.get("answer_value") or ""),
        value_source=_enum(data.get("value_source", "none"), ValueSource, "field.value_source"),
        error=str(data.get("error") or ""),
    )


def _button_from_dict(data: dict[str, Any]) -> ButtonObservation:
    return ButtonObservation(
        button_id=str(data.get("button_id") or ""),
        label=str(data.get("label") or ""),
        enabled=bool(data.get("enabled", True)),
        is_submit=bool(data.get("is_submit", False)),
        is_next=bool(data.get("is_next", False)),
        is_back=bool(data.get("is_back", False)),
    )


def observation_from_dict(data: dict[str, Any]) -> Observation:
    return Observation(
        page_kind=_enum(data.get("page_kind", "unknown"), PageKind, "page_kind"),
        ats=str(data.get("ats") or ""),
        url=str(data.get("url") or ""),
        step=int(data.get("step", 0)),
        fields=tuple(_field_from_dict(item) for item in data.get("fields") or []),
        buttons=tuple(_button_from_dict(item) for item in data.get("buttons") or []),
        validation_errors=tuple(str(item) for item in data.get("validation_errors") or []),
        captcha=bool(data.get("captcha", False)),
        two_factor=bool(data.get("two_factor", False)),
        account_wall=bool(data.get("account_wall", False)),
        # Default to false: a replay case must opt into a gate rather than
        # accidentally authorizing submit.
        quality_ready=bool(data.get("quality_ready", False)),
        risk_allowed=bool(data.get("risk_allowed", False)),
        job_age_days=(
            int(data["job_age_days"]) if data.get("job_age_days") is not None else None
        ),
        application_id=str(data.get("application_id") or ""),
        attempt=int(data.get("attempt", 0)),
        confidence=float(data.get("confidence", 1.0)),
        state_hash=str(data.get("state_hash") or ""),
    )


def load_case(path: str | Path) -> DecisionCase:
    source = Path(path)
    data = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise DecisionError(f"{source}: root must be an object")
    name = str(data.get("name") or source.stem)
    expected = data.get("expected") or {}
    if not isinstance(expected, dict):
        raise DecisionError(f"{source}: expected must be an object")
    observation = observation_from_dict(data.get("observation") or {})
    return DecisionCase(
        name=name,
        observation=observation,
        expected_action=_enum(expected.get("action"), ActionKind, "expected.action"),
        expected_rule=str(expected.get("rule") or ""),
        expected_target=str(expected.get("target") or ""),
        expected_requires_human=(
            bool(expected["requires_human"])
            if "requires_human" in expected
            else None
        ),
        source=str(source),
    )


def evaluate_case(case: DecisionCase) -> CaseResult:
    checks: list[dict[str, Any]] = []
    try:
        decision = decide(case.observation)
    except Exception as exc:  # noqa: BLE001 - surfaced as a case failure
        return CaseResult(
            name=case.name,
            ok=False,
            action="",
            rule="",
            target="",
            checks=[_check("decision", False, f"{type(exc).__name__}: {exc}")],
            error=f"{type(exc).__name__}: {exc}",
        )

    checks.append(
        _check(
            "action",
            decision.action is case.expected_action,
            f"got {decision.action.value!r}, expected {case.expected_action.value!r}",
        )
    )
    if case.expected_rule:
        checks.append(
            _check(
                "rule",
                decision.policy_rule == case.expected_rule,
                f"got {decision.policy_rule!r}, expected {case.expected_rule!r}",
            )
        )
    if case.expected_target:
        checks.append(
            _check(
                "target",
                decision.target == case.expected_target,
                f"got {decision.target!r}, expected {case.expected_target!r}",
            )
        )
    if case.expected_requires_human is not None:
        checks.append(
            _check(
                "requires_human",
                decision.requires_human is case.expected_requires_human,
                f"got {decision.requires_human!r}, expected {case.expected_requires_human!r}",
            )
        )
    checks.append(_check("rationale", bool(decision.rationale.strip()), decision.rationale))
    checks.append(
        _check("policy_rule", bool(decision.policy_rule.strip()), decision.policy_rule)
    )

    if decision.action is ActionKind.SUBMIT:
        try:
            decision.validate_against(case.observation)
            checks.append(_check("submit_gate", True, "all submit preconditions hold"))
        except DecisionError as exc:
            checks.append(_check("submit_gate", False, str(exc)))
    else:
        checks.append(_check("submit_gate", True, "not a submit decision"))

    # Explicit unsafe-action tripwire. The decision constructor already blocks
    # model-draft fills; this catches an accidental submit-shaped fixture.
    if decision.action is ActionKind.SUBMIT:
        safe = (
            case.observation.page_kind is PageKind.APPLICATION_FORM
            and case.observation.risk_allowed
            and case.observation.quality_ready
            and not case.observation.required_missing
            and not case.observation.validation_errors
            and not case.observation.required_review
        )
        checks.append(_check("unsafe_submit", safe, "submit gate tripwire"))

    ok = all(check["passed"] for check in checks)
    return CaseResult(
        name=case.name,
        ok=ok,
        action=decision.action.value,
        rule=decision.policy_rule,
        target=decision.target,
        checks=checks,
        error="",
    )


def evaluate_all(fixture_dir: str | Path = DEFAULT_FIXTURE_DIR) -> list[CaseResult]:
    paths = sorted(Path(fixture_dir).glob("*.json"))
    return [evaluate_case(load_case(path)) for path in paths]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay frozen decision cases.")
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=DEFAULT_FIXTURE_DIR,
        help="directory of decision-case JSON files",
    )
    parser.add_argument("--json", action="store_true", help="emit the full JSON verdict")
    args = parser.parse_args(argv)

    results = evaluate_all(args.fixtures)
    ok = bool(results) and all(result.ok for result in results)
    payload = {
        "ok": ok,
        "count": len(results),
        "passed": sum(1 for result in results if result.ok),
        "cases": [result.to_dict() for result in results],
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        for result in results:
            status = "ok" if result.ok else "FAIL"
            print(f"{status:4} {result.name:40} {result.action:20} {result.rule}")
        print(f"{payload['passed']}/{payload['count']} decision cases passed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
