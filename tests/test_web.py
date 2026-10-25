from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from jobforge import db
from jobforge.config import Config, BrowserConfig, EmailConfig, LLMConfig, ServerConfig
from jobforge.matching.score import score_job
from jobforge.models import Job, MatchResult, Profile
from jobforge.queue import Queue
from jobforge.web.app import create_app


def _config(tmp_path: Path) -> Config:
    return Config(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        db_path=tmp_path / "data" / "test.sqlite3",
        profile_path=None,
        resume_path=None,
        sources={},
        preferences={"titles": ["Software Engineer"]},
        llm=LLMConfig(),
        email=EmailConfig(),
        browser=BrowserConfig(),
        server=ServerConfig(),
    )


def test_pages_render(tmp_path: Path):
    app = create_app(_config(tmp_path))
    client = TestClient(app)
    for path in ["/", "/jobs", "/queue", "/applications", "/profiles", "/settings", "/inbox", "/runs", "/healthz"]:
        response = client.get(path)
        assert response.status_code == 200, path
        assert "JobForge" in response.text or path == "/healthz"


def _seed(
    tmp_path: Path,
    score: float = 10,
    apply_url: str = "https://jobs.example/apply",
    location: str = "",
    salary_min: int | None = None,
    salary_max: int | None = None,
):
    cfg = _config(tmp_path)
    cfg.ensure_dirs()
    app = create_app(cfg)
    conn = db.connect(cfg.db_path)
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n")
    profile_id = db.save_profile(conn, Profile(name="web-test", resume_path=str(resume)))
    db.set_active_profile(conn, profile_id)
    job_id = db.upsert_job(
        conn,
        Job(
            source="lever",
            source_id="web-1",
            company="Acme",
            title="Engineer",
            location=location,
            salary_min=salary_min,
            salary_max=salary_max,
            apply_url=apply_url,
            description="Requirements:\n- Python",
        ),
    )
    db.set_match(
        conn,
        MatchResult(
            job_id=job_id,
            profile_id=profile_id,
            score=score,
            coverage=score / 100,
            gate_survival=1.0,
        ),
    )
    conn.close()
    return cfg, app, job_id


def test_job_detail_disables_fill_when_quality_is_blocked(tmp_path: Path):
    _cfg, app, job_id = _seed(tmp_path, score=10)
    client = TestClient(app)
    response = client.get(f"/jobs/{job_id}")

    assert response.status_code == 200
    assert "Blocked." in response.text
    assert 'disabled title="Quality gate is blocked' in response.text


def test_apply_route_blocks_before_spawning_a_browser(tmp_path: Path, monkeypatch):
    _cfg, app, job_id = _seed(tmp_path, score=10)
    client = TestClient(app)

    def fail(*args, **kwargs):
        raise AssertionError("subprocess must not be launched")

    monkeypatch.setattr("jobforge.web.app.services.launch_apply_subprocess", fail)
    response = client.post(f"/jobs/{job_id}/apply", follow_redirects=True)

    assert response.status_code == 200
    assert "Apply blocked" in response.text


def test_job_detail_shows_pursuit_and_comp_policy(tmp_path: Path):
    cfg, app, job_id = _seed(
        tmp_path,
        score=90,
        location="New York, NY",
        salary_min=145_000,
        salary_max=155_000,
    )
    conn = db.connect(cfg.db_path)
    profile = db.get_active_profile(conn)
    prefs = db.get_preferences(conn, profile.id or 0)
    job = db.get_job(conn, job_id)
    db.set_match(conn, score_job(job, profile, prefs))
    conn.close()
    response = TestClient(app).get(f"/jobs/{job_id}")

    assert response.status_code == 200
    assert "Pursuit" in response.text
    assert "Comp policy:" in response.text
    assert "meets target" in response.text


def test_apply_route_blocks_manual_only_before_spawning(tmp_path: Path, monkeypatch):
    _cfg, app, job_id = _seed(
        tmp_path, score=90, apply_url="https://www.indeed.com/viewjob?jk=abc123"
    )
    client = TestClient(app)

    def fail(*args, **kwargs):
        raise AssertionError("subprocess must not be launched")

    monkeypatch.setattr("jobforge.web.app.services.launch_apply_subprocess", fail)
    response = client.post(f"/jobs/{job_id}/apply", follow_redirects=True)

    assert response.status_code == 200
    assert "manual-only" in response.text


def test_queue_page_lists_built_items(tmp_path: Path):
    cfg, app, _job_id = _seed(tmp_path, score=90)
    conn = db.connect(cfg.db_path)
    profile = db.get_active_profile(conn)
    prefs = db.get_preferences(conn, profile.id or 0)
    queue = Queue(cfg.data_dir / "queue.json")
    queue.build(conn, profile, prefs, limit=5, min_score=0)
    conn.close()

    response = TestClient(app).get("/queue")

    assert response.status_code == 200
    assert "Application queue" in response.text
    assert "Acme" in response.text
    assert "pending" in response.text


def test_api_status(tmp_path: Path):
    app = create_app(_config(tmp_path))
    client = TestClient(app)
    response = client.get("/api/status")
    assert response.status_code == 200
    assert "jobs" in response.json()
