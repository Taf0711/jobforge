from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobforge import db, risk, services
from jobforge.automation.base import FillReport
from jobforge.config import BrowserConfig, Config, EmailConfig, LLMConfig, ServerConfig
from jobforge.models import Job, MatchResult, Profile
from jobforge.queue import Queue
from jobforge.web.app import create_app


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


def _seed(cfg: Config, tmp_path: Path) -> tuple[int, int]:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n")
    conn = db.connect(cfg.db_path)
    profile = Profile(name="risk-test", resume_path=str(resume), email="risk@example.com")
    profile_id = db.save_profile(conn, profile)
    db.set_active_profile(conn, profile_id)
    job = Job(
        source="lever",
        source_id="risk-1",
        company="Acme",
        title="Software Engineer",
        apply_url="https://jobs.example/apply",
        description="Requirements:\n- Python",
    )
    job_id = db.upsert_job(conn, job)
    conn.close()
    return profile_id, job_id


def test_default_is_off_and_warning_is_direct(tmp_path: Path):
    cfg = _config(tmp_path)
    policy = risk.load_policy(cfg)
    assert policy.unattended_submit is False
    assert "OFF BY DEFAULT" in risk.warning_text("unattended_submit")
    assert "never solves" in risk.warning_text("captcha_solver")
    assert "NOT AVAILABLE" in risk.warning_text("indeed_linkedin_automation")


def test_enable_requires_exact_phrase_and_sets_0600(tmp_path: Path):
    cfg = _config(tmp_path)
    with pytest.raises(risk.RiskError):
        risk.enable(cfg, "unattended_submit", "yes")

    policy = risk.enable(cfg, "unattended_submit", risk.CONSENT_PHRASE)
    assert policy.unattended_submit is True
    assert policy.consented_at
    assert (cfg.data_dir / "risk.json").stat().st_mode & 0o777 == 0o600


def test_unavailable_modes_are_rejected(tmp_path: Path):
    cfg = _config(tmp_path)
    for flag in risk.UNAVAILABLE_FLAGS:
        with pytest.raises(risk.RiskError):
            risk.enable(cfg, flag, risk.CONSENT_PHRASE)


def test_malformed_cap_falls_back_to_default(tmp_path: Path):
    cfg = _config(tmp_path)
    (cfg.data_dir / "risk.json").write_text(
        '{"unattended_submit": true, "consented_at": "2026-01-01", '
        '"consent_phrase": "I UNDERSTAND THE RISK", "daily_submit_cap": "ten"}',
        encoding="utf-8",
    )
    policy = risk.load_policy(cfg)
    assert policy.daily_submit_cap == 10
    assert risk.status(cfg)["policy"]["daily_submit_cap"] == 10


def test_hand_edited_flag_without_consent_stays_off(tmp_path: Path):
    cfg = _config(tmp_path)
    (cfg.data_dir / "risk.json").write_text('{"unattended_submit": true}', encoding="utf-8")
    assert risk.load_policy(cfg).unattended_submit is False


def test_daily_cap_blocks_and_rollover_resets(tmp_path: Path):
    cfg = _config(tmp_path)
    policy = risk.enable(cfg, "unattended_submit", risk.CONSENT_PHRASE)
    policy.daily_submit_cap = 2
    risk.save_policy(cfg, policy)

    risk.record_submission(cfg)
    risk.record_submission(cfg)
    allowed, reason, _ = risk.can_submit_unattended(cfg)
    assert allowed is False
    assert "cap" in reason.lower()

    (cfg.data_dir / "risk_usage.json").write_text('{"date": "2000-01-01", "count": 99}')
    allowed, reason, _ = risk.can_submit_unattended(cfg)
    assert allowed is True


def test_reserve_submission_checks_and_increments_atomically(tmp_path: Path):
    cfg = _config(tmp_path)
    policy = risk.enable(cfg, "unattended_submit", risk.CONSENT_PHRASE)
    policy.daily_submit_cap = 1
    risk.save_policy(cfg, policy)

    allowed, _reason, _policy = risk.reserve_submission(cfg)
    assert allowed is True
    allowed, reason, _policy = risk.reserve_submission(cfg)
    assert allowed is False
    assert "cap" in reason.lower()
    assert risk.load_usage(cfg)["count"] == 1


def test_reserve_submission_is_atomic_under_concurrency(tmp_path: Path):
    cfg = _config(tmp_path)
    policy = risk.enable(cfg, "unattended_submit", risk.CONSENT_PHRASE)
    policy.daily_submit_cap = 1
    risk.save_policy(cfg, policy)
    barrier = threading.Barrier(8)
    results: list[bool] = []

    def worker() -> None:
        barrier.wait()
        allowed, _reason, _policy = risk.reserve_submission(cfg)
        results.append(allowed)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(results) == 1
    assert risk.load_usage(cfg)["count"] == 1


def test_apply_job_reserves_before_browser_and_caps(tmp_path: Path, monkeypatch):
    cfg = _config(tmp_path)
    _profile_id, job_id = _seed(cfg, tmp_path)
    conn = db.connect(cfg.db_path)
    policy = risk.enable(cfg, "unattended_submit", risk.CONSENT_PHRASE)
    policy.daily_submit_cap = 1
    risk.save_policy(cfg, policy)
    seen_counts: list[int] = []

    def fake_run_application(*args, **kwargs):
        seen_counts.append(risk.load_usage(cfg)["count"])
        return FillReport(ats="fake", url="https://jobs.example/apply", submitted=True)

    monkeypatch.setattr("jobforge.automation.base.run_application", fake_run_application)
    services.apply_job(cfg, conn, job_id, allow_submit=True)

    with pytest.raises(risk.RiskError):
        services.apply_job(cfg, conn, job_id, allow_submit=True)

    conn.close()
    assert seen_counts == [1]
    assert risk.load_usage(cfg)["count"] == 1


def test_consent_changes_are_logged(tmp_path: Path):
    cfg = _config(tmp_path)
    conn = db.connect(cfg.db_path)
    risk.enable(cfg, "unattended_submit", risk.CONSENT_PHRASE, conn=conn)
    risk.disable(cfg, "unattended_submit", conn=conn)
    rows = conn.execute(
        "SELECT detail FROM events WHERE kind='risk_consent' ORDER BY id"
    ).fetchall()
    conn.close()

    details = [row[0] for row in rows]
    assert details == ["enabled unattended_submit cap=10", "disabled unattended_submit"]


def test_apply_requires_consent_and_records_usage(tmp_path: Path, monkeypatch):
    cfg = _config(tmp_path)
    _profile_id, job_id = _seed(cfg, tmp_path)
    conn = db.connect(cfg.db_path)

    with pytest.raises(risk.RiskError):
        services.apply_job(cfg, conn, job_id, allow_submit=True)

    risk.enable(cfg, "unattended_submit", risk.CONSENT_PHRASE)

    def fake_run_application(*args, **kwargs):
        return FillReport(ats="fake", url="https://jobs.example/apply", submitted=True)

    monkeypatch.setattr("jobforge.automation.base.run_application", fake_run_application)
    result = services.apply_job(cfg, conn, job_id, allow_submit=True)
    conn.close()

    assert result["submitted"] is True
    assert risk.load_usage(cfg)["count"] == 1


def test_queue_auto_submit_requires_consent(tmp_path: Path):
    cfg = _config(tmp_path)
    profile_id, job_id = _seed(cfg, tmp_path)
    conn = db.connect(cfg.db_path)
    db.set_match(
        conn,
        MatchResult(
            job_id=job_id,
            profile_id=profile_id,
            score=90,
            coverage=0.9,
            gate_survival=1.0,
        ),
    )
    queue = Queue(tmp_path / "queue.json")
    queue.build(conn, db.get_profile(conn, profile_id), db.get_preferences(conn, profile_id), limit=1, min_score=0)
    queue.mark_prepared(job_id, str(tmp_path / "resume.pdf"))
    conn.close()

    with pytest.raises(risk.RiskError):
        queue.apply_next(cfg, db.connect(cfg.db_path), auto_submit=True)


def test_risk_page_defaults_off_and_enables_with_phrase(tmp_path: Path):
    cfg = _config(tmp_path)
    _seed(cfg, tmp_path)
    client = TestClient(create_app(cfg))

    page = client.get("/settings/risk")
    assert page.status_code == 200
    assert "OFF (default)" in page.text
    assert "I UNDERSTAND THE RISK" in page.text

    wrong = client.post(
        "/settings/risk",
        data={"action": "enable", "flag": "unattended_submit", "acknowledge": "yes"},
        follow_redirects=True,
    )
    assert wrong.status_code == 200
    assert risk.load_policy(cfg).unattended_submit is False

    good = client.post(
        "/settings/risk",
        data={
            "action": "enable",
            "flag": "unattended_submit",
            "acknowledge": risk.CONSENT_PHRASE,
        },
        follow_redirects=True,
    )
    assert good.status_code == 200
    assert risk.load_policy(cfg).unattended_submit is True
    assert "ENABLED" in client.get("/settings/risk").text
