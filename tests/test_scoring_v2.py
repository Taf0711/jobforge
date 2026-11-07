from __future__ import annotations

from jobforge.matching.jd import parse_jd
from jobforge.matching.score import score_job
from jobforge.models import Job, Preferences, Profile


def _profile(**kwargs) -> Profile:
    base = {
        "id": 1,
        "first_name": "Test",
        "last_name": "User",
        "skills": ["Python"],
        "education": [{"degree": "Bachelor of Science", "major": "Computer Science"}],
        "work_auth": {"years_experience": 1},
    }
    base.update(kwargs)
    return Profile(**base)


def _job(description: str, title: str = "Software Engineer", location: str = "Remote") -> Job:
    return Job(
        id=1,
        source="lever",
        source_id="1",
        company="Acme",
        title=title,
        location=location,
        description=description,
    )


def _prefs() -> Preferences:
    return Preferences(
        titles=["Software Engineer"],
        locations=["Remote"],
        experience_levels=["entry", "junior"],
    )


def test_requirement_sections_ignore_company_bio_and_boilerplate():
    text = """About us
Our founder spent 18 years at Google and has a BS in Growth.

What We Look For
- 3+ years of Python experience.
- Bachelor's degree in Computer Science.

Salary: $150,000 - $180,000
Multiple Positions Available.
#LI-DNI
"""
    jd = parse_jd(text, title_hint="Software Engineer", location_hint="Remote")

    assert jd.years_required == 3
    assert jd.degree == "bachelors"
    assert "Python" in jd.must_skills
    requirement_text = "\n".join(item["text"] for item in jd.requirements)
    for boilerplate in ("Salary:", "Multiple Positions", "#LI-DNI"):
        assert boilerplate not in requirement_text


def test_company_bio_years_do_not_trigger_experience_gate():
    job = _job(
        """About us
Our founder spent 18 years at Google.

What We Look For
- 1+ years of Python experience.
"""
    )
    result = score_job(job, _profile(), _prefs())
    assert result.gate_survival == 1.0
    assert not any("18" in gap for gap in result.gaps)


def test_scoring_v2_uses_evidence_and_stops_prose_credit():
    job = _job("Requirements:\n- Python\n- Excellent communication skills")
    profile = _profile()
    prefs = _prefs()

    current = score_job(job, profile, prefs, scoring_v2=True)
    legacy = score_job(job, profile, prefs, scoring_v2=False)

    # Python is in the skills list only (0.4 evidence), and the unparsed prose
    # row is excluded from the v2 denominator. Legacy keeps neutral prose credit.
    assert current.coverage == 0.4
    assert current.score == 20.0
    assert current.meta["sparse_jd"] is True
    assert legacy.coverage == 0.8
    assert legacy.score == 80.0
    assert any(row["matched_by"] == "unparsed" for row in current.requirements)
    assert any(row["matched_by"] == "direct" for row in current.requirements)
    unparsed = next(row for row in current.requirements if row["matched_by"] == "unparsed")
    assert unparsed["weight"] == 0.0


def test_context_skills_do_not_make_a_jd_non_thin():
    job = _job("Responsibilities\n- Build services with Python")
    result = score_job(job, _profile(), _prefs(), scoring_v2=True)

    assert result.coverage == 0.0
    assert result.score == 0.0
    assert any("thin" in gap.lower() for gap in result.gaps)


def test_remote_scope_is_not_treated_as_global_remote():
    prefs = _prefs()
    profile = _profile()
    india = score_job(_job("Requirements:\n- Python", location="Remote - India"), profile, prefs)
    usa = score_job(_job("Requirements:\n- Python", location="Remote - USA"), profile, prefs)

    assert india.gate_survival == 1.0
    assert india.meta["location"]["relocation_fit"] == 0.25
    assert any("Remote scope" in gap for gap in india.gaps)
    assert usa.gate_survival == 1.0


def test_sr_abbreviation_triggers_senior_title_gate():
    job = _job("Requirements:\n- Python", title="Sr. Software Engineer")
    result = score_job(job, _profile(), _prefs(), scoring_v2=True)

    assert result.gate_survival <= 0.6
    assert any("Title level mismatch" in gap for gap in result.gaps)


def test_year_ranges_use_the_floor_not_the_ceiling():
    jd = parse_jd("Requirements:\n- 2-12+ years of industry software engineering experience")
    assert jd.years_required == 2


def test_company_age_is_not_a_candidate_year_requirement():
    jd = parse_jd(
        "Requirements:\n"
        "For 30+ years, we have delivered software.\n"
        "- 4+ years of relevant experience"
    )
    assert jd.years_required == 4
    assert all("30" not in item["text"] for item in jd.requirements)


def test_philosophy_degree_is_not_mistaken_for_phd():
    profile = _profile(education=[{"degree": "Bachelor of Philosophy", "major": "Computer Science"}])
    job = _job("Requirements:\n- Bachelor's degree\n- Python")
    result = score_job(job, profile, _prefs(), scoring_v2=True)

    assert result.gate_survival == 1.0
    assert not any("Degree floor" in gap for gap in result.gaps)


def test_unknown_years_are_not_treated_as_zero():
    profile = _profile(work_auth={})
    job = _job("Requirements:\n- 2+ years of Python experience.\n- Python")
    result = score_job(job, profile, _prefs(), scoring_v2=True)

    assert result.gate_survival == 1.0
    assert result.meta["years"]["status"] == "unknown"
    assert any("years are unknown" in gap for gap in result.gaps)
    assert not any("profile has 0" in gap for gap in result.gaps)


def test_dated_experience_supplies_missing_years():
    profile = _profile(
        work_auth={},
        experience=[{"title": "Software Engineer", "start": "2023-03", "end": "2026-06"}],
    )
    job = _job("Requirements:\n- 2+ years of Python experience.\n- Python")
    result = score_job(job, profile, _prefs(), scoring_v2=True)

    assert result.gate_survival == 1.0
    assert result.meta["years"]["source"] == "experience"
    assert any("Experience estimate" in reason for reason in result.reasons)


def test_unparsed_year_mention_is_flagged_not_guessed():
    job = _job(
        "About us\n"
        "We are a backend team.\n"
        "Our engineers have 5+ years of experience in Python."
    )
    result = score_job(job, _profile(work_auth={"years_experience": 3}), _prefs(), scoring_v2=True)

    assert result.meta["years"]["required"] is None
    assert result.meta["years"]["status"] == "unparsed"
    assert any("mentioned but not parsed" in gap for gap in result.gaps)


def test_explicit_zero_years_still_penalizes_one_year_floor():
    preferences = Preferences(
        titles=["Software Engineer"],
        locations=["Remote"],
        experience_levels=["entry"],
        years_of_experience=0,
        years_experience_confirmed=True,
    )
    job = _job("Requirements:\n- 1+ year of Python experience.\n- Python")
    result = score_job(job, _profile(work_auth={}), preferences, scoring_v2=True)

    assert result.gate_survival == 0.65
    assert result.meta["years"]["status"] == "close"


def test_year_mention_ignores_company_age():
    company_age = parse_jd("About us\nFor 30+ years, we have delivered software.")
    candidate_years = parse_jd("About us\nOur engineers have 5+ years of experience.")
    assert company_age.years_mentioned is False
    assert candidate_years.years_mentioned is True
