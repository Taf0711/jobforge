from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobforge.automation.answers import AnswerBank
from jobforge.models import Preferences, Profile


def _bank(tmp_path: Path, overrides: dict[str, str]) -> AnswerBank:
    path = tmp_path / "answers.json"
    path.write_text(json.dumps(overrides), encoding="utf-8")
    return AnswerBank(path)


def test_short_override_cannot_answer_sensitive_questions(tmp_path: Path):
    bank = _bank(tmp_path, {"a": "No"})
    profile = Profile(work_auth={"authorized": True})

    assert bank.answer("Veteran status", profile, Preferences()).value == ""
    assert bank.answer("Race", profile, Preferences()).value == ""
    assert bank.answer("Gender", profile, Preferences()).value == ""
    assert bank.answer("Do you have a disability?", profile, Preferences()).value == ""
    assert bank.answer("Are you authorized to work?", profile, Preferences()).value == "Yes"
    assert bank.answer("Do you require visa sponsorship?", profile, Preferences()).value == "No"


@pytest.mark.parametrize(
    "question",
    [
        "Do you have the right to work in the US?",
        "Do you have authorization to work in the EU?",
        "Are you work authorized?",
        "Are you legally permitted to work in the US?",
        "Are you a US citizen?",
        "What is your date of birth?",
        "Are you Hispanic or Latino?",
        "What is your national origin?",
        "What is your military status?",
        "What is your marital status?",
        "What is your sex?",
        "Work_authorization",
        "work_authorization_status",
        "veteran_status",
        "date_of_birth",
        "security_clearance",
        "race_ethnicity",
        "sexual_orientation",
    ],
)
def test_common_sensitive_phrasings_ignore_overrides(tmp_path: Path, question: str):
    bank = _bank(tmp_path, {"a": "No"})
    answer = bank.answer(question, Profile(), Preferences())
    assert answer.value == ""
    assert answer.category != "override"


@pytest.mark.parametrize(
    "question",
    [
        "Do you have right-to-work status?",
        "Are you eligible for employment?",
        "Are you authorized for employment?",
    ],
)
def test_work_auth_phrasings_cannot_be_overridden(tmp_path: Path, question: str):
    bank = _bank(tmp_path, {"a": "Yes"})
    profile = Profile(work_auth={"authorized": False})
    answer = bank.answer(question, profile, Preferences())
    assert answer.value == "No"
    assert answer.category == "work_authorization"


@pytest.mark.parametrize(
    "value,expected",
    [
        (False, "No"),
        (True, "Yes"),
        (0, "No"),
        (1, "Yes"),
        ("No", "No"),
        ("false", "No"),
        ("0", "No"),
        ("Yes", "Yes"),
        ("true", "Yes"),
        ("1", "Yes"),
    ],
)
def test_work_authorization_boolean_coercion(tmp_path: Path, value, expected: str):
    bank = _bank(tmp_path, {})
    profile = Profile(work_auth={"authorized": value})
    assert bank.answer("Are you authorized to work?", profile, Preferences()).value == expected


def test_work_authorization_and_clearance_require_explicit_profile_values(tmp_path: Path):
    bank = _bank(tmp_path, {"authorized to work": "No", "clearance": "Secret"})
    empty = Profile()

    assert bank.answer("Are you authorized to work?", empty, Preferences()).value == ""
    assert bank.answer("Do you hold a security clearance?", empty, Preferences()).value == ""

    explicit = Profile(work_auth={"authorized": False, "clearance": "Secret"})
    assert bank.answer("Are you authorized to work?", explicit, Preferences()).value == "No"
    assert bank.answer("Do you hold a security clearance?", explicit, Preferences()).value == "Secret"


def test_normal_override_still_wins(tmp_path: Path):
    bank = _bank(tmp_path, {"notice period": "1 month"})
    profile = Profile()

    answer = bank.answer("What is your notice period?", profile, Preferences())

    assert answer.value == "1 month"
    assert answer.category == "override"
