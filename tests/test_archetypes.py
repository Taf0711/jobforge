from __future__ import annotations

from jobforge.matching.archetypes import detect_archetype, load_archetypes
from jobforge.matching.jd import parse_jd
from jobforge.models import Profile
from jobforge.tailoring.tailor import select_projects


ARCHETYPES = {
    "systems_networking_embedded": {
        "label": "Systems / low-level",
        "jd_signals": ["C", "C++", "Linux", "embedded", "networking", "TCP/IP"],
        "strongest_projects": ["N-Body Galaxy Simulator", "TCP/IP Network Stack", "Edge-Cloud Task Scheduler"],
    },
    "backend_platform": {
        "label": "Backend / platform",
        "jd_signals": ["backend", "platform", "API", "microservices", "databases", "Python", "Java"],
        "strongest_projects": ["Splice", "JIT Access & Policy Gateway", "Edge-Cloud Task Scheduler"],
    },
}


def test_detect_systems_archetype_from_query_engine_jd():
    jd = parse_jd(
        "We build query engines. Requirements: C++, Rust, distributed systems, operating systems.",
        title_hint="Software Engineer, New Grad - Query Engines",
    )

    match = detect_archetype(jd, ARCHETYPES)

    assert match is not None
    assert match.name == "systems_networking_embedded"
    assert match.score > 0


def test_select_projects_uses_archetype_priority():
    profile = Profile(
        name="test",
        projects=[
            {"title": "Splice", "name": "Splice", "tags": ["Go", "AI"]},
            {"title": "N-Body Galaxy Simulator", "name": "N-Body Galaxy Simulator", "tags": ["C++", "systems"]},
            {"title": "TCP/IP Network Stack", "name": "TCP/IP Network Stack", "tags": ["C", "networking"]},
            {"title": "Edge-Cloud Task Scheduler", "name": "Edge-Cloud Task Scheduler", "tags": ["Python", "systems"]},
        ],
    )
    jd = parse_jd(
        "We build query engines. Requirements: C++, Rust, distributed systems.",
        title_hint="Software Engineer, New Grad - Query Engines",
    )

    selected = select_projects(profile, jd, archetypes=ARCHETYPES)

    assert [item["name"] for item in selected] == [
        "N-Body Galaxy Simulator",
        "TCP/IP Network Stack",
        "Edge-Cloud Task Scheduler",
    ]


def test_load_archetypes_accepts_wrapped_and_bare_mappings(tmp_path):
    wrapped = tmp_path / "wrapped.yml"
    wrapped.write_text(
        "archetypes:\n  systems:\n    label: Systems\n    jd_signals: [C++]\n    strongest_projects: [N-Body]\n",
        encoding="utf-8",
    )
    bare = tmp_path / "bare.yml"
    bare.write_text(
        "systems:\n  label: Systems\n  jd_signals: [C++]\n  strongest_projects: [N-Body]\n",
        encoding="utf-8",
    )

    assert load_archetypes(wrapped)["systems"]["label"] == "Systems"
    assert load_archetypes(bare)["systems"]["label"] == "Systems"
