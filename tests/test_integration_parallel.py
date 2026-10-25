from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from jobforge import db, services
from jobforge.cli import build_parser
from jobforge.config import BrowserConfig, Config, EmailConfig, LLMConfig, ServerConfig
from jobforge.models import Job, MatchResult, Profile
from jobforge.web.app import create_app


def _config(tmp_path: Path, token: str = "") -> Config:
    cfg = Config(
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
        server=ServerConfig(access_token=token),
    )
    cfg.ensure_dirs()
    return cfg


def _seed(cfg: Config) -> tuple[int, int]:
    db.init_db(cfg.db_path)
    (cfg.data_dir / "network.yml").write_text(
        """
connections:
  - name: Alex Rivera
    company: Acme, Inc.
    role: Engineering Manager
    relationship: hiring_manager
    strength: strong
    last_contact: "2026-10-01"
    notes: Former colleague
""",
        encoding="utf-8",
    )
    conn = db.connect(cfg.db_path)
    profile = Profile(name="tester", first_name="Test", last_name="User", email="test@example.com")
    profile_id = db.save_profile(conn, profile)
    db.set_active_profile(conn, profile_id)
    job = Job(
        source="lever",
        source_id="1",
        company="Acme",
        title="Software Engineer",
        location="Remote",
        description="Requirements:\n- Python\n- FastAPI",
    )
    job_id = db.upsert_job(conn, job)
    db.set_match(
        conn,
        MatchResult(job_id=job_id, profile_id=profile_id, score=80, coverage=0.8, gate_survival=1.0),
    )
    conn.close()
    return profile_id, job_id


def test_warm_path_is_attached_without_changing_stored_score(tmp_path: Path):
    cfg = _config(tmp_path)
    profile_id, job_id = _seed(cfg)
    conn = db.connect(cfg.db_path)
    data = services.job_context(conn, job_id, profile_id, config=cfg)
    conn.close()
    assert data is not None
    assert data["match"]["score"] == 80
    assert data["warm"]["score"] >= 70
    assert data["warm"]["channel"] == "referral_first"
    assert data["warm"]["adjusted_fit"] == 95.0
    assert data["warm"]["contacts"][0]["name"] == "Alex Rivera"


def test_answers_router_and_warm_panel(tmp_path: Path):
    cfg = _config(tmp_path)
    _, job_id = _seed(cfg)
    app = create_app(cfg)
    client = TestClient(app)

    page = client.get("/answers")
    assert page.status_code == 200
    assert "Answer bank" in page.text

    saved = client.post(
        "/answers",
        data={"key": "notice period", "value": "2 weeks"},
        follow_redirects=True,
    )
    assert saved.status_code == 200
    overrides = json.loads((cfg.data_dir / "answers.json").read_text(encoding="utf-8"))
    assert overrides["notice period"] == "2 weeks"

    api = client.get("/api/answers")
    assert api.status_code == 200
    assert api.json()["notice period"] == "2 weeks"

    detail = client.get(f"/jobs/{job_id}")
    assert detail.status_code == 200
    assert "Warm path" in detail.text
    assert "Alex Rivera" in detail.text

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "Warm" in dashboard.text
    assert "referral first" in dashboard.text


def test_code_cli_is_registered():
    args = build_parser().parse_args(["code", "--company", "Acme", "--timeout", "1"])
    assert args.func.__name__ == "cmd_code"
    assert args.company == "Acme"


def test_cross_origin_writes_are_blocked(tmp_path: Path):
    cfg = _config(tmp_path)
    _seed(cfg)
    client = TestClient(create_app(cfg))
    evil = {"Origin": "https://evil.example"}

    post = client.post(
        "/answers",
        data={"key": "notice period", "value": "2 weeks"},
        headers=evil,
        follow_redirects=False,
    )
    assert post.status_code == 403

    put = client.put("/api/answers", json={"notice period": "2 weeks"}, headers=evil)
    assert put.status_code == 403


def test_cold_only_warm_path_is_hidden(tmp_path: Path):
    cfg = _config(tmp_path)
    profile_id, _ = _seed(cfg)
    conn = db.connect(cfg.db_path)
    job_id = db.upsert_job(
        conn,
        Job(
            source="lever",
            source_id="2",
            company="NoContacts",
            title="Software Engineer",
            location="Remote",
            description="Requirements:\n- Python",
        ),
    )
    db.set_match(
        conn,
        MatchResult(job_id=job_id, profile_id=profile_id, score=80, coverage=0.8, gate_survival=1.0),
    )
    conn.close()
    client = TestClient(create_app(cfg))

    detail = client.get(f"/jobs/{job_id}")
    assert detail.status_code == 200
    assert "Warm path" not in detail.text

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "cold only" not in dashboard.text


def test_origin_must_match_exactly(tmp_path: Path):
    cfg = _config(tmp_path)
    _seed(cfg)
    client = TestClient(create_app(cfg))

    spoofed = client.post(
        "/answers",
        data={"key": "notice period", "value": "2 weeks"},
        headers={"Origin": "https://jobforge.local.evil.com", "Host": "jobforge.local"},
        follow_redirects=False,
    )
    assert spoofed.status_code == 403

    same_origin = client.post(
        "/answers",
        data={"key": "notice period", "value": "2 weeks"},
        headers={"Origin": "http://testserver", "Host": "testserver"},
        follow_redirects=False,
    )
    assert same_origin.status_code in (200, 303)

    missing_host = client.post(
        "/answers",
        data={"key": "notice period", "value": "2 weeks"},
        headers={"Origin": "http://testserver", "Host": ""},
        follow_redirects=False,
    )
    assert missing_host.status_code == 403


def test_access_token_protects_reads_and_writes(tmp_path: Path):
    cfg = _config(tmp_path, token="s3cret")
    _seed(cfg)
    client = TestClient(create_app(cfg))

    assert client.get("/healthz").status_code == 200
    assert client.get("/api/answers").status_code == 401
    assert client.put("/api/answers", json={"notice period": "2 weeks"}).status_code == 401
    assert client.post("/answers", data={"key": "notice period", "value": "2 weeks"}).status_code == 401

    authed = client.get("/api/answers", headers={"Authorization": "Bearer s3cret"})
    assert authed.status_code == 200

    login = client.get("/auth?token=s3cret", follow_redirects=False)
    assert login.status_code == 303
    assert client.get("/api/answers").status_code == 200


def test_workday_is_wired_into_registry_and_adapter():
    from jobforge.automation.base import get_adapter
    from jobforge.automation.workday import WorkdayAdapter
    from jobforge.sources.registry import SOURCES, get_source
    from jobforge.sources.workday import WorkdaySource

    assert "workday" in SOURCES
    assert isinstance(get_source("workday"), WorkdaySource)
    adapter = get_adapter("https://acme.wd1.myworkdayjobs.com/en-US/External/job/123")
    assert isinstance(adapter, WorkdayAdapter)
