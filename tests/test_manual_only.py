from __future__ import annotations

import pytest

from jobforge import db, services
from jobforge.automation.base import is_manual_only_url, run_application
from jobforge.config import BrowserConfig, Config, EmailConfig, LLMConfig, ServerConfig
from jobforge.models import Job, Preferences, Profile


def test_manual_only_hosts_are_detected():
    assert is_manual_only_url("https://www.indeed.com/viewjob?jk=1")
    assert is_manual_only_url("https://jobs.linkedin.com/jobs/view/1")
    assert is_manual_only_url("https://www.linkedin.com./jobs/view/1")
    assert is_manual_only_url("https://indeed.com./viewjob?jk=1")
    assert not is_manual_only_url("https://jobs.lever.co/acme/1")
    assert not is_manual_only_url("https://notindeed.com/job/1")


def test_run_application_refuses_indeed_and_linkedin():
    for url in ("https://www.indeed.com/viewjob?jk=1", "https://www.linkedin.com/jobs/view/1"):
        job = Job(source="direct", source_id="1", company="Acme", title="Engineer", apply_url=url)
        with pytest.raises(ValueError, match="manual-only"):
            run_application(job, Profile(), Preferences(), BrowserConfig(headless=True), "resume.pdf")


def test_services_apply_job_refuses_manual_only_before_browser(tmp_path):
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
        browser=BrowserConfig(headless=True),
        server=ServerConfig(),
    )
    cfg.ensure_dirs()
    db.init_db(cfg.db_path)
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n")
    conn = db.connect(cfg.db_path)
    profile_id = db.save_profile(conn, Profile(name="test", resume_path=str(resume)))
    db.set_active_profile(conn, profile_id)
    job_id = db.upsert_job(
        conn,
        Job(
            source="direct",
            source_id="1",
            company="Acme",
            title="Engineer",
            apply_url="https://www.indeed.com/viewjob?jk=1",
        ),
    )
    conn.close()

    with pytest.raises(ValueError, match="manual-only"):
        services.apply_job(cfg, db.connect(cfg.db_path), job_id)
