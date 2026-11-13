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


def test_init_db_migrates_v7_email_tracker_columns(tmp_path: Path):
    import sqlite3

    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            profile_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            ats TEXT NOT NULL DEFAULT '',
            resume_path TEXT NOT NULL DEFAULT '',
            cover_letter_path TEXT NOT NULL DEFAULT '',
            answers_json TEXT NOT NULL DEFAULT '{}',
            screenshot_path TEXT NOT NULL DEFAULT '',
            error_code TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            submitted_at TEXT,
            updated_at TEXT NOT NULL,
            UNIQUE(job_id, profile_id)
        );
        CREATE TABLE emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id INTEGER,
            message_id TEXT NOT NULL DEFAULT '' UNIQUE,
            sender TEXT NOT NULL DEFAULT '',
            subject TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL DEFAULT '',
            received_at TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT 'other',
            verification_code TEXT NOT NULL DEFAULT '',
            forwarded INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.close()

    db.init_db(path)
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
    app_columns = {row["name"] for row in conn.execute("PRAGMA table_info(applications)")}
    email_columns = {row["name"] for row in conn.execute("PRAGMA table_info(emails)")}
    assert "status_source" in app_columns
    assert {"match_score", "match_reason", "match_source", "status_before", "status_after", "processed_at"} <= email_columns
    conn.close()

def test_email_relink_resets_processed_state(tmp_path: Path):
    path = tmp_path / "test.sqlite3"
    db.init_db(path)
    conn = db.connect(path)
    pid = db.save_profile(conn, Profile(name="tester"))
    job_a = db.upsert_job(conn, Job(source="lever", source_id="a", company="Acme", title="Engineer A"))
    job_b = db.upsert_job(conn, Job(source="lever", source_id="b", company="Beta", title="Engineer B"))
    app_a = db.create_application(conn, Application(job_id=job_a, profile_id=pid, status="submitted"))
    app_b = db.create_application(conn, Application(job_id=job_b, profile_id=pid, status="submitted"))
    from jobforge.models import EmailMessage

    msg = EmailMessage(
        message_id="<relink@example.com>",
        sender="Recruiter <r@example.com>",
        subject="Update",
        body="Update",
        received_at="2026-11-12T12:00:00+00:00",
    )
    email_id = db.save_email(conn, msg)
    db.link_email(conn, email_id, app_a, source="auto", score=80)
    db.mark_email_processed(conn, email_id, status_before="submitted", status_after="interview")
    assert db.get_email(conn, email_id)["processed_at"]

    msg.application_id = app_b
    msg.match_score = 90
    msg.match_source = "auto"
    db.save_email(conn, msg, replace_match=True)
    row = db.get_email(conn, email_id)
    assert row["application_id"] == app_b
    assert row["processed_at"] == ""
    assert row["status_before"] == ""
    assert row["status_after"] == ""
