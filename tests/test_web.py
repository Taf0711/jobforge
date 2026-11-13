from __future__ import annotations

import json
import threading
from pathlib import Path
from urllib.parse import unquote

from fastapi.testclient import TestClient

from jobforge import db
from jobforge.config import Config, BrowserConfig, EmailConfig, LLMConfig, ServerConfig
from jobforge.locking import action_lock
from jobforge.matching.score import score_job
from jobforge.models import Application, Job, MatchResult, Preferences, Profile
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
    assert "Experience:" in response.text


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

    artifact = client.get(f"/artifacts/{app_id}/resume")
    assert artifact.status_code == 200
    disposition = artifact.headers["content-disposition"]
    assert disposition.startswith("inline;")
    assert "_Resume.pdf" in disposition

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


def test_workbench_shell_has_live_store_identity_and_no_more_overlay(tmp_path: Path):
    cfg = _config(tmp_path)
    cfg.ensure_dirs()
    app = create_app(cfg)
    conn = db.connect(cfg.db_path)
    profile_id = db.save_profile(conn, Profile(name="web-test"))
    db.set_active_profile(conn, profile_id)
    db.setting_set(conn, "setup_complete", "1")
    conn.close()

    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert "Live store" in response.text
    assert "v" in response.text
    assert "nav-more" not in response.text
    assert "More" not in response.text


def test_jobs_page_labels_filters_and_paginates(tmp_path: Path):
    cfg = _config(tmp_path)
    cfg.ensure_dirs()
    app = create_app(cfg)
    conn = db.connect(cfg.db_path)
    profile_id = db.save_profile(conn, Profile(name="web-test"))
    job_id = db.upsert_job(
        conn,
        Job(source="greenhouse", source_id="ui-1", company="Acme", title="Engineer"),
    )
    db.set_match(
        conn,
        MatchResult(job_id=job_id, profile_id=profile_id, score=80, coverage=0.8, gate_survival=1.0),
    )
    db.set_active_profile(conn, profile_id)
    db.setting_set(conn, "setup_complete", "1")
    conn.close()

    response = TestClient(app).get("/jobs")
    assert response.status_code == 200
    assert 'for="jobs-min-fit"' in response.text
    assert "Page 1 of 1" in response.text
    assert "Minimum fit" in response.text


def test_queue_build_skip_and_reopen_routes(tmp_path: Path):
    cfg = _config(tmp_path)
    cfg.ensure_dirs()
    app = create_app(cfg)
    conn = db.connect(cfg.db_path)
    profile_id = db.save_profile(conn, Profile(name="web-test"))
    db.save_preferences(conn, profile_id, Preferences(min_match=55))
    job_id = db.upsert_job(
        conn,
        Job(source="greenhouse", source_id="queue-ui-1", company="Acme", title="Engineer"),
    )
    db.set_match(
        conn,
        MatchResult(job_id=job_id, profile_id=profile_id, score=90, coverage=0.9, gate_survival=1.0),
    )
    db.set_active_profile(conn, profile_id)
    db.setting_set(conn, "setup_complete", "1")
    conn.close()

    client = TestClient(app)
    built = client.post("/queue/build", data={"limit": "10"}, follow_redirects=False)
    assert built.status_code == 303
    queue = json.loads((cfg.data_dir / "queue.json").read_text(encoding="utf-8"))
    assert [item["job_id"] for item in queue["items"]] == [job_id]

    skipped = client.post(f"/queue/{job_id}/skip", follow_redirects=False)
    assert skipped.status_code == 303
    queue = json.loads((cfg.data_dir / "queue.json").read_text(encoding="utf-8"))
    assert queue["items"][0]["status"] == "skipped"

    reopened = client.post(f"/queue/{job_id}/reopen", follow_redirects=False)
    assert reopened.status_code == 303
    queue = json.loads((cfg.data_dir / "queue.json").read_text(encoding="utf-8"))
    assert queue["items"][0]["status"] == "pending"


def test_api_status_identifies_the_live_store(tmp_path: Path):
    cfg = _config(tmp_path)
    cfg.ensure_dirs()
    app = create_app(cfg)
    conn = db.connect(cfg.db_path)
    profile_id = db.save_profile(conn, Profile(name="web-test"))
    db.set_active_profile(conn, profile_id)
    conn.close()

    data = TestClient(app).get("/api/status").json()
    assert data["profile"] == "web-test"
    assert data["data_dir"] == str(cfg.data_dir)
    assert data["db_path"] == str(cfg.db_path)
    assert data["port"] == cfg.server.port
    assert data["pid"] > 0


def test_import_job_url_route_stores_and_redirects(monkeypatch, tmp_path: Path):
    from jobforge import services
    from jobforge.models import Job

    cfg, app, _ = _seed(tmp_path)
    fetched = Job(
        source="direct",
        source_id="url-1",
        company="URL Co",
        title="URL Engineer",
        location="Remote",
        url="https://example.com/jobs/1",
        apply_url="https://example.com/jobs/1",
        description="Python, React, and SQL role for a new grad.",
    )
    monkeypatch.setattr(services, "fetch_job_url", lambda url: fetched)

    client = TestClient(app)
    response = client.post(
        "/jobs/import-url",
        data={"url": "https://example.com/jobs/1"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "/jobs/" in response.headers["location"]
    conn = db.connect(cfg.db_path)
    row = conn.execute("SELECT company, title FROM jobs WHERE source='direct'").fetchone()
    conn.close()
    assert row["company"] == "URL Co"
    assert row["title"] == "URL Engineer"

def test_application_tracker_links_email_and_shows_timeline(tmp_path: Path):
    cfg, app, job_id = _seed(tmp_path)
    conn = db.connect(cfg.db_path)
    profile = db.get_active_profile(conn)
    app_id = db.create_application(
        conn,
        Application(job_id=job_id, profile_id=profile.id, status="draft", ats="lever"),
    )
    from jobforge.models import EmailMessage

    db.save_email(
        conn,
        EmailMessage(
            message_id="<tracker@acme>",
            sender="Acme Recruiting <no-reply@acme.com>",
            subject="Application received",
            body="Thank you for applying to the Engineer role at Acme. We received your application.",
            received_at="2026-11-12T12:00:00+00:00",
            category="confirmation",
        ),
    )
    email_id = conn.execute("SELECT id FROM emails WHERE message_id='<tracker@acme>'").fetchone()["id"]
    conn.close()

    client = TestClient(app)
    detail = client.get(f"/applications/{app_id}")
    assert detail.status_code == 200
    assert "Acme" in detail.text
    assert "Timeline" in detail.text

    inbox = client.get("/inbox")
    assert inbox.status_code == 200
    assert "Application received" in inbox.text

    linked = client.post(
        f"/email/{email_id}/link",
        data={"application_id": str(app_id)},
        follow_redirects=False,
    )
    assert linked.status_code == 303

    conn = db.connect(cfg.db_path)
    stored = db.get_application(conn, app_id)
    assert stored.status == "submitted"
    assert stored.status_source == "email"
    email_row = db.get_email(conn, email_id)
    assert email_row["application_id"] == app_id
    assert email_row["match_source"] == "manual"
    assert email_row["status_after"] == "submitted"
    conn.close()

    tracker = client.get("/applications")
    assert "via email" in tracker.text
    assert "Application received" in tracker.text

    unlinked = client.post(f"/email/{email_id}/unlink", follow_redirects=False)
    assert unlinked.status_code == 303
    conn = db.connect(cfg.db_path)
    assert db.get_email(conn, email_id)["match_source"] == "manual_ignore"
    conn.close()

def test_email_settings_save_never_writes_the_password(tmp_path: Path, monkeypatch):
    import yaml

    from jobforge.config import EmailConfig

    monkeypatch.setenv("JOBFORGE_DISABLE_EMAIL_AUTO_POLL", "1")

    cfg = _config(tmp_path)
    cfg.email = EmailConfig(
        enabled=False,
        host="imap.example.com",
        port=993,
        username="",
        secret_dir=str(tmp_path / "data" / "connections" / "email"),
    )
    cfg.config_path = tmp_path / "config.yml"
    cfg.ensure_dirs()
    app = create_app(cfg)
    conn = db.connect(cfg.db_path)
    profile_id = db.save_profile(conn, Profile(name="settings-test"))
    db.set_active_profile(conn, profile_id)
    db.setting_set(conn, "setup_complete", "1")
    conn.close()

    client = TestClient(app)
    response = client.post(
        "/settings/email",
        data={
            "enabled": "1",
            "host": "imap.example.com",
            "port": "993",
            "username": "me@example.com",
            "folder": "INBOX",
            "lookback_days": "14",
            "auto_poll": "1",
            "poll_interval_minutes": "10",
            "timeout_seconds": "20",
            "status_window_days": "45",
            "password": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    data = yaml.safe_load(cfg.config_path.read_text()) or {}
    email = data["email"]
    assert email["enabled"] is True
    assert email["username"] == "me@example.com"
    assert email["lookback_days"] == 14
    assert "password" not in email
    assert cfg.email.enabled is True
    assert cfg.email.poll_interval_minutes == 10

def test_work_authorization_save_and_rescore(tmp_path: Path):
    cfg, app, _job_id = _seed(tmp_path)
    client = TestClient(app)
    saved = client.post(
        "/profiles/work-auth",
        data={
            "us_citizen": "yes",
            "itar_eligible": "no",
            "clearance_eligible": "yes",
            "authorized": "yes",
            "needs_sponsorship": "no",
            "clearance": "secret",
            "gpa": "3.5",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    conn = db.connect(cfg.db_path)
    profile = db.get_active_profile(conn)
    assert profile.work_auth["us_citizen"] is True
    assert profile.work_auth["itar_eligible"] is False
    assert profile.work_auth["clearance_eligible"] is True
    assert profile.work_auth["authorized"] is True
    assert profile.work_auth["needs_sponsorship"] is False
    assert profile.work_auth["clearance"] == "secret"
    assert profile.work_auth["gpa"] == 3.5
    conn.close()

    cleared = client.post(
        "/profiles/work-auth",
        data={
            "us_citizen": "unknown",
            "itar_eligible": "unknown",
            "clearance_eligible": "unknown",
            "authorized": "unknown",
            "needs_sponsorship": "unknown",
            "clearance": "",
            "gpa": "",
        },
        follow_redirects=False,
    )
    assert cleared.status_code == 303
    conn = db.connect(cfg.db_path)
    profile = db.get_active_profile(conn)
    for key in (
        "us_citizen",
        "itar_eligible",
        "clearance_eligible",
        "authorized",
        "needs_sponsorship",
        "clearance",
        "gpa",
    ):
        assert key not in profile.work_auth
    conn.close()
