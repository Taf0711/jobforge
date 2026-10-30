from __future__ import annotations

import threading
from pathlib import Path
from urllib.parse import unquote

from fastapi.testclient import TestClient

from jobforge import db
from jobforge.config import Config, BrowserConfig, EmailConfig, LLMConfig, ServerConfig
from jobforge.locking import action_lock
from jobforge.matching.score import score_job
from jobforge.models import Application, Job, MatchResult, Profile
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
    cfg = _config(tmp_path)
    cfg.ensure_dirs()
    app = create_app(cfg)
    conn = db.connect(cfg.db_path)
    profile_id = db.save_profile(conn, Profile(name="web-test"))
    db.set_active_profile(conn, profile_id)
    db.setting_set(conn, "setup_complete", "1")
    conn.close()

    client = TestClient(app)
    for path in ["/", "/jobs", "/agents", "/queue", "/applications", "/profiles", "/settings", "/inbox", "/runs", "/healthz"]:
        response = client.get(path)
        assert response.status_code == 200, path
        assert "JobForge" in response.text or path == "/healthz"


def test_read_pages_require_setup_without_creating_state(tmp_path: Path):
    cfg = _config(tmp_path)
    cfg.ensure_dirs()
    app = create_app(cfg)
    client = TestClient(app)

    dashboard = client.get("/", follow_redirects=False)
    assert dashboard.status_code == 303
    assert dashboard.headers["location"] == "/setup"

    setup_page = client.get("/setup")
    assert setup_page.status_code == 200
    assert "JobForge" in setup_page.text

    api = client.get("/api/status")
    assert api.status_code == 409
    assert api.json()["error"] == "setup_required"

    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT COUNT(*) AS n FROM profiles").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM preferences").fetchone()["n"] == 0
    conn.close()


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
    assert 'data-ask-surface' in response.text


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
    _cfg, app, _job_id = _seed(tmp_path)
    client = TestClient(app)
    response = client.get("/api/status")
    assert response.status_code == 200
    assert "jobs" in response.json()


def test_apply_route_forces_review_only_even_when_config_allows_submit(
    tmp_path: Path, monkeypatch
):
    from jobforge.quality import QualityReport

    cfg, app, job_id = _seed(tmp_path, score=90)
    cfg.browser.allow_submit = True
    monkeypatch.setattr(
        "jobforge.web.app.quality.evaluate",
        lambda *args, **kwargs: QualityReport(score=100, ready=True, checks=[]),
    )
    seen: dict = {}

    def fake_launch(config, requested_job_id, hold_seconds=600, **kwargs):
        seen.update(
            {
                "job_id": requested_job_id,
                "hold_seconds": hold_seconds,
                **kwargs,
            }
        )
        return 4242

    monkeypatch.setattr(
        "jobforge.web.app.services.launch_apply_subprocess", fake_launch
    )
    response = TestClient(app).post(f"/jobs/{job_id}/apply", follow_redirects=True)

    assert response.status_code == 200
    assert seen["job_id"] == job_id
    assert seen["review_only"] is True


def test_application_tracking_is_profile_scoped_and_uses_artifact_routes(
    tmp_path: Path,
):
    cfg, app, job_id = _seed(tmp_path)
    conn = db.connect(cfg.db_path)
    profile_id = db.get_active_profile(conn).id or 0
    resume = Path(cfg.output_dir) / "profile-1" / "resume.pdf"
    resume.parent.mkdir(parents=True, exist_ok=True)
    resume.write_bytes(b"%PDF-1.4\n")
    screenshot = Path(cfg.data_dir) / "screenshots" / "pre-submit.png"
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    screenshot.write_bytes(b"\x89PNG\r\n\x1a\n")
    app_id = db.create_application(
        conn,
        Application(
            job_id=job_id,
            profile_id=profile_id,
            status="prepared",
            ats="lever",
            resume_path=str(resume),
            screenshot_path=str(screenshot),
            answers={"Why this role?": "Because I build reliable systems."},
        ),
    )
    conn.close()
    client = TestClient(app)

    detail = client.get(f"/jobs/{job_id}")
    assert detail.status_code == 200
    assert "Because I build reliable systems." in detail.text
    assert f"/artifacts/{app_id}/resume" in detail.text
    assert f"/artifacts/{app_id}/screenshot" in detail.text
    assert "file://" not in detail.text

    listing = client.get("/applications")
    assert f"/artifacts/{app_id}/resume" in listing.text
    assert "data-autosubmit" in listing.text
    assert "file://" not in listing.text

    assert client.get(f"/artifacts/{app_id}/screenshot").status_code == 200

    submitted = client.post(
        f"/applications/{app_id}/status",
        data={"status": "submitted"},
        follow_redirects=False,
    )
    assert submitted.status_code == 303
    conn = db.connect(cfg.db_path)
    stored = db.get_application(conn, app_id)
    assert stored is not None
    assert stored.status == "submitted"
    assert stored.submitted_at

    other_id = db.save_profile(conn, Profile(name="Other profile"))
    db.set_active_profile(conn, other_id)
    conn.close()
    blocked = client.post(
        f"/applications/{app_id}/status",
        data={"status": "rejected"},
        follow_redirects=False,
    )
    assert blocked.status_code == 404


def test_write_returns_busy_while_another_action_holds_the_lock(tmp_path: Path):
    cfg, app, job_id = _seed(tmp_path)
    client = TestClient(app)
    entered = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with action_lock(cfg):
            entered.set()
            release.wait(timeout=5)

    thread = threading.Thread(target=holder)
    thread.start()
    assert entered.wait(timeout=5)
    try:
        response = client.post(f"/jobs/{job_id}/save", follow_redirects=False)
    finally:
        release.set()
        thread.join(timeout=5)

    assert response.status_code == 303
    assert "Another JobForge action is running" in unquote(response.headers["location"])
