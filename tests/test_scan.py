from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jobforge import db
from jobforge.config import BrowserConfig, Config, EmailConfig, LLMConfig, ServerConfig
from jobforge.models import Job, Preferences, Profile
from jobforge.scan.quick import filter_job, is_us_location, quick_scan
from jobforge.scan.service import dedupe_jobs


def _ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def _profile() -> Profile:
    return Profile(
        id=1,
        name="scan-test",
        first_name="Scan",
        last_name="Test",
        headline="New Grad / Entry-Level Software Engineer",
        skills=["Python", "React", "TypeScript", "SQL", "Docker", "C++"],
        work_auth={
            "us_citizen": True,
            "work_authorized_us": True,
            "itar_eligible": True,
            "clearance_eligible": True,
            "gpa": 3.03,
        },
        facts=[
            {
                "section": "experience",
                "text": "Built a React and Python portal.",
                "skills": ["React", "Python"],
            }
        ],
        projects=[{"name": "Splice", "tech": ["Python", "TypeScript"]}],
    )


def _prefs() -> Preferences:
    return Preferences(
        titles=["Software Engineer"],
        industries=["Full-stack web", "AI/ML/LLM", "Security/identity/compliance"],
        locations=["New York City", "Remote USA"],
        open_to_relocation=True,
        experience_levels=["entry", "junior"],
        min_match=45,
    )


def _job(**overrides) -> Job:
    data = {
        "source": "greenhouse",
        "source_id": "1",
        "company": "Acme",
        "title": "Software Engineer - New Grad",
        "location": "San Francisco, CA",
        "posted_at": _ago(2),
        "description": "We are hiring a new grad software engineer. Python, React, SQL.",
        "raw": {"competition_profile": "direct_ats"},
        "competition_profile": "direct_ats",
    }
    data.update(overrides)
    return Job(**data)


def _config(tmp_path):
    cfg = Config(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        db_path=tmp_path / "data" / "test.sqlite3",
        profile_path=None,
        resume_path=None,
        sources={},
        preferences={},
        llm=LLMConfig(),
        email=EmailConfig(),
        browser=BrowserConfig(),
        server=ServerConfig(),
    )
    cfg.ensure_dirs()
    db.init_db(cfg.db_path)
    return cfg


def test_us_location_and_remote_scope():
    assert is_us_location("San Francisco, CA") is True
    assert is_us_location("New York City") is True
    assert is_us_location("Toronto, ON, Canada") is False
    assert is_us_location("Bristol, UK") is False
    assert is_us_location("Canada Santa Clara, CA United States") is True
    assert is_us_location("4 locations SF Remote in USA Europe NYC") is True
    assert is_us_location("Remote in Canada") is False
    assert is_us_location("Bangalore") is False


def test_filter_rejects_stale_non_us_and_internships():
    prefs = _prefs()
    scan_cfg = {"max_age_days": 30, "exclude_non_us": True, "exclude_internships": True}

    stale = _job(source="github_jobs", posted_at=_ago(210), competition_profile="mass_aggregator")
    keep, reason = filter_job(stale, prefs=prefs, applications=[], scan_cfg=scan_cfg)
    assert keep is False
    assert "stale" in reason

    canada = _job(location="Toronto, ON, Canada", source="github_jobs", competition_profile="mass_aggregator")
    keep, reason = filter_job(canada, prefs=prefs, applications=[], scan_cfg=scan_cfg)
    assert keep is False
    assert reason == "non-US location"

    intern = _job(title="Software Engineer Intern", source="github_jobs", competition_profile="mass_aggregator")
    keep, reason = filter_job(intern, prefs=prefs, applications=[], scan_cfg=scan_cfg)
    assert keep is False
    assert "internship" in reason

    non_swe = _job(title="Fraud Strategy Associate", company="Brex")
    keep, reason = filter_job(non_swe, prefs=prefs, applications=[], scan_cfg=scan_cfg)
    assert keep is False
    assert reason == "non-SWE title"

    fresh = _job()
    keep, reason = filter_job(fresh, prefs=prefs, applications=[], scan_cfg=scan_cfg)
    assert keep is True, reason


def test_filter_rejects_already_applied_and_recent_rejection():
    prefs = _prefs()
    scan_cfg = {"max_age_days": 30, "recent_rejection_days": 60}
    applied = _job(company="Acme Corp", title="Software Engineer")
    applications = [
        {
            "company": "Acme",
            "title": "Software Engineer",
            "status": "submitted",
            "updated_at": _ago(10),
        }
    ]
    keep, reason = filter_job(applied, prefs=prefs, applications=applications, scan_cfg=scan_cfg)
    assert keep is False
    assert reason == "already applied"

    rejected = _job(company="Acme Corp", title="Machine Learning Engineer")
    applications = [
        {
            "company": "Acme",
            "title": "Software Engineer",
            "status": "rejected",
            "updated_at": _ago(10),
        }
    ]
    keep, reason = filter_job(rejected, prefs=prefs, applications=applications, scan_cfg=scan_cfg)
    assert keep is False
    assert reason == "recent rejection at company"


def test_quick_scan_ranks_direct_ats_and_penalizes_mass_aggregator():
    profile = _profile()
    prefs = _prefs()
    direct = _job(
        source="greenhouse",
        title="Software Engineer - New Grad",
        location="San Francisco, CA",
        competition_profile="direct_ats",
        raw={"competition_profile": "direct_ats"},
    )
    lead = _job(
        source="github_jobs",
        title="Artificial Intelligence/Machine Learning Engineer 1",
        company="Peraton",
        location="Laurel, MD",
        posted_at=_ago(30),
        description="Artificial Intelligence/Machine Learning Engineer 1\nPeraton — Laurel, MD\nCommunity job list: SimplifyJobs/New-Grad-Positions. Open the employer posting.",
        competition_profile="mass_aggregator",
        raw={"competition_profile": "mass_aggregator"},
    )
    direct_scan = quick_scan(direct, profile=profile, prefs=prefs, archetypes={}, scan_cfg={})
    lead_scan = quick_scan(lead, profile=profile, prefs=prefs, archetypes={}, scan_cfg={})
    assert direct_scan.competition_profile == "direct_ats"
    assert lead_scan.competition_profile == "mass_aggregator"
    assert direct_scan.scan_score > lead_scan.scan_score
    assert direct_scan.confidence > lead_scan.confidence


def test_dedupe_prefers_direct_ats_for_same_role():
    mass = _job(source="github_jobs", company="Acme", title="Software Engineer", competition_profile="mass_aggregator")
    direct = _job(source="greenhouse", company="Acme", title="Software Engineer", competition_profile="direct_ats")
    selected, deduped = dedupe_jobs([mass, direct])
    assert deduped == 1
    assert len(selected) == 1
    assert selected[0].source == "greenhouse"


def test_scan_results_persist_and_sort(tmp_path):
    cfg = _config(tmp_path)
    conn = db.connect(cfg.db_path)
    profile = _profile()
    profile.id = None
    pid = db.save_profile(conn, profile)
    db.set_active_profile(conn, pid)
    db.save_preferences(conn, pid, _prefs())
    profile = db.get_profile(conn, pid)
    prefs = db.get_preferences(conn, pid)

    low = _job(source="github_jobs", source_id="low", company="LowCo", title="AI/ML Engineer 1", competition_profile="mass_aggregator")
    high = _job(source="greenhouse", source_id="high", company="HighCo", title="Software Engineer - New Grad", competition_profile="direct_ats")
    low.id = db.upsert_job(conn, low)
    high.id = db.upsert_job(conn, high)
    db.set_scan(conn, quick_scan(low, profile=profile, prefs=prefs, archetypes={}, scan_cfg={}))
    db.set_scan(conn, quick_scan(high, profile=profile, prefs=prefs, archetypes={}, scan_cfg={}))

    rows = db.list_jobs(conn, pid, sort_by="scan", limit=10)
    conn.close()

    assert rows[0]["scan"]["score"] >= rows[1]["scan"]["score"]
    assert rows[0]["scan"]["score"] > 0
    assert all(row["scan"] is not None for row in rows)


def test_quick_scan_caps_over_level_years():
    profile = _profile()
    profile.work_auth["years_experience"] = 3
    job = _job(description="Requirements:\n- 5+ years of Python experience.\n- Python, React, SQL.")
    scan = quick_scan(job, profile=profile, prefs=_prefs(), archetypes={}, scan_cfg={})

    assert scan.scan_score <= 50
    assert scan.meta["years"]["status"] == "short"
    assert any("below the floor" in gap for gap in scan.gaps)


def test_quick_scan_caps_far_over_level_years():
    profile = _profile()
    profile.work_auth["years_experience"] = 3
    job = _job(description="Requirements:\n- 8+ years of Python experience.\n- Python")
    scan = quick_scan(job, profile=profile, prefs=_prefs(), archetypes={}, scan_cfg={})

    assert scan.scan_score <= 35
    assert scan.meta["years"]["status"] == "far_short"


def test_quick_scan_flags_unknown_candidate_years():
    job = _job(description="Requirements:\n- 2+ years of Python experience.\n- Python")
    scan = quick_scan(job, profile=_profile(), prefs=_prefs(), archetypes={}, scan_cfg={})

    assert scan.scan_score <= 70
    assert scan.meta["years"]["status"] == "unknown"
    assert any("years are unknown" in gap for gap in scan.gaps)


def test_quick_scan_flags_unparsed_year_mention():
    job = _job(
        description=(
            "About us\n"
            "We are a backend team.\n"
            "Our engineers have 5+ years of experience in Python."
        )
    )
    scan = quick_scan(job, profile=_profile(), prefs=_prefs(), archetypes={}, scan_cfg={})

    assert scan.scan_score <= 70
    assert scan.meta["years"]["status"] == "unparsed"
    assert any("mentioned but not parsed" in gap for gap in scan.gaps)
