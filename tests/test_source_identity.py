from __future__ import annotations

from jobforge.models import Profile
from jobforge.tailoring.llm_resume import build_source_items


def test_identical_bullets_under_different_entries_remain_separately_citable():
    profile = Profile(
        experience=[
            {
                "company": "Acme",
                "title": "Engineer",
                "bullets": ["Built Python services."],
            },
            {
                "company": "Beta",
                "title": "Engineer",
                "bullets": ["Built Python services."],
            },
        ]
    )

    items = build_source_items(profile)

    assert len(items) == 2
    assert {item.id for item in items} == {"exp:0:b:0", "exp:1:b:0"}
    assert {item.entry_key for item in items} == {"exp:0", "exp:1"}
