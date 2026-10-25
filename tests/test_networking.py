from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from jobforge.networking import (
    NetworkGraph,
    WarmPath,
    channel,
    modify_fit,
    normalize_company,
)

SAMPLE_PATH = Path(__file__).resolve().parents[1] / "network.example.yml"
TODAY = date(2026, 10, 24)


def _graph(tmp_path: Path, contacts: list[dict]) -> NetworkGraph:
    path = tmp_path / "network.yml"
    path.write_text(json.dumps({"connections": contacts}), encoding="utf-8")
    return NetworkGraph(path)


def _contact(**overrides) -> dict:
    contact = {
        "name": "Alex Rivera",
        "company": "Acme, Inc.",
        "role": "Engineer",
        "relationship": "former_colleague",
        "strength": "medium",
        "last_contact": "",
        "notes": "",
    }
    contact.update(overrides)
    return contact


def test_loads_sample_yaml():
    graph = NetworkGraph(SAMPLE_PATH)

    assert graph.warnings == []
    assert len(graph.connections) == 3
    assert {contact["name"] for contact in graph.connections} == {
        "Dana Whitfield",
        "Priya Raman",
        "Marcus Lee",
    }
    assert graph.warm_path("Northwind Robotics").score > 0
    assert graph.warm_path("Acme Analytics").score > 0
    assert graph.warm_path("Beacon Health").score > 0


def test_normalize_company_strips_suffixes_and_punctuation():
    assert normalize_company("Acme, Inc.") == "acme"
    assert normalize_company("Acme") == "acme"
    assert normalize_company("Acme Technologies LLC") == "acme"
    assert normalize_company("  ACME   Software Systems, Inc. ") == "acme"
    assert normalize_company("Acme Corporation") == normalize_company("Acme Co.")
    assert normalize_company("Acme Labs") == "acme"


def test_contacts_sorted_by_strength_then_recency(tmp_path):
    graph = _graph(
        tmp_path,
        [
            _contact(name="Weak Recent", strength="weak", last_contact="2026-10-01"),
            _contact(name="Strong Old", strength="strong", last_contact="2024-01-01"),
            _contact(name="Medium", strength="medium", last_contact="2026-09-01"),
            _contact(name="Strong New", strength="strong", last_contact="2026-10-20"),
        ],
    )

    names = [contact["name"] for contact in graph.contacts_for("Acme Technologies LLC")]
    assert names == ["Strong New", "Strong Old", "Medium", "Weak Recent"]
    assert graph.contacts_for("Other Co") == []


def test_role_bonuses(tmp_path):
    # Medium base is 0.6 and these contacts have no recency bonus.
    hiring = _graph(
        tmp_path, [_contact(role="Hiring Manager", relationship="former_colleague")]
    ).warm_path("Acme", TODAY)
    recruiter = _graph(
        tmp_path, [_contact(role="Technical Recruiter", relationship="other")]
    ).warm_path("Acme", TODAY)
    alumni = _graph(
        tmp_path, [_contact(role="Engineer", relationship="alumni")]
    ).warm_path("Acme", TODAY)
    plain = _graph(
        tmp_path, [_contact(role="Engineer", relationship="friend")]
    ).warm_path("Acme", TODAY)

    assert hiring.score == 75
    assert recruiter.score == 70
    assert alumni.score == 65
    assert plain.score == 60


def test_recency_bonus_and_cap(tmp_path):
    graph = _graph(
        tmp_path,
        [
            _contact(name="Recent", company="Recent Co", last_contact=(TODAY - timedelta(days=30)).isoformat()),
            _contact(name="Within Year", company="Year Co", last_contact=(TODAY - timedelta(days=200)).isoformat()),
            _contact(name="Old", company="Old Co", last_contact=(TODAY - timedelta(days=800)).isoformat()),
            _contact(name="Missing", company="Missing Co"),
            _contact(
                name="Capped",
                company="Capped Co",
                strength="strong",
                role="Hiring Manager",
                last_contact=(TODAY - timedelta(days=5)).isoformat(),
            ),
        ],
    )

    assert graph.warm_path("Recent Co", TODAY).score == 70
    assert graph.warm_path("Year Co", TODAY).score == 65
    assert graph.warm_path("Old Co", TODAY).score == 60
    assert graph.warm_path("Missing Co", TODAY).score == 60
    # 1.0 base + 0.15 role + 0.10 recency would exceed 1.0.
    assert graph.warm_path("Capped Co", TODAY).score == 100


def test_no_contacts_returns_cold_path(tmp_path):
    graph = _graph(tmp_path, [])
    warm = graph.warm_path("Nobody Inc")

    assert warm.score == 0
    assert warm.contacts == []
    assert warm.action == "cold_only"
    assert warm.reason


@pytest.mark.parametrize(
    ("fit", "warm", "expected"),
    [
        (80, 80, "referral_first"),
        (80, 50, "warm_intro"),
        (80, 10, "cold_with_warm_fallback"),
        (80, 0, "cold_only"),
        (60, 80, "warm_intro"),
        (60, 50, "warm_intro"),
        (60, 10, "cold_with_warm_fallback"),
        (60, 0, "cold_only"),
        (39, 100, "cold_only"),
    ],
)
def test_channel_matrix(fit, warm, expected):
    warm_path = WarmPath(
        company="acme",
        score=warm,
        contacts=[],
        reason="test",
        action="cold_only",
    )
    assert channel(fit, warm_path) == expected


def test_modify_fit_bounds_and_delta_cap():
    warm_80 = WarmPath("acme", 80, [], "test", "referral_first")
    warm_100 = WarmPath("acme", 100, [], "test", "referral_first")

    adjusted, delta, reason = modify_fit(60, warm_80)
    assert adjusted == pytest.approx(72.0)
    assert delta == pytest.approx(12.0)
    assert reason

    adjusted, delta, _ = modify_fit(95, warm_80)
    assert adjusted == pytest.approx(100.0)
    assert delta == pytest.approx(5.0)

    adjusted, delta, _ = modify_fit(0, warm_100)
    assert adjusted == pytest.approx(15.0)
    assert delta == pytest.approx(15.0)

    adjusted, delta, _ = modify_fit(100, warm_100)
    assert adjusted == 100
    assert delta == 0

    for fit in (0, 40, 95, 100):
        for warm in (0, 10, 50, 100):
            path = WarmPath("acme", warm, [], "test", "cold_only")
            adjusted, delta, _ = modify_fit(fit, path)
            assert 0 <= adjusted <= 100
            assert delta <= 15
            assert adjusted == pytest.approx(min(100.0, fit + 0.15 * warm))


def test_weak_contact_does_not_rescue_poor_fit(tmp_path):
    graph = _graph(
        tmp_path,
        [_contact(strength="weak", role="Engineer", relationship="friend")],
    )
    warm = graph.warm_path("Acme", TODAY)
    assert warm.score == 30

    adjusted, delta, _ = modify_fit(35, warm)
    assert adjusted == pytest.approx(39.5)
    assert adjusted < 50
    assert delta <= 4.5


def test_malformed_yaml_returns_empty_graph_with_warnings(tmp_path):
    path = tmp_path / "network.yml"
    path.write_text("connections: [\n  - name: broken\n", encoding="utf-8")

    graph = NetworkGraph(path)

    assert graph.connections == []
    assert graph.warnings
    assert graph.warm_path("Acme").score == 0


def test_malformed_entries_are_skipped_with_warnings(tmp_path):
    path = tmp_path / "network.yml"
    path.write_text(
        """
connections:
  - name: Valid Person
    company: Acme Inc
    relationship: friend
    strength: weak
  - company: Missing Name
    relationship: friend
    strength: weak
  - name: Bad Strength
    company: Acme
    relationship: friend
    strength: super-strong
  - just a string
  - name: Bad Relationship
    company: Acme
    relationship: vendor
    strength: weak
  - name: Bad Date
    company: Acme
    relationship: friend
    strength: weak
    last_contact: someday
""",
        encoding="utf-8",
    )

    graph = NetworkGraph(path)

    assert [contact["name"] for contact in graph.connections] == ["Valid Person"]
    assert len(graph.warnings) == 5


def test_loads_json_file(tmp_path):
    path = tmp_path / "network.json"
    path.write_text(json.dumps({"connections": [_contact()]}), encoding="utf-8")

    graph = NetworkGraph(path)

    assert graph.warnings == []
    assert graph.warm_path("Acme").score == 60
