from __future__ import annotations

from jobforge.matching.jd import parse_jd
from jobforge.matching.skills import SkillGraph, coverage, extract_skills
from jobforge.matching.score import score_job
from jobforge.models import Job, Preferences, Profile


def _profile(**kwargs) -> Profile:
    base = dict(
        id=1,
        first_name="Test",
        last_name="User",
        skills=["Python", "C++", "TypeScript"],
        education=[{"institution": "City College", "degree": "Bachelor of Science", "major": "Computer Science"}],
        work_auth={"authorized": True, "years_experience": 2},
    )
    base.update(kwargs)
    return Profile(**base)


def test_extract_skills_avoids_clevel_false_positive():
    assert "C" not in extract_skills("We work with C-level executives and customers")
    skills = extract_skills("Proficiency in Python, C++, and Go")
    assert {"Python", "C++", "Go"} <= set(skills)


def test_partial_credit_from_adjacent_language():
    graph = SkillGraph()
    result = coverage(
        [{"text": "Go", "skill": "Go", "importance": "must"}],
        ["C++"],
        graph,
    )
    assert result.coverage == 0.5
    assert result.rows[0].matched_by.startswith("adjacent")


def test_jd_sections_do_not_treat_responsibilities_as_requirements():
    text = """
    What You'll Be Doing
    Build backend services with Python.
    Pair with machine learning engineers.

    What you should have
    2+ years of professional software engineering experience.
    Proficiency in Python or TypeScript.

    Bonus Points
    You have worked on notifications.
    """
    jd = parse_jd(text)
    assert "Python" in jd.must_skills
    assert "TypeScript" in jd.must_skills
    assert "Machine Learning" not in jd.must_skills
    assert jd.years_required == 2


def test_clearance_gate_zeroes_score():
    job = Job(
        id=1,
        source="lever",
        source_id="1",
        company="Defense Co",
        title="Software Engineer",
        location="Remote",
        description="Requirements:\n- Active security clearance required\n- Python experience",
    )
    result = score_job(job, _profile(), Preferences(titles=["Software Engineer"], locations=["Remote"]))
    assert result.gate_survival == 0
    assert result.score == 0


def test_senior_title_penalty_for_entry_profile():
    job = Job(
        id=2,
        source="lever",
        source_id="2",
        company="Acme",
        title="Senior Software Engineer",
        location="Remote",
        description="Requirements:\n- Python",
    )
    result = score_job(
        job,
        _profile(),
        Preferences(titles=["Software Engineer"], locations=["Remote"], experience_levels=["entry"]),
    )
    assert result.gate_survival <= 0.6
    assert any("Senior title" in r for r in result.reasons) or any("Senior title" in g for g in result.gaps)


def test_thin_jd_is_capped():
    job = Job(id=3, source="lever", source_id="3", company="Acme", title="Sales Engineer", location="Remote", description="")
    result = score_job(job, _profile(), Preferences(titles=["Software Engineer"], locations=["Remote"]))
    assert result.score <= 30
    assert any("thin" in g.lower() for g in result.gaps)
