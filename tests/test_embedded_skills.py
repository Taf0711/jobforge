from __future__ import annotations

from jobforge.matching.jd import parse_jd
from jobforge.matching.score import score_job
from jobforge.matching.skills import extract_skills
from jobforge.models import Job, Preferences, Profile


def test_embedded_requirements_are_parsed_and_credited():
    assert extract_skills("Development experience with microcontrollers") == ["Microcontrollers"]
    assert "NXP Microcontrollers" in extract_skills("Familiarity with NXP microcontrollers")
    assert extract_skills("Understanding of verification methodologies") == [
        "Verification Methodologies"
    ]
    assert {"Computer Architecture", "Networking"} <= set(
        extract_skills("Solid fundamental knowledge of computer architecture and networks")
    )

    jd = parse_jd(
        "Requirements:\n"
        "- Professional development experience in C++ or C languages\n"
        "- Development experience with microcontrollers\n"
        "- Solid fundamental knowledge of computer architecture and networks\n"
        "- Familiarity with NXP microcontrollers\n"
        "- Linux and bash shell scripting\n"
        "- Understanding of verification methodologies\n"
    )
    skills = {
        skill
        for row in jd.requirements
        for skill in (row.get("skills") or ([row["skill"]] if row.get("skill") else []))
    }
    assert {
        "C",
        "C++",
        "Microcontrollers",
        "Computer Architecture",
        "NXP Microcontrollers",
        "Linux",
        "Verification Methodologies",
    } <= skills

    profile = Profile(
        first_name="Test",
        last_name="User",
        skills=[
            "C",
            "C++",
            "Microcontrollers",
            "NXP Microcontrollers",
            "Computer Architecture",
            "Networking",
            "Linux",
            "Bash",
            "Verification Methodologies",
        ],
        facts=[
            {
                "section": "projects",
                "title": "TCP/IP Network Stack",
                "text": "Built a TCP/IP stack in C.",
                "skills": ["C", "Linux", "Networking"],
            },
            {
                "section": "projects",
                "title": "Kilo",
                "text": "Built a terminal editor in C.",
                "skills": ["C", "Linux", "Operating Systems"],
            },
        ],
    )
    job = Job(
        source="test",
        source_id="embedded",
        company="Space Co",
        title="New Graduate Engineer, Aerospace Software",
        location="Remote",
        description=(
            "Requirements:\n"
            "- Professional development experience in C++ or C languages\n"
            "- Development experience with microcontrollers\n"
            "- Solid fundamental knowledge of computer architecture and networks\n"
            "- Familiarity with NXP microcontrollers\n"
            "- Linux and bash shell scripting\n"
            "- Understanding of verification methodologies\n"
        ),
    )
    result = score_job(
        job,
        profile,
        Preferences(titles=["Software Engineer"], locations=["Remote"]),
        scoring_v2=True,
    )
    assert result.coverage > 0.5
    assert not any("not parsed" in gap for gap in result.gaps)


def test_documentation_requirement_credits_confluence():
    """Confluence is a documentation tool, so it is adjacent to the skill."""
    from jobforge.matching.skills import SkillGraph

    credit, matched_by = SkillGraph().partial_credit("Documentation", {"Confluence"})
    assert credit == 0.6
    assert matched_by == "adjacent:Confluence"


def test_location_is_a_reason_not_a_skill_gap():
    """Location is logistics, not a fit knockout, so it belongs in reasons."""
    profile = Profile(
        first_name="Test",
        last_name="User",
        skills=["Python"],
        facts=[
            {
                "section": "experience",
                "title": "Engineer",
                "text": "Built Python services.",
                "skills": ["Python"],
            }
        ],
    )
    job = Job(
        source="test",
        source_id="location",
        company="Space Co",
        title="Software Engineer",
        location="Hawthorne, CA",
        description="Requirements:\n- Professional development experience in Python\n",
    )
    prefs = Preferences(
        titles=["Software Engineer"],
        locations=["New York, NY"],
        open_to_relocation=True,
    )
    result = score_job(job, profile, prefs, scoring_v2=True)
    assert any("Location outside preferred" in reason for reason in result.reasons)
    assert not any("Location" in gap for gap in result.gaps)
