from __future__ import annotations

import json
from pathlib import Path

from jobforge import autopilot, db, risk, services
from jobforge.config import BrowserConfig, Config, EmailConfig, LLMConfig, ServerConfig
from jobforge.models import Job, MatchResult, Preferences, Profile


def _config(tmp_path: Path) -> Config:
    cfg = Config(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        db_path=tmp_path / "data" / "test.sqlite3",
        profile_path=None,
        resume_path=None,
        sources={},
        preferences={"titles": ["Software Engineer"], "min_match": 0},
        llm=LLMConfig(),
        email=EmailConfig(),
        browser=BrowserConfig(headless=True),
        server=ServerConfig(),
    )
    cfg.ensure_dirs()
    db.init_db(cfg.db_path)
    return cfg


def _seed(cfg: Config, tmp_path: Path) -> None:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n")
    conn = db.connect(cfg.db_path)
    profile = Profile(name="autopilot", resume_path=str(resume), email="ap@example.com")
    profile_id = db.save_profile(conn, profile)
    db.set_active_profile(conn, profile_id)
    db.save_preferences(conn, profile_id, Preferences(titles=["Software Engineer"], min_match=0))
    for source_id, company, title, url, score in (
        ("lever-1", "LeverCo", "Backend Engineer", "https://jobs.lever.co/leverco/1", 90),
        ("wd-1", "WorkdayCo", "Platform Engineer", "https://acme.myworkdayjobs.com/job/1", 95),
    ):
        job = Job(
            source="lever" if "lever" in url else "workday",
            source_id=source_id,
            company=company,
            title=title,
            apply_url=url,
            description="Requirements:\n- Python",
        )
        job_id = db.upsert_job(conn, job)
        db.set_match(
            conn,
            MatchResult(
                job_id=job_id,
                profile_id=profile_id,
                score=score,
                coverage=1.0,
                gate_survival=1.0,
            ),
        )
    conn.close()


def test_autopilot_dry_run_prepares_only_allowlisted_ats(tmp_path: Path, monkeypatch):
    cfg = _config(tmp_path)
    _seed(cfg, tmp_path)
    monkeypatch.setattr(
        services,
        "tailor_job",
        lambda *args, **kwargs: {"pdf": str(tmp_path / "tailored.pdf")},
    )
    conn = db.connect(cfg.db_path)

    stats = autopilot.run_once(cfg, conn, limit=2, dry_run=True)

    conn.close()
    assert stats["dry_run"] is True
    assert stats["prepared"] == 1
    assert stats["skipped"] == 1
    assert stats["submitted"] == 0
    assert stats["stopped_reason"] == "dry_run"
    assert Path(stats["digest_path"]).is_file()
    digest = json.loads(Path(stats["digest_path"]).read_text(encoding="utf-8"))
    assert digest["prepared"] == 1


def test_autopilot_submits_only_after_consent_and_marks_queue(tmp_path: Path, monkeypatch):
    cfg = _config(tmp_path)
    _seed(cfg, tmp_path)
    risk.enable(cfg, "autopilot_submit", risk.CONSENT_PHRASE)
    monkeypatch.setattr(
        services,
        "tailor_job",
        lambda *args, **kwargs: {"pdf": str(tmp_path / "tailored.pdf")},
    )
    monkeypatch.setattr(
        services,
        "apply_job",
        lambda *args, **kwargs: {"submitted": True, "captcha": False},
    )
    conn = db.connect(cfg.db_path)

    stats = autopilot.run_once(cfg, conn, limit=1)

    conn.close()
    assert stats["submitted"] == 1
    assert stats["prepared"] == 1
    assert stats["skipped"] == 1
    assert stats["stopped_reason"] == ""


def test_autopilot_limit_counts_each_job_once(tmp_path: Path, monkeypatch):
    cfg = _config(tmp_path)
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n")
    conn = db.connect(cfg.db_path)
    profile = Profile(name="limit", resume_path=str(resume), email="limit@example.com")
    profile_id = db.save_profile(conn, profile)
    db.set_active_profile(conn, profile_id)
    db.save_preferences(conn, profile_id, Preferences(titles=[], min_match=0))
    for index in range(6):
        job = Job(
            source="lever",
            source_id=f"lever-limit-{index}",
            company=f"LeverCo{index}",
            title="Backend Engineer",
            apply_url=f"https://jobs.lever.co/leverco{index}/1",
            description="Requirements:\n- Python",
        )
        job_id = db.upsert_job(conn, job)
        db.set_match(
            conn,
            MatchResult(
                job_id=job_id,
                profile_id=profile_id,
                score=80.0,
                coverage=1.0,
                gate_survival=1.0,
            ),
        )
    conn.close()

    risk.enable(cfg, "autopilot_submit", risk.CONSENT_PHRASE)
    monkeypatch.setattr(
        services,
        "tailor_job",
        lambda *args, **kwargs: {"pdf": str(tmp_path / "tailored.pdf")},
    )
    monkeypatch.setattr(
        services,
        "apply_job",
        lambda *args, **kwargs: {"submitted": True, "captcha": False},
    )
    conn = db.connect(cfg.db_path)

    stats = autopilot.run_once(cfg, conn, limit=5)

    conn.close()
    assert stats["submitted"] == 5
    assert stats["prepared"] == 5
    assert stats["queue"]["submitted"] == 5
    assert stats["queue"]["pending"] == 1


def test_autopilot_stops_on_captcha_without_marking_submitted(tmp_path: Path, monkeypatch):
    cfg = _config(tmp_path)
    _seed(cfg, tmp_path)
    risk.enable(cfg, "autopilot_submit", risk.CONSENT_PHRASE)
    monkeypatch.setattr(
        services,
        "tailor_job",
        lambda *args, **kwargs: {"pdf": str(tmp_path / "tailored.pdf")},
    )
    monkeypatch.setattr(
        services,
        "apply_job",
        lambda *args, **kwargs: {"submitted": False, "captcha": True},
    )
    conn = db.connect(cfg.db_path)

    stats = autopilot.run_once(cfg, conn, limit=2)

    conn.close()
    assert stats["submitted"] == 0
    assert stats["prepared"] == 1
    assert stats["stopped_reason"] == "captcha"


def test_autopilot_refuses_submit_without_risk_flag(tmp_path: Path):
    cfg = _config(tmp_path)
    _seed(cfg, tmp_path)
    conn = db.connect(cfg.db_path)

    stats = autopilot.run_once(cfg, conn, limit=1)

    conn.close()
    assert stats["submitted"] == 0
    assert "disabled" in stats["stopped_reason"].lower()


def test_autopilot_page_and_dry_run_launch(tmp_path: Path, monkeypatch):
    from fastapi.testclient import TestClient
    from jobforge.web import app as web_app

    cfg = _config(tmp_path)
    _seed(cfg, tmp_path)
    client = TestClient(web_app.create_app(cfg))
    page = client.get("/autopilot")
    assert page.status_code == 200
    assert "Autopilot submit" in page.text
    assert "Dry run" in page.text

    calls = []
    monkeypatch.setattr(
        web_app,
        "launch_autopilot",
        lambda *args, **kwargs: calls.append(kwargs) or 123,
    )
    response = client.post(
        "/autopilot/run",
        data={"limit": "2", "dry_run": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert calls and calls[0]["dry_run"] is True


def test_autopilot_live_launch_requires_risk_flag(tmp_path: Path, monkeypatch):
    from fastapi.testclient import TestClient
    from jobforge.web import app as web_app

    cfg = _config(tmp_path)
    _seed(cfg, tmp_path)
    client = TestClient(web_app.create_app(cfg))
    calls = []
    monkeypatch.setattr(
        web_app,
        "launch_autopilot",
        lambda *args, **kwargs: calls.append(kwargs) or 123,
    )
    response = client.post(
        "/autopilot/run",
        data={"limit": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert not calls
    assert "disabled" in response.headers["location"].lower()

    risk.enable(cfg, "autopilot_submit", risk.CONSENT_PHRASE)
    response = client.post(
        "/autopilot/run",
        data={"limit": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert calls and calls[0]["dry_run"] is False


def test_autopilot_live_pass_submits_an_existing_prepared_item(tmp_path: Path, monkeypatch):
    cfg = _config(tmp_path)
    _seed(cfg, tmp_path)
    monkeypatch.setattr(
        services,
        "tailor_job",
        lambda *args, **kwargs: {"pdf": str(tmp_path / "tailored.pdf")},
    )
    conn = db.connect(cfg.db_path)
    autopilot.run_once(cfg, conn, limit=2, dry_run=True)
    risk.enable(cfg, "autopilot_submit", risk.CONSENT_PHRASE)
    monkeypatch.setattr(
        services,
        "apply_job",
        lambda *args, **kwargs: {"submitted": True, "captcha": False},
    )

    stats = autopilot.run_once(cfg, conn, limit=1)

    conn.close()
    assert stats["submitted"] == 1
    assert stats["prepared"] == 0
    assert stats["queue"]["submitted"] == 1


def test_autopilot_stops_on_required_fields_and_records_them(tmp_path: Path, monkeypatch):
    cfg = _config(tmp_path)
    _seed(cfg, tmp_path)
    risk.enable(cfg, "autopilot_submit", risk.CONSENT_PHRASE)
    monkeypatch.setattr(
        services,
        "tailor_job",
        lambda *args, **kwargs: {"pdf": str(tmp_path / "tailored.pdf")},
    )
    monkeypatch.setattr(
        services,
        "apply_job",
        lambda *args, **kwargs: {
            "submitted": False,
            "captcha": False,
            "required_missing": ["Location (required field left empty)", "Why N1?"],
        },
    )
    conn = db.connect(cfg.db_path)

    stats = autopilot.run_once(cfg, conn, limit=2)

    conn.close()
    assert stats["submitted"] == 0
    assert stats["stopped_reason"] == "required_fields"
    assert "Why N1?" in stats["required_missing"]
    assert any(
        item["category"] == "motivation" and item["action"] == "review"
        for item in stats["required_review"]
    )


def test_autopilot_writes_state_and_trace(tmp_path: Path, monkeypatch):
    cfg = _config(tmp_path)
    _seed(cfg, tmp_path)
    monkeypatch.setattr(
        services,
        "tailor_job",
        lambda *args, **kwargs: {"pdf": str(tmp_path / "tailored.pdf"), "llm_used": True},
    )
    conn = db.connect(cfg.db_path)

    autopilot.run_once(cfg, conn, limit=2, dry_run=True)
    conn.close()

    state = autopilot.read_state(cfg)
    assert state is not None
    assert state["status"] == "ok"
    assert state["stage"] == "done"
    assert state["finished_at"]
    assert state["counters"]["prepared"] == 1
    events = [item["event"] for item in autopilot.read_trace(cfg)]
    assert "run_started" in events
    assert "job_started" in events
    assert "tailored" in events
    assert "run_finished" in events


def test_autopilot_status_api_and_page(tmp_path: Path, monkeypatch):
    from fastapi.testclient import TestClient
    from jobforge.web import app as web_app

    cfg = _config(tmp_path)
    _seed(cfg, tmp_path)
    monkeypatch.setattr(
        services,
        "tailor_job",
        lambda *args, **kwargs: {"pdf": str(tmp_path / "tailored.pdf")},
    )
    conn = db.connect(cfg.db_path)
    autopilot.run_once(cfg, conn, limit=1, dry_run=True)
    conn.close()

    client = TestClient(web_app.create_app(cfg))
    payload = client.get("/api/autopilot/status").json()
    assert payload["running"] is False
    assert payload["state"]["status"] == "ok"
    assert payload["trace"]
    page = client.get("/autopilot")
    assert page.status_code == 200
    assert 'id="autopilot-live"' in page.text
    assert "Live trace" in page.text


def test_read_trace_offset_reads_past_the_recent_window(tmp_path: Path):
    cfg = _config(tmp_path)
    _seed(cfg, tmp_path)
    for index in range(6):
        autopilot.trace_event(cfg, f"event_{index}")

    newest = autopilot.read_trace(cfg, limit=2)
    assert [item["event"] for item in newest] == ["event_4", "event_5"]

    page = autopilot.read_trace(cfg, limit=2, offset=2)
    assert [item["event"] for item in page] == ["event_2", "event_3"]

    rest = autopilot.read_trace(cfg, limit=2, offset=4)
    assert [item["event"] for item in rest] == ["event_4", "event_5"]
