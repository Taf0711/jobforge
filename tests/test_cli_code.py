from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from jobforge import db
from jobforge.cli import _code_payload, cmd_code
from jobforge.email.code_bridge import CodeResult, record_bridge_event
from jobforge.models import Application, Job, Profile


def _seed(tmp_path: Path) -> tuple[Path, int]:
    cfg_path = tmp_path / "config.yml"
    data = tmp_path / "data"
    out = tmp_path / "output"
    cfg_path.write_text(
        f"""
data_dir: {data}
output_dir: {out}
sources: {{}}
preferences: {{}}
llm: {{enabled: false}}
email: {{enabled: false}}
browser: {{headless: true, allow_submit: false}}
server: {{host: 127.0.0.1, port: 8765}}
""",
        encoding="utf-8",
    )
    db.init_db(data / "jobforge.sqlite3")
    conn = db.connect(data / "jobforge.sqlite3")
    profile_id = db.save_profile(conn, Profile(name="test"))
    job_id = db.upsert_job(conn, Job(source="test", source_id="1", company="Acme", title="Engineer"))
    app_id = db.create_application(conn, Application(job_id=job_id, profile_id=profile_id))
    conn.close()
    return cfg_path, app_id


def test_payload_hides_code_by_default():
    result = CodeResult(
        matched=True,
        code="123456",
        message_id="msg-1",
        sender="no-reply@example.com",
        subject="Your code is 123456",
        received_at="2026-10-24T00:00:00+00:00",
        elapsed_s=1.25,
        reason="matched",
    )
    default = _code_payload(result)
    assert "code" not in default
    assert default["redacted"].endswith("56")
    assert "123456" not in default["subject"]
    assert _code_payload(result, reveal=True)["code"] == "123456"


def test_payload_redacts_separator_formatted_code():
    result = CodeResult(
        matched=True,
        code="123456",
        subject="Acme: your code is 123 456",
        sender="no-reply@acme.test",
        message_id="msg-1",
        received_at="2026-10-24T00:00:00+00:00",
        elapsed_s=0.5,
        reason="matched",
    )
    default = _code_payload(result)
    assert "123 456" not in default["subject"]
    assert "123456" not in default["subject"]


def test_event_detail_redacts_separator_code(tmp_path: Path):
    _, app_id = _seed(tmp_path)
    conn = db.connect(tmp_path / "data" / "jobforge.sqlite3")
    result = CodeResult(
        matched=True,
        code="123456",
        subject="Acme: your code is 123 456",
        sender="no-reply@acme.test",
        message_id="m 123-456",
        received_at="2026-10-24T00:00:00+00:00",
        elapsed_s=0.5,
        reason="matched",
    )
    record_bridge_event(conn, app_id, result)
    detail = db.list_events(conn, app_id)[0]["detail"]
    conn.close()

    assert "123 456" not in detail
    assert "123-456" not in detail
    assert "123456" not in detail
    assert "\u2022\u2022\u2022\u202256" in detail


def test_reveal_flag_is_parsed():
    from jobforge.cli import build_parser

    args = build_parser().parse_args(["code", "--company", "Acme", "--reveal-code"])
    assert args.reveal_code is True


def test_timeout_records_an_event(tmp_path: Path, monkeypatch, capsys):
    cfg_path, app_id = _seed(tmp_path)
    import jobforge.email.code_bridge as bridge

    monkeypatch.setattr(
        bridge,
        "wait_for_code",
        lambda conn, cfg, request: CodeResult(matched=False, reason="timeout", elapsed_s=0.1),
    )
    args = SimpleNamespace(
        config=str(cfg_path),
        company="Acme",
        sender=None,
        subject=None,
        since="",
        timeout=1,
        application_id=app_id,
        reveal_code=False,
    )

    assert cmd_code(args) == 2
    conn = db.connect(tmp_path / "data" / "jobforge.sqlite3")
    events = db.list_events(conn, app_id)
    conn.close()
    assert any(event["kind"] == "verification_code" and "no match" in event["detail"] for event in events)
    payload = json.loads(capsys.readouterr().out)
    assert payload["matched"] is False
    assert "code" not in payload


def test_hint_is_required(tmp_path: Path):
    args = SimpleNamespace(
        config=str(tmp_path / "missing.yml"),
        company="",
        sender=None,
        subject=None,
        since="",
        timeout=1,
        application_id=None,
        reveal_code=False,
    )
    assert cmd_code(args) == 2


def test_invalid_application_id_is_rejected(tmp_path: Path, monkeypatch):
    cfg_path, _ = _seed(tmp_path)
    import jobforge.email.code_bridge as bridge

    monkeypatch.setattr(
        bridge,
        "wait_for_code",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not wait")),
    )
    args = SimpleNamespace(
        config=str(cfg_path),
        company="Acme",
        sender=None,
        subject=None,
        since="",
        timeout=1,
        application_id=99999,
        reveal_code=False,
    )
    assert cmd_code(args) == 1


def test_application_id_zero_is_rejected(tmp_path: Path, monkeypatch):
    cfg_path, _ = _seed(tmp_path)
    import jobforge.email.code_bridge as bridge

    monkeypatch.setattr(
        bridge,
        "wait_for_code",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not wait")),
    )
    args = SimpleNamespace(
        config=str(cfg_path),
        company="Acme",
        sender=None,
        subject=None,
        since="",
        timeout=1,
        application_id=0,
        reveal_code=False,
    )
    assert cmd_code(args) == 1
