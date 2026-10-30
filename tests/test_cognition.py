from __future__ import annotations

from jobforge.automation.answers import AnswerBank
from jobforge.automation.cognition import classify_missing, classify_question
from jobforge.models import Preferences, Profile


def test_classifier_routes_profile_fields_and_review_gaps():
    profile = Profile(
        first_name="Ada",
        last_name="Lovelace",
        email="ada@example.com",
        location="New York, NY",
        work_auth={"authorized": True},
        disclosures={},
    )
    prefs = Preferences()
    bank = AnswerBank()

    assert classify_question("First name", profile, prefs, bank).action == "fill"
    location = classify_question("Location", profile, prefs, bank)
    assert location.category == "location"
    assert location.action == "fill"

    motivation = classify_question("Why N1?", profile, prefs, bank)
    assert motivation.category == "motivation"
    assert motivation.action == "review"

    veteran = classify_question("Veteran status", profile, prefs, bank)
    assert veteran.category == "veteran"
    assert veteran.action == "review"

    unknown = classify_question("Twitter URL", profile, prefs, bank)
    assert unknown.action == "review"


def test_classify_missing_deduplicates_and_strips_suffixes():
    profile = Profile(location="New York, NY")
    bank = AnswerBank()
    decisions = classify_missing(
        ["Why N1? (required field left empty)", "Why N1?", "Location"],
        profile,
        Preferences(),
        bank,
    )
    labels = [item["label"] for item in decisions]
    assert labels.count("Why N1?") == 1
    assert any(item["label"] == "Location" and item["action"] == "fill" for item in decisions)
