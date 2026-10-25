from __future__ import annotations

from pathlib import Path

from jobforge import db
from jobforge.email.code_bridge import (
    CodeRequest,
    CodeResult,
    match_message,
    record_bridge_event,
    redact_code,
    wait_for_code,
)
from jobforge.models import Application, EmailCategory, EmailMessage, Job, Profile

SINCE = "2026-10-24T12:00:00+00:00"
RECEIVED = "2026-10-24T12:00:05+00:00"
CODE = "482913"


def make_message(**overrides) -> EmailMessage:
    values = {
        "message_id": "<msg-1@example.com>",
        "sender": "Greenhouse <no-reply@greenhouse.io>",
        "subject": "Your verification code",
        "body": "Use the code to finish your application.",
        "received_at": RECEIVED,
        "category": EmailCategory.VERIFICATION.value,
        "verification_code": CODE,
    }
    values.update(overrides)
    return EmailMessage(**values)


def make_request(**overrides) -> CodeRequest:
    values = {
        "company": "",
        "sender_hints": [],
        "subject_hints": [],
        "since_iso": SINCE,
        "timeout_s": 10,
        "poll_interval_s": 3.0,
    }
    values.update(overrides)
    return CodeRequest(**values)


def test_match_on_sender_hint():
    request = make_request(sender_hints=["greenhouse.io"])
    assert match_message(make_message(), request) is True


def test_match_on_company():
    request = make_request(company="Acme")
    message = make_message(sender="careers@acme.test", body="Thanks for applying to Acme.")
    assert match_message(message, request) is True


def test_match_on_subject_hint():
    request = make_request(subject_hints=["verify your email"])
    message = make_message(subject="Please verify your email", body="No hint in the body.")
    assert match_message(message, request) is True


def test_reject_non_verification_category():
    request = make_request(company="Acme")
    message = make_message(category=EmailCategory.CONFIRMATION.value, body="Acme received it")
    assert match_message(message, request) is False


def test_reject_old_message():
    request = make_request(sender_hints=["greenhouse.io"])
    message = make_message(received_at="2026-10-24T11:59:59+00:00")
    assert match_message(message, request) is False


def test_timeout_with_fake_clock():
    now = {"t": 0.0}
    fetches = {"n": 0}

    def clock() -> float:
        return now["t"]

    def sleeper(seconds: float) -> None:
        now["t"] += seconds

    def fetcher(_config) -> list[EmailMessage]:
        fetches["n"] += 1
        return []

    result = wait_for_code(
        None,
        object(),
        make_request(timeout_s=10, poll_interval_s=3.0),
        fetcher=fetcher,
        sleeper=sleeper,
        clock=clock,
    )
    assert result.matched is False
    assert result.reason == "timeout"
    assert result.elapsed_s == 10.0
    assert fetches["n"] == 4


def test_wait_returns_first_match_and_elapsed():
    now = {"t": 0.0}
    message = make_message()

    def clock() -> float:
        return now["t"]

    result = wait_for_code(
        None,
        object(),
        make_request(sender_hints=["greenhouse.io"]),
        fetcher=lambda _config: [message],
        sleeper=lambda seconds: now.__setitem__("t", now["t"] + seconds),
        clock=clock,
    )
    assert result.matched is True
    assert result.code == CODE
    assert result.message_id == "<msg-1@example.com>"
    assert result.reason == "matched"
    assert result.elapsed_s == 0.0


def test_duplicate_message_id_is_not_reprocessed():
    now = {"t": 0.0}
    polls = {"n": 0}

    def clock() -> float:
        return now["t"]

    def sleeper(seconds: float) -> None:
        now["t"] += seconds

    def fetcher(_config) -> list[EmailMessage]:
        polls["n"] += 1
        # Same id: harmless first, then it would match on a later poll.
        category = EmailCategory.CONFIRMATION.value if polls["n"] == 1 else EmailCategory.VERIFICATION.value
        return [make_message(category=category, verification_code=CODE if polls["n"] > 1 else "")]

    result = wait_for_code(
        None,
        object(),
        make_request(sender_hints=["greenhouse.io"], timeout_s=5, poll_interval_s=1.0),
        fetcher=fetcher,
        sleeper=sleeper,
        clock=clock,
    )
    assert result.matched is False
    assert result.reason == "timeout"
    assert result.elapsed_s == 5.0


def test_redact_code_never_reveals_full_code():
    redacted = redact_code(CODE)
    assert CODE not in redacted
    assert redacted.endswith("13")
    assert redact_code("12") == "\u2022\u2022"
    assert redact_code("") == ""


def test_record_bridge_event_uses_application_id_and_redacts(tmp_path: Path):
    path = tmp_path / "bridge.sqlite3"
    db.init_db(path)
    conn = db.connect(path)
    profile_id = db.save_profile(conn, Profile(name="tester"))
    job_id = db.upsert_job(conn, Job(source="lever", source_id="bridge-1", company="Acme", title="Engineer"))
    app_id = db.create_application(conn, Application(job_id=job_id, profile_id=profile_id))

    result = CodeResult(
        matched=True,
        code=CODE,
        message_id="<msg-1@example.com>",
        sender="Greenhouse <no-reply@greenhouse.io>",
        subject=f"Your verification code is {CODE}",
        received_at=RECEIVED,
        elapsed_s=1.5,
        reason="matched",
    )
    record_bridge_event(conn, app_id, result)

    events = db.list_events(conn, app_id)
    assert len(events) == 1
    assert events[0]["kind"] == "verification_code"
    assert events[0]["application_id"] == app_id
    assert CODE not in events[0]["detail"]
    assert ("\u2022" * 4 + "13") in events[0]["detail"]
