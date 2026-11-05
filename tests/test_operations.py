from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from jobforge import db, quality, risk
from jobforge.config import BrowserConfig, Config, EmailConfig, LLMConfig, ServerConfig
from jobforge.models import Application, Job, MatchResult, Profile
from jobforge.web.app import create_app
from jobforge.workbench.worker import run_operation


def _config(tmp_path: Path) -> Config:
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


def _seed(tmp_path: Path):
    cfg = _config(tmp_path)
    conn = db.connect(cfg.db_path)
    base = tmp_path / "base.pdf"
    base.write_bytes(b"%PDF-1.4\n")
    profile_id = db.save_profile(
        conn,
        Profile(
            name="Operations Test",
            first_name="Test",
            last_name="User",
            resume_path=str(base),
            skills=["Python"],
        ),
    )
    db.set_active_profile(conn, profile_id)
    job_id = db.upsert_job(
        conn,
        Job(
            source="greenhouse",
            source_id="ops-job",
            company="Ops Co",
            title="Backend Engineer",
            description="Python backend role",
        ),
    )
    db.set_match(
        conn,
        MatchResult(
            job_id=job_id,
            profile_id=profile_id,
            score=80.0,
            coverage=0.8,
            gate_survival=1.0,
        ),
    )
    instance = db.create_agent_instance(conn, "ops-test")
    connection = db.create_agent_connection(
        conn,
        instance_id=str(instance["id"]),
        profile_id=profile_id,
        scopes=["proposals:create", "profile:resume_context"],
    )
    operation = db.create_agent_operation(
        conn,
        connection_id=str(connection["id"]),
        profile_id=profile_id,
        action="render_resume",
        payload={"job_id": job_id},
        idempotency_key="ops-test-1",
    )
    conn.close()
    return cfg, profile_id, job_id, str(connection["id"]), operation


def test_operations_page_approval_and_deny(tmp_path: Path, monkeypatch):
    cfg, _profile_id, _job_id, _connection_id, operation = _seed(tmp_path)
    launched: list[str] = []
    monkeypatch.setattr(
        "jobforge.web.app.launch_worker", lambda config, operation_id: launched.append(operation_id) or 123
    )
    client = TestClient(create_app(cfg))

    page = client.get("/operations")
    assert page.status_code == 200
    assert "Ops Co" in page.text
    assert "pending_approval" in page.text

    detail = client.get(f"/operations/{operation['id']}")
    assert detail.status_code == 200
    assert "Approve and render" in detail.text

    approved = client.post(
        f"/operations/{operation['id']}/approve", follow_redirects=False
    )
    assert approved.status_code == 303
    assert launched == [operation["id"]]
    conn = db.connect(cfg.db_path)
    assert db.get_agent_operation(conn, operation["id"])["state"] == "approved"
    conn.close()

    denied = client.post(
        f"/operations/{operation['id']}/deny", follow_redirects=False
    )
    assert denied.status_code == 303
    conn = db.connect(cfg.db_path)
    assert db.get_agent_operation(conn, operation["id"])["state"] == "approved"
    conn.close()


def test_artifact_route_serves_only_output_pdfs(tmp_path: Path):
    cfg, profile_id, job_id, _connection_id, _operation = _seed(tmp_path)
    good = Path(cfg.output_dir) / "tailored.pdf"
    good.write_bytes(b"%PDF-1.4\nartifact\n")
    bad = tmp_path / "outside.pdf"
    bad.write_bytes(b"%PDF-1.4\noutside\n")
    conn = db.connect(cfg.db_path)
    good_app = db.create_application(
        conn,
        Application(
            job_id=job_id,
            profile_id=profile_id,
            status="draft",
            ats="greenhouse",
            resume_path=str(good),
        ),
    )
    bad_job = db.upsert_job(
        conn,
        Job(
            source="greenhouse",
            source_id="ops-bad",
            company="Bad Co",
            title="Bad Role",
            description="Python",
        ),
    )
    bad_app = db.create_application(
        conn,
        Application(
            job_id=bad_job,
            profile_id=profile_id,
            status="draft",
            resume_path=str(bad),
        ),
    )
    conn.close()
    client = TestClient(create_app(cfg))
    response = client.get(f"/artifacts/{good_app}/resume")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")
    assert client.get(f"/artifacts/{bad_app}/resume").status_code == 404


def test_worker_claims_and_finishes_a_resume_operation(tmp_path: Path, monkeypatch):
    cfg, _profile_id, job_id, _connection_id, operation = _seed(tmp_path)
    conn = db.connect(cfg.db_path)
    db.approve_agent_operation(conn, operation["id"])
    conn.close()

    monkeypatch.setattr(
        "jobforge.workbench.worker.services.tailor_job",
        lambda config, conn, requested_job_id, *, profile_id=None: {
            "application_id": 42,
            "ats": {"ok": True},
            "facts": 3,
            "warnings": [],
            "llm_used": False,
            "llm_model": "",
            "mode": "deterministic",
        },
    )
    conn = db.connect(cfg.db_path)
    try:
        result = run_operation(cfg, conn, operation["id"])
    finally:
        conn.close()
    assert result["state"] == "succeeded"
    assert result["result"]["application_id"] == 42


def test_worker_rechecks_a_revoked_connection(tmp_path: Path, monkeypatch):
    cfg, _profile_id, _job_id, connection_id, operation = _seed(tmp_path)
    conn = db.connect(cfg.db_path)
    db.approve_agent_operation(conn, operation["id"])
    db.disable_agent_connection(conn, connection_id)
    conn.close()

    def fail(*args, **kwargs):
        raise AssertionError("tailor must not run for a revoked connection")

    monkeypatch.setattr("jobforge.workbench.worker.services.tailor_job", fail)
    conn = db.connect(cfg.db_path)
    try:
        result = run_operation(cfg, conn, operation["id"])
    finally:
        conn.close()
    assert result["state"] == "failed"
    assert result["error_code"] == "CONNECTION_DISABLED"


def test_worker_renders_for_the_approved_profile_not_the_active_one(
    tmp_path: Path, monkeypatch
):
    cfg, profile_id, _job_id, _connection_id, operation = _seed(tmp_path)
    conn = db.connect(cfg.db_path)
    other_id = db.save_profile(conn, Profile(name="Other profile"))
    db.set_active_profile(conn, other_id)
    db.approve_agent_operation(conn, operation["id"])
    conn.close()
    seen = {}

    def fake_tailor(config, connection, requested_job_id, *, profile_id=None):
        seen["profile_id"] = profile_id
        return {
            "application_id": 42,
            "ats": {"ok": True},
            "facts": 0,
            "warnings": [],
            "llm_used": False,
            "llm_model": "",
            "mode": "deterministic",
        }

    monkeypatch.setattr("jobforge.workbench.worker.services.tailor_job", fake_tailor)
    conn = db.connect(cfg.db_path)
    try:
        result = run_operation(cfg, conn, operation["id"])
    finally:
        conn.close()
    assert result["state"] == "succeeded"
    assert seen["profile_id"] == profile_id
    assert seen["profile_id"] != other_id


def test_worker_does_not_run_before_approval(tmp_path: Path, monkeypatch):
    cfg, _profile_id, _job_id, _connection_id, operation = _seed(tmp_path)

    def fail(*args, **kwargs):
        raise AssertionError("tailor must not run")

    monkeypatch.setattr("jobforge.workbench.worker.services.tailor_job", fail)
    conn = db.connect(cfg.db_path)
    try:
        result = run_operation(cfg, conn, operation["id"])
    finally:
        conn.close()
    assert result["state"] == "pending_approval"


def test_worker_failure_is_recorded_without_a_path(tmp_path: Path, monkeypatch):
    cfg, _profile_id, _job_id, _connection_id, operation = _seed(tmp_path)
    conn = db.connect(cfg.db_path)
    db.approve_agent_operation(conn, operation["id"])
    conn.close()

    def fail(*args, **kwargs):
        raise ValueError(f"no base resume at {tmp_path}/secret.pdf")

    monkeypatch.setattr("jobforge.workbench.worker.services.tailor_job", fail)
    conn = db.connect(cfg.db_path)
    try:
        result = run_operation(cfg, conn, operation["id"])
    finally:
        conn.close()
    assert result["state"] == "failed"
    assert result["error_code"] == "RENDER_FAILED"
    assert str(tmp_path) not in result["error_message"]
    assert "[local path]" in result["error_message"]


def _submission_operation(tmp_path: Path):
    cfg, profile_id, job_id, connection_id, _ = _seed(tmp_path)
    conn = db.connect(cfg.db_path)
    db.update_agent_connection_scopes(conn, connection_id, ["submission:request"])
    operation = db.create_agent_operation(
        conn,
        connection_id=connection_id,
        profile_id=profile_id,
        action="submit_application",
        payload={"job_id": job_id},
        idempotency_key="submit-test-1",
    )
    conn.close()
    return cfg, profile_id, job_id, connection_id, operation


def test_submission_approval_page_shows_risk_and_action(tmp_path: Path):
    cfg, _profile_id, _job_id, _connection_id, operation = _submission_operation(tmp_path)
    client = TestClient(create_app(cfg))

    detail = client.get(f"/operations/{operation['id']}")

    assert detail.status_code == 200
    assert "Submission request" in detail.text
    assert "Approve and submit" in detail.text
    assert "Risk consent is not ready" in detail.text
    assert "Daily cap" in detail.text


def test_worker_submits_only_after_approval_and_rechecks_profile(
    tmp_path: Path, monkeypatch
):
    cfg, profile_id, _job_id, _connection_id, operation = _submission_operation(tmp_path)
    conn = db.connect(cfg.db_path)
    other_id = db.save_profile(conn, Profile(name="Other profile"))
    db.set_active_profile(conn, other_id)
    db.approve_agent_operation(conn, operation["id"])
    conn.close()
    seen = {}

    def fake_apply(config, connection, job_id, **kwargs):
        seen.update(kwargs)
        seen["job_id"] = job_id
        return {
            "application_id": 99,
            "submitted": True,
            "captcha": False,
            "ats": {"ats": "greenhouse"},
            "warnings": [],
        }

    monkeypatch.setattr("jobforge.workbench.worker.services.apply_job", fake_apply)
    conn = db.connect(cfg.db_path)
    try:
        result = run_operation(cfg, conn, operation["id"])
    finally:
        conn.close()

    assert result["state"] == "succeeded"
    assert result["result"]["submitted"] is True
    from jobforge.workbench.actions import _operation_public

    assert _operation_public(result)["submission_performed_by_jobforge"] is True
    assert seen["allow_submit"] is True
    assert seen["submission_mode"] == "unattended"
    assert seen["review_only"] is False
    assert seen["profile_id"] == profile_id
    assert seen["profile_id"] != other_id


def test_worker_maps_daily_cap_refusal(tmp_path: Path, monkeypatch):
    cfg, _profile_id, _job_id, _connection_id, operation = _submission_operation(tmp_path)
    conn = db.connect(cfg.db_path)
    db.approve_agent_operation(conn, operation["id"])
    conn.close()

    def fail(*args, **kwargs):
        raise risk.RiskError("Daily submit cap reached (10). Wait until tomorrow.")

    monkeypatch.setattr("jobforge.workbench.worker.services.apply_job", fail)
    conn = db.connect(cfg.db_path)
    try:
        result = run_operation(cfg, conn, operation["id"])
    finally:
        conn.close()

    assert result["state"] == "failed"
    assert result["error_code"] == "DAILY_CAP_REACHED"


def test_worker_blocks_submission_when_scope_is_revoked(tmp_path: Path, monkeypatch):
    cfg, _profile_id, _job_id, connection_id, operation = _submission_operation(tmp_path)
    conn = db.connect(cfg.db_path)
    db.approve_agent_operation(conn, operation["id"])
    conn.execute(
        "UPDATE agent_connections SET scopes_json=?, generation=generation+1 WHERE id=?",
        (json.dumps(["jobs:read"]), connection_id),
    )
    conn.commit()
    conn.close()

    def fail(*args, **kwargs):
        raise AssertionError("apply_job must not run without the submission scope")

    monkeypatch.setattr("jobforge.workbench.worker.services.apply_job", fail)
    conn = db.connect(cfg.db_path)
    try:
        result = run_operation(cfg, conn, operation["id"])
    finally:
        conn.close()

    assert result["state"] == "failed"
    assert result["error_code"] == "SCOPE_DENIED"


def test_worker_maps_quality_block_and_does_not_report_submission(
    tmp_path: Path, monkeypatch
):
    cfg, _profile_id, _job_id, _connection_id, operation = _submission_operation(tmp_path)
    conn = db.connect(cfg.db_path)
    db.approve_agent_operation(conn, operation["id"])
    conn.close()

    def fail(*args, **kwargs):
        raise quality.QualityBlocked(
            quality.QualityReport(
                score=0,
                ready=False,
                checks=[
                    quality.QualityCheck(
                        name="match_score",
                        passed=False,
                        severity=quality.BLOCKER,
                        detail="match 10 is below minimum 55",
                    )
                ],
            )
        )

    monkeypatch.setattr("jobforge.workbench.worker.services.apply_job", fail)
    conn = db.connect(cfg.db_path)
    try:
        result = run_operation(cfg, conn, operation["id"])
    finally:
        conn.close()

    assert result["state"] == "failed"
    assert result["error_code"] == "QUALITY_BLOCKED"
    assert result["result"].get("submitted") is not True


def test_worker_records_captcha_as_handoff(tmp_path: Path, monkeypatch):
    cfg, _profile_id, _job_id, _connection_id, operation = _submission_operation(tmp_path)
    conn = db.connect(cfg.db_path)
    db.approve_agent_operation(conn, operation["id"])
    conn.close()

    def fake_apply(*args, **kwargs):
        return {
            "application_id": 17,
            "submitted": False,
            "captcha": True,
            "ats": {"ats": "greenhouse"},
            "warnings": ["captcha present"],
        }

    monkeypatch.setattr("jobforge.workbench.worker.services.apply_job", fake_apply)
    conn = db.connect(cfg.db_path)
    try:
        result = run_operation(cfg, conn, operation["id"])
    finally:
        conn.close()

    assert result["state"] == "failed"
    assert result["error_code"] == "CAPTCHA_REQUIRED"
    assert result["result"]["submitted"] is False
    assert result["result"]["captcha"] is True


def test_worker_expires_an_old_submission_approval(tmp_path: Path, monkeypatch):
    cfg, _profile_id, _job_id, _connection_id, operation = _submission_operation(tmp_path)
    conn = db.connect(cfg.db_path)
    db.approve_agent_operation(conn, operation["id"])
    old = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat(
        timespec="seconds"
    )
    conn.execute(
        "UPDATE agent_operations SET approved_at=? WHERE id=?",
        (old, operation["id"]),
    )
    conn.commit()
    conn.close()

    def fail(*args, **kwargs):
        raise AssertionError("apply_job must not run after the approval window")

    monkeypatch.setattr("jobforge.workbench.worker.services.apply_job", fail)
    conn = db.connect(cfg.db_path)
    try:
        result = run_operation(cfg, conn, operation["id"])
    finally:
        conn.close()

    assert result["state"] == "expired"
