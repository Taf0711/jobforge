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


def test_clearance_gate_nearly_zeroes_ineligible_score():
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
    assert result.gate_survival == 0.02
    assert result.score < 2


def test_clearance_eligible_profile_gets_partial_survival():
    job = Job(
        id=1,
        source="lever",
        source_id="1",
        company="Defense Co",
        title="Software Engineer",
        location="Remote",
        description="Requirements:\n- Active security clearance required\n- Python experience",
    )
    profile = _profile(work_auth={"authorized": True, "years_experience": 2, "clearance_eligible": True})
    result = score_job(job, profile, Preferences(titles=["Software Engineer"], locations=["Remote"]))
    assert result.gate_survival == 0.35
    assert any("Clearance-eligible" in reason for reason in result.reasons)


def test_citizenship_itar_and_gpa_gates():
    profile = _profile(work_auth={"authorized": True, "years_experience": 2, "gpa": 3.0})
    preferences = Preferences(titles=["Software Engineer"], locations=["Remote"])
    citizenship = score_job(
        Job(
            id=4,
            source="lever",
            source_id="4",
            company="Defense Co",
            title="Software Engineer",
            location="Remote",
            description="Requirements:\n- Must be a US citizen\n- Python",
        ),
        profile,
        preferences,
    )
    assert citizenship.gate_survival == 0.02

    itar = score_job(
        Job(
            id=5,
            source="lever",
            source_id="5",
            company="Defense Co",
            title="Software Engineer",
            location="Remote",
            description="Requirements:\n- ITAR position\n- Python",
        ),
        profile,
        preferences,
    )
    assert itar.gate_survival == 0.05

    gpa = score_job(
        Job(
            id=6,
            source="lever",
            source_id="6",
            company="Acme",
            title="Software Engineer",
            location="Remote",
            description="Requirements:\n- Minimum GPA 3.5\n- Python",
        ),
        profile,
        preferences,
    )
    assert gpa.gate_survival == 0.08


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


def test_what_we_require_and_technologies_headings_are_parsed():
    jd = parse_jd(
        """The Role
Build query engines.

Technologies We Use
Java, Scala, Rust, and Python
Apache Spark and Apache DataFusion

What We Require
Proficiency in Java, Rust, Scala, C++, or C.
Strong foundations in algorithms and data structures.
""",
        title_hint="Software Engineer, Query Engines",
    )

    assert "Java" in jd.must_skills
    assert "C++" in jd.must_skills
    assert "Python" in jd.skills
    assert "Spark" in jd.skills
