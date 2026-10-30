from __future__ import annotations

import json
from pathlib import Path

from jobforge.audit.decision_eval import evaluate_all, load_case, main

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "decisions"


def test_all_frozen_decision_cases_pass():
    results = evaluate_all(FIXTURE_DIR)
    assert len(results) >= 13
    assert all(result.ok for result in results), [
        result.to_dict() for result in results if not result.ok
    ]
    assert any(result.action == "submit" for result in results)
    assert any(result.action == "stop_captcha" for result in results)
    assert any(result.action == "request_review" for result in results)


def test_wrong_expectation_fails_the_case(tmp_path: Path):
    source = FIXTURE_DIR / "greenhouse_captcha.json"
    data = json.loads(source.read_text(encoding="utf-8"))
    data["expected"] = {"action": "submit", "rule": "submit_ready", "target": "submit"}
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps(data), encoding="utf-8")

    result = evaluate_all(tmp_path)[0]
    assert result.ok is False
    assert any(check["name"] == "action" and not check["passed"] for check in result.checks)


def test_replay_cli_emits_json(capsys):
    code = main(["--fixtures", str(FIXTURE_DIR), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True
    assert payload["passed"] == payload["count"]


def test_submit_case_has_no_unsafe_flag():
    case = load_case(FIXTURE_DIR / "ashby_n1_submit_ready.json")
    from jobforge.audit.decision_eval import evaluate_case

    result = evaluate_case(case)
    assert result.ok is True
    assert result.action == "submit"
    assert all(check["passed"] for check in result.checks)
