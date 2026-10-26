from __future__ import annotations

from pathlib import Path

import pytest

from jobforge import db, quality, services
from jobforge.automation.base import FieldReport, FillReport
from jobforge.config import BrowserConfig, Config, EmailConfig, LLMConfig, ServerConfig
from jobforge.models import Application, Job, MatchResult, Preferences, Profile


def _config(tmp_path: Path) -> Config:
    cfg = Config(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        db_path=tmp_path / "data" / "test.sqlite3",
        profile_path=None,
        resume_path=None,
        sources={},
        preferences={"min_match": 0},
        llm=LLMConfig(),
        email=EmailConfig(),
        browser=BrowserConfig(headless=True),
        server=ServerConfig(),
    )
    cfg.ensure_dirs()
    db.init_db(cfg.db_path)
    return cfg


def _seed(tmp_path: Path, *, score: float = 100):
    cfg = _config(tmp_path)
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4")
    conn = db.connect(cfg.db_path)
    profile = Profile(name="tester", email="t@example.com", resume_path=str(resume))
    profile.id = db.save_profile(conn, profile)
    db.set_active_profile(conn, profile.id)
    db.save_preferences(conn, profile.id, Preferences(min_match=50))
    job = Job(
        source="lever",
        source_id="apply-1",
        company="Acme",
        title="Engineer",
        apply_url="https://jobs.example/apply",
        description="Requirements:\n- Python",
    )
    job.id = db.upsert_job(conn, job)
    db.set_match(
        conn,
        MatchResult(
            job_id=job.id,
            profile_id=profile.id,
            score=score,
            coverage=1.0,
            gate_survival=1.0,
        ),
    )
    return cfg, conn, job, resume


def test_apply_job_runs_quality_gate_directly(tmp_path: Path):
    cfg, conn, job, _resume = _seed(tmp_path)
    conn.execute("UPDATE matches SET score=0 WHERE job_id=?", (job.id,))
    conn.commit()

    with pytest.raises(quality.QualityBlocked):
        services.apply_job(cfg, conn, job.id)

    conn.close()


def test_apply_job_preserves_cover_letter_and_answers(tmp_path: Path, monkeypatch):
    cfg, conn, job, resume = _seed(tmp_path)
    db.create_application(
        conn,
        Application(
            job_id=job.id,
            profile_id=1,
            status="prepared",
            ats="lever",
            resume_path=str(resume),
            cover_letter_path="tailored-cover.txt",
            answers={"old": "answer"},
            screenshot_path="old-shot.png",
        ),
    )
    calls: list[dict] = []

    def fake_run_application(*args, **kwargs):
        calls.append(kwargs)
        return FillReport(
            ats="lever",
            url="https://jobs.example/apply",
            resume_uploaded=True,
            cover_letter_uploaded=True,
            fields=[FieldReport("Email", "t@example.com", "email", "filled")],
            submitted=False,
        )

    monkeypatch.setattr("jobforge.automation.base.run_application", fake_run_application)

    result = services.apply_job(cfg, conn, job.id)

    row = conn.execute("SELECT * FROM applications WHERE job_id=?", (job.id,)).fetchone()
    assert result["submitted"] is False
    assert row["cover_letter_path"] == "tailored-cover.txt"
    assert row["status"] == "prepared"
    assert row["answers_json"] == '{"Email": "t@example.com"}'
    assert calls and calls[0]["cover_letter_path"] == "tailored-cover.txt"
    assert calls[0]["submission_authorized"] is False
    conn.close()
