from __future__ import annotations

from datetime import date

from jobforge.matching.experience import coerce_years, estimate_years, resolve_years
from jobforge.models import Preferences, Profile


def test_estimate_years_merges_overlaps_and_ignores_internships():
    entries = [
        {"title": "Software Engineer", "start": "2023-03", "end": "2026-06"},
        {"title": "Software Engineering Intern", "start": "2022-01", "end": "2022-03"},
        {"title": "Software Engineer", "start": "2026-06", "end": "2026-09"},
        {"title": "Software Engineer", "start": "2026-10", "end": "Present"},
    ]

    assert estimate_years(entries, today=date(2026, 11, 1)) == 3.6


def test_estimate_years_does_not_double_count_overlapping_roles():
    entries = [
        {"title": "Engineer", "start": "2024-01", "end": "2025-01"},
        {"title": "Consultant", "start": "2024-06", "end": "2024-12"},
    ]

    assert estimate_years(entries, today=date(2026, 11, 1)) == 1.0


def test_estimate_years_returns_unknown_without_dated_roles():
    assert estimate_years([{"title": "Engineer", "start": "", "end": ""}]) is None
    assert estimate_years([]) is None


def test_resolve_years_priority_and_unknown():
    experience = [{"title": "Engineer", "start": "2023-01", "end": "2024-01"}]
    explicit_profile = Profile(work_auth={"years_experience": 4}, experience=experience)
    configured = Preferences(years_of_experience=2, years_experience_confirmed=True)
    assert resolve_years(explicit_profile, configured, today=date(2024, 2, 1)).source == "profile"
    assert resolve_years(explicit_profile, configured, today=date(2024, 2, 1)).value == 4.0

    profile = Profile(experience=experience)
    estimate = resolve_years(profile, configured, today=date(2024, 2, 1))
    assert estimate.source == "preferences"
    assert estimate.value == 2.0

    legacy = Preferences(years_of_experience=3)
    assert resolve_years(profile, legacy, today=date(2024, 2, 1)).source == "preferences"

    derived = resolve_years(profile, Preferences(), today=date(2024, 2, 1))
    assert derived.source == "experience"
    assert derived.value == 1.0

    unknown = resolve_years(Profile(), Preferences(), today=date(2024, 2, 1))
    assert unknown.value is None
    assert unknown.source == "unknown"


def test_explicit_zero_years_is_known_but_legacy_zero_is_not():
    profile = Profile()
    explicit_zero = Preferences(years_of_experience=0, years_experience_confirmed=True)
    assert resolve_years(profile, explicit_zero).value == 0.0
    assert resolve_years(profile, explicit_zero).source == "preferences"

    legacy_zero = Preferences(years_of_experience=0)
    assert resolve_years(profile, legacy_zero).value is None
    assert resolve_years(profile, legacy_zero).source == "unknown"


def test_coerce_years_rejects_invalid_values():
    assert coerce_years("3.5") == 3.5
    assert coerce_years("") is None
    assert coerce_years(None) is None
    assert coerce_years(-1) is None
    assert coerce_years("not-a-number") is None
