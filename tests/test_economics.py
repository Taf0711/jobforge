from __future__ import annotations

from jobforge.matching.economics import (
    comp_policy,
    is_nyc_role,
    location_economics,
    parse_salary_range,
)


def test_nyc_comp_policy_meets_target_and_exception_band():
    meets = comp_policy(True, 145_000, 155_000)
    assert meets["verdict"] == "meets_target"
    assert meets["modifier"] == 2

    exception = comp_policy(True, 110_000, 120_000, channel="cold_portal")
    assert exception["verdict"] == "exception_unlikely"
    assert exception["modifier"] == -12

    warm = comp_policy(True, 110_000, 120_000, channel="referral")
    assert warm["verdict"] == "exception_ok"
    assert warm["modifier"] == 0


def test_nyc_comp_policy_below_floor_and_missing_band():
    below = comp_policy(True, 70_000, 90_000)
    assert below["verdict"] == "below_floor"
    assert below["modifier"] == -20

    verify = comp_policy(True, None, None)
    assert verify["verdict"] == "verify_comp"
    assert verify["modifier"] == -5

    non_nyc = comp_policy(False, 70_000, 90_000)
    assert non_nyc["verdict"] == "not_applicable"
    assert non_nyc["modifier"] == 0


def test_location_economics_and_salary_parsing():
    assert is_nyc_role("New York, NY")
    assert parse_salary_range("$145,000 - $155,000/year") == (145_000.0, 155_000.0)

    mod, notes = location_economics("San Francisco, CA", 120_000, 0.55)
    assert mod == -2
    assert notes

    remote_mod, _ = location_economics("New York, NY", 120_000, 1.0)
    assert remote_mod == 0
