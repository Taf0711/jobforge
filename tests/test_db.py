from __future__ import annotations

from pathlib import Path

from jobforge import db
from jobforge.models import Application, Job, MatchResult, Preferences, Profile


def test_job_upsert_and_dedup(tmp_path: Path):
    path = tmp_path / "test.sqlite3"
    db.init_db(path)
    conn = db.connect(path)
    job = Job(source="lever", source_id="1", company="Acme", title="Software Engineer", location="NYC")
    first = db.upsert_job(conn, job)
    second = db.upsert_job(conn, job)
    assert first == second
    assert db.count_jobs(conn) == 1
    assert db.get_job(conn, first).title == "Software Engineer"


def test_profile_preferences_and_match(tmp_path: Path):
    path = tmp_path / "test.sqlite3"
    db.init_db(path)
    conn = db.connect(path)
    profile = Profile(name="tester", first_name="Test", last_name="User", skills=["Python"])
    pid = db.save_profile(conn, profile)
    db.save_preferences(conn, pid, Preferences(titles=["Engineer"], min_match=50))
    job = Job(source="lever", source_id="2", company="Acme", title="Engineer")
    jid = db.upsert_job(conn, job)
    db.set_match(
        conn,
        MatchResult(
            job_id=jid,
            profile_id=pid,
            score=77.5,
            coverage=0.8,
            gate_survival=0.97,
            meta={"pursuit": 79.5, "comp": {"verdict": "meets_target"}},
        ),
    )
    rows = db.list_jobs(conn, pid)
    assert rows[0]["score"] == 77.5
    assert rows[0]["meta"]["pursuit"] == 79.5
    assert db.get_match(conn, jid, pid).meta["comp"]["verdict"] == "meets_target"
    assert db.get_preferences(conn, pid).titles == ["Engineer"]


def test_application_upsert_persists_submitted_at(tmp_path: Path):
    path = tmp_path / "test.sqlite3"
    db.init_db(path)
    conn = db.connect(path)
    pid = db.save_profile(conn, Profile(name="tester"))
    jid = db.upsert_job(
        conn, Job(source="lever", source_id="4", company="Acme", title="Engineer")
    )
    app_id = db.create_application(
        conn, Application(job_id=jid, profile_id=pid, status="prepared")
    )
    assert db.get_application(conn, app_id).submitted_at is None

    db.create_application(
        conn,
        Application(
            job_id=jid,
            profile_id=pid,
            status="submitted",
            submitted_at="2026-10-29T12:00:00+00:00",
        ),
    )
    stored = db.get_application(conn, app_id)
    assert stored.status == "submitted"
    assert stored.submitted_at == "2026-10-29T12:00:00+00:00"

    # A later non-submitted upsert must not erase submission history.
    db.create_application(
        conn, Application(job_id=jid, profile_id=pid, status="prepared")
    )
    assert db.get_application(conn, app_id).submitted_at == "2026-10-29T12:00:00+00:00"


def test_application_events_and_export(tmp_path: Path):
    path = tmp_path / "test.sqlite3"
    db.init_db(path)
    conn = db.connect(path)
    profile = Profile(name="tester")
    pid = db.save_profile(conn, profile)
    job = Job(source="lever", source_id="3", company="Acme", title="Engineer")
    jid = db.upsert_job(conn, job)
    app_id = db.create_application(conn, Application(job_id=jid, profile_id=pid, status="prepared"))
    db.update_application(conn, app_id, status="submitted")
    db.log_event(conn, app_id, "submitted", "test")
    assert db.get_application(conn, app_id).status == "submitted"
    assert db.list_events(conn, app_id)[0]["kind"] == "submitted"
    exported = db.export_all(conn)
    assert len(exported["jobs"]) == 1
    assert len(exported["applications"]) == 1
