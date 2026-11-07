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


def test_extract_skills_does_not_match_next_generation():
    assert "Next.js" not in extract_skills("Design the next generation of APIs")
    assert "Next.js" in extract_skills("Experience with Next.js and React")


def test_extract_skills_guards_date_and_go_to_market_homonyms():
    assert "Spring" not in extract_skills("Must be graduating in Spring 2027")
    assert "Spring" in extract_skills("Spring Boot and Spring Framework")
    assert "Go" not in extract_skills("Own the Go to market strategy")
    assert "Go" in extract_skills("Experience with Go and Python")


def test_partial_credit_from_adjacent_language():
    graph = SkillGraph()
    result = coverage(
        [{"text": "Go", "skill": "Go", "importance": "must"}],
        ["C++"],
        graph,
    )
    assert result.coverage == 0.5
    assert result.rows[0].matched_by.startswith("adjacent")


def test_transfer_credit_uses_the_source_skill_evidence():
    graph = SkillGraph()
    direct = coverage(
        [{"text": "Go", "skill": "Go", "importance": "must"}],
        ["Go"],
        graph,
        {"Go": 0.4},
        evidence_direct=True,
    )
    transfer = coverage(
        [{"text": "Go", "skill": "Go", "importance": "must"}],
        ["C++"],
        graph,
        {"C++": 0.4},
    )

    assert direct.coverage == 0.4
    assert transfer.coverage == 0.2
    assert direct.coverage > transfer.coverage


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


def test_or_language_clause_is_one_requirement_row():
    jd = parse_jd(
        "Requirements:\n- Proficiency in Java, Rust, Scala, C++, or C.\n- Python"
    )

    rows = [
        row for row in jd.requirements
        if "Proficiency in Java" in row.get("text", "")
    ]
    assert len(rows) == 1
    assert {"Java", "Rust", "Scala", "C++", "C"} <= set(rows[0]["skills"])


def test_title_and_company_are_not_requirement_evidence():
    job = Job(
        id=1,
        source="ashby",
        source_id="1",
        company="Datadog",
        title="Machine Learning Engineer",
        location="Remote",
        description="Requirements:\n- Python",
    )
    result = score_job(
        job,
        _profile(skills=["Python"]),
        Preferences(titles=["Software Engineer"], locations=["Remote"]),
    )

    assert all("Datadog" not in row["requirement"] for row in result.requirements)
    assert all("Machine Learning" not in (row["skill"] or "") for row in result.requirements)
    assert any(row["skill"] == "Python" for row in result.requirements)


def test_body_paragraph_is_not_a_requirement_without_a_bullet():
    jd = parse_jd(
        "We build Python services for customers and care deeply about tests, "
        "security, and distributed systems across the whole platform."
    )
    assert jd.requirements == []


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


def test_comp_policy_and_pursuit_are_stored_in_meta():
    job = Job(
        id=7,
        source="lever",
        source_id="7",
        company="Acme",
        title="Software Engineer",
        location="New York, NY",
        salary_min=145_000,
        salary_max=155_000,
        description="Requirements:\n- Python",
    )
    result = score_job(
        job,
        _profile(),
        Preferences(titles=["Software Engineer"], locations=["New York, NY"]),
    )

    assert result.meta["comp"]["verdict"] == "meets_target"
    assert result.meta["pursuit"] == round(result.score + 2, 1)


def test_comp_policy_penalizes_nyc_below_floor():
    job = Job(
        id=8,
        source="lever",
        source_id="8",
        company="Acme",
        title="Software Engineer",
        location="New York, NY",
        salary_min=70_000,
        salary_max=90_000,
        description="Requirements:\n- Python",
    )
    result = score_job(
        job,
        _profile(),
        Preferences(titles=["Software Engineer"], locations=["New York, NY"]),
    )

    assert result.meta["comp"]["verdict"] == "below_floor"
    assert result.meta["pursuit"] == round(result.score - 20, 1)


def test_or_clause_uses_best_match_and_does_not_gap_unmet_alternatives():
    result = coverage(
        [
            {
                "text": "Proficiency in Java, Rust, Scala, C++, or C.",
                "skill": "Java",
                "skills": ["Java", "Rust", "Scala", "C++", "C"],
                "importance": "must",
            }
        ],
        ["C++"],
        SkillGraph(),
        prose_credit=0.0,
        evidence_direct=True,
    )

    assert result.coverage == 1.0
    assert result.rows[0].skill == "C++"
    assert result.rows[0].gap is False
    assert "Java" not in result.gaps


def test_unparsed_rows_are_reported_but_not_in_the_denominator():
    result = coverage(
        [
            {"text": "Python", "skill": "Python", "importance": "must"},
            {"text": "Excellent communication skills", "skill": None, "importance": "must"},
        ],
        ["Python"],
        SkillGraph(),
        prose_credit=0.0,
        evidence_direct=True,
    )

    assert result.coverage == 1.0
    assert next(row for row in result.rows if row.matched_by == "unparsed").weight == 0.0


def test_direct_skills_list_evidence_is_thin_not_a_missing_gap():
    result = coverage(
        [{"text": "Python", "skill": "Python", "importance": "must"}],
        ["Python"],
        SkillGraph(),
        {"Python": 0.4},
        prose_credit=0.0,
        evidence_direct=True,
    )

    row = result.rows[0]
    assert row.credit == 0.4
    assert row.matched_by == "direct"
    assert row.gap is False
    assert row.thin is True
    assert result.gaps == []
