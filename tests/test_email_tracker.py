from __future__ import annotations

from pathlib import Path

from jobforge import db
from jobforge.config import Config, BrowserConfig, EmailConfig, LLMConfig, ServerConfig
from jobforge.email.imap import classify, poll
from jobforge.email.tracker import (
    allowed_transition,
    match_email,
    process_email,
    process_messages,
)
from jobforge.models import Application, EmailMessage, Job, Profile


def _config(tmp_path: Path) -> Config:
    return Config(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        db_path=tmp_path / "data" / "test.sqlite3",
        profile_path=None,
        resume_path=None,
        sources={},
        preferences={},
        llm=LLMConfig(),
        email=EmailConfig(
            enabled=True,
            username="me@example.com",
            secret_dir=str(tmp_path / "data" / "connections" / "email"),
        ),
        browser=BrowserConfig(),
        server=ServerConfig(),
    )


def _message(
    *,
    message_id: str,
    subject: str,
    body: str,
    sender: str = "Acme Recruiting <no-reply@us.greenhouse-mail.io>",
    received_at: str = "2026-11-12T12:00:00+00:00",
) -> EmailMessage:
    return EmailMessage(
        message_id=message_id,
        sender=sender,
        subject=subject,
        body=body,
        received_at=received_at,
        category=classify(subject, body),
    )


def _seed(tmp_path: Path) -> tuple[Config, object, int, int]:
    cfg = _config(tmp_path)
    cfg.ensure_dirs()
    db.init_db(cfg.db_path)
    conn = db.connect(cfg.db_path)
    profile_id = db.save_profile(conn, Profile(name="tracker-test"))
    job_id = db.upsert_job(
        conn,
        Job(
            source="greenhouse",
            source_id="acme-1",
            company="Acme",
            title="Software Engineer",
            apply_url="https://boards.greenhouse.io/acme/jobs/123",
            url="https://boards.greenhouse.io/acme/jobs/123",
        ),
    )
    app_id = db.create_application(
        conn,
        Application(
            job_id=job_id,
            profile_id=profile_id,
            status="draft",
            ats="greenhouse",
        ),
    )
    return cfg, conn, profile_id, app_id


def test_classification_prefers_rejection_over_negative_offer_and_ignores_quotes():
    assert classify("Offer update", "Unfortunately, we are unable to offer you the position.") == "rejection"
    assert classify("Job offer", "We are pleased to offer you the role.") == "offer"
    assert classify("Next step", "Please complete this online assessment by Friday.") == "assessment"
    assert (
        classify(
            "Your application",
            "We received your application for Software Engineer.\n"
            "On Mon, Nov 10, 2026 at 9:00 AM Recruiter <r@acme.com> wrote:\n"
            "Unfortunately we are not moving forward.",
        )
        == "confirmation"
    )


def test_match_uses_role_to_disambiguate_same_company(tmp_path: Path):
    cfg, conn, profile_id, app_id = _seed(tmp_path)
    second_job = db.upsert_job(
        conn,
        Job(
            source="greenhouse",
            source_id="acme-2",
            company="Acme",
            title="Data Engineer",
            apply_url="https://boards.greenhouse.io/acme/jobs/456",
        ),
    )
    second_app = db.create_application(
        conn,
        Application(job_id=second_job, profile_id=profile_id, status="draft", ats="greenhouse"),
    )

    match = match_email(
        conn,
        "Acme Recruiting <no-reply@us.greenhouse-mail.io>",
        "Interview for Software Engineer",
        "We would like to schedule a call about the Software Engineer role at Acme.",
    )
    assert match.application_id == app_id
    assert match.score >= 60

    ambiguous = match_email(
        conn,
        "Acme Recruiting <no-reply@us.greenhouse-mail.io>",
        "Update from Acme",
        "Your application at Acme is moving to the next step.",
    )
    assert ambiguous.application_id is None
    assert ambiguous.ambiguous is True
    assert second_app != app_id


def test_status_changes_are_forward_only_and_deduplicated(tmp_path: Path):
    cfg, conn, _profile_id, app_id = _seed(tmp_path)
    confirmation = _message(
        message_id="<confirm@acme>",
        subject="Application received",
        body="Thank you for applying to the Software Engineer role at Acme. "
        "We received your application.",
    )
    first = process_messages(conn, cfg.email, [confirmation])
    assert first["status_changes"] == 1
    application = db.get_application(conn, app_id)
    assert application.status == "submitted"
    assert application.status_source == "email"
    assert application.submitted_at == confirmation.received_at

    second = process_messages(conn, cfg.email, [confirmation])
    assert second["status_changes"] == 0
    assert len(db.list_events(conn, app_id)) == 1

    screening = _message(
        message_id="<assessment@acme>",
        subject="Next step: online assessment",
        body="Please complete the coding challenge for the Software Engineer role at Acme.",
        received_at="2026-11-13T12:00:00+00:00",
    )
    assert process_messages(conn, cfg.email, [screening])["status_changes"] == 1
    assert db.get_application(conn, app_id).status == "screening"

    offer = _message(
        message_id="<offer@acme>",
        subject="Your offer",
        body="We are pleased to offer you the Software Engineer position at Acme.",
        received_at="2026-11-14T12:00:00+00:00",
    )
    assert process_messages(conn, cfg.email, [offer])["status_changes"] == 1
    assert db.get_application(conn, app_id).status == "offer"

    late_rejection = _message(
        message_id="<late-reject@acme>",
        subject="Update",
        body="Unfortunately we are not moving forward with other candidates for the Software Engineer role at Acme.",
        received_at="2026-11-15T12:00:00+00:00",
    )
    assert process_messages(conn, cfg.email, [late_rejection])["status_changes"] == 0
    assert db.get_application(conn, app_id).status == "offer"


def test_low_confidence_link_does_not_advance_status(tmp_path: Path):
    cfg, conn, _profile_id, app_id = _seed(tmp_path)
    message = _message(
        message_id="<weak@acme>",
        subject="Update",
        body="We received your application for the Software Engineer role at Acme.",
        sender="notifications@example.com",
    )
    result = process_messages(conn, cfg.email, [message])
    stored = conn.execute("SELECT * FROM emails WHERE message_id=?", (message.message_id,)).fetchone()
    assert stored["application_id"] == app_id
    assert 45 <= float(stored["match_score"]) < 60
    assert result["status_changes"] == 0
    assert db.get_application(conn, app_id).status == "draft"


def test_manual_link_can_apply_status_and_unlink_can_be_ignored(tmp_path: Path):
    cfg, conn, _profile_id, app_id = _seed(tmp_path)
    message = _message(
        message_id="<manual@acme>",
        subject="Update",
        body="Unfortunately we are not moving forward with other candidates.",
        sender="recruiter@example.com",
    )
    process_messages(conn, cfg.email, [message])
    email_row = conn.execute("SELECT * FROM emails WHERE message_id=?", (message.message_id,)).fetchone()
    assert email_row["application_id"] is None

    db.link_email(
        conn,
        int(email_row["id"]),
        app_id,
        source="manual",
        score=100,
        reason="dashboard link",
    )
    result = process_email(conn, cfg.email, int(email_row["id"]))
    assert result["changed"] is True
    assert db.get_application(conn, app_id).status == "rejected"

    db.link_email(conn, int(email_row["id"]), None, source="manual_ignore")
    db.mark_email_processed(conn, int(email_row["id"]), status_before="", status_after="")
    assert db.get_email(conn, int(email_row["id"]))["match_source"] == "manual_ignore"


def test_poll_uses_fetcher_and_tracks_categories(tmp_path: Path):
    cfg, conn, _profile_id, app_id = _seed(tmp_path)
    messages = [
        _message(
            message_id="<poll@acme>",
            subject="Interview invitation",
            body="We would like to schedule a call for the Software Engineer role at Acme.",
        )
    ]
    result = poll(cfg.email, conn, fetcher=lambda _cfg, _limit: messages)
    assert result["fetched"] == 1
    assert result["matched"] == 1
    assert result["status_changes"] == 1
    assert result["categories"] == {"interview": 1}
    assert db.get_application(conn, app_id).status == "interview"


def test_allowed_transition_is_conservative():
    assert allowed_transition("draft", "submitted") is True
    assert allowed_transition("submitted", "screening") is True
    assert allowed_transition("interview", "offer") is True
    assert allowed_transition("offer", "rejected") is False
    assert allowed_transition("rejected", "offer") is False
    assert allowed_transition("withdrawn", "interview") is False

def test_old_email_links_but_does_not_advance_status(tmp_path: Path):
    cfg, conn, _profile_id, app_id = _seed(tmp_path)
    cfg.email.status_window_days = 30
    message = _message(
        message_id="<old@acme>",
        subject="Application received",
        body="Thank you for applying to the Software Engineer role at Acme. "
        "We received your application.",
        received_at="2020-01-01T12:00:00+00:00",
    )
    result = process_messages(conn, cfg.email, [message])
    stored = conn.execute("SELECT * FROM emails WHERE message_id=?", (message.message_id,)).fetchone()
    assert stored["application_id"] == app_id
    assert result["status_changes"] == 0
    assert db.get_application(conn, app_id).status == "draft"
