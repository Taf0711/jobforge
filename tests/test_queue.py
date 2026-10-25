from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from jobforge import db, quality, services
from jobforge.config import BrowserConfig, Config, EmailConfig, LLMConfig, ServerConfig
from jobforge.models import Application, Job, MatchResult, Preferences, Profile
from jobforge.queue import Queue


def _config(tmp_path: Path) -> Config:
    config = Config(
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
    config.ensure_dirs()
    return config


def _seed(tmp_path: Path):
    config = _config(tmp_path)
    db.init_db(config.db_path)
    conn = db.connect(config.db_path)
    profile_id = db.save_profile(conn, Profile(name="tester"))
    db.set_active_profile(conn, profile_id)
    db.save_preferences(conn, profile_id, Preferences(one_application_per_company=True))
    profile = db.get_active_profile(conn)
    prefs = db.get_preferences(conn, profile_id)
    assert profile is not None
    return config, conn, profile, prefs


def _add_job(
    conn,
    profile_id: int,
    company: str,
    title: str,
    score: float,
) -> int:
    job = Job(
        source="lever",
        source_id=f"{company}-{title}",
        company=company,
        title=title,
        location="Remote",
        apply_url=f"https://jobs.example/{company}/{title}".replace(" ", "-"),
    )
    job_id = db.upsert_job(conn, job)
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
    return job_id


def test_build_ranks_jobs_one_per_company_above_min_score(tmp_path: Path):
    config, conn, profile, prefs = _seed(tmp_path)
    acme_top = _add_job(conn, profile.id or 0, "Acme", "Senior Engineer", 90)
    beta = _add_job(conn, profile.id or 0, "Beta", "Backend Engineer", 85)
    acme_second = _add_job(conn, profile.id or 0, "Acme", "Platform Engineer", 80)
    delta = _add_job(conn, profile.id or 0, "Delta", "Engineer", 70)
    _add_job(conn, profile.id or 0, "Gamma", "Junior Engineer", 40)
    db.create_application(
        conn,
        Application(job_id=beta, profile_id=profile.id, status="prepared"),
    )

    queue = Queue(tmp_path / "state" / "queue.json")
    added = queue.build(conn, profile, prefs, limit=10, min_score=50)

    assert [item.job_id for item in added] == [acme_top, delta]
    assert [item.status for item in added] == ["pending", "pending"]
    assert [item.score for item in added] == [90.0, 70.0]
    assert acme_second not in [item.job_id for item in added]


def test_build_excludes_jobs_with_an_application_without_company_filter(tmp_path: Path):
    _, conn, profile, prefs = _seed(tmp_path)
    applied = _add_job(conn, profile.id or 0, "Acme", "Engineer", 90)
    available = _add_job(conn, profile.id or 0, "Acme", "Engineer II", 80)
    db.create_application(
        conn,
        Application(job_id=applied, profile_id=profile.id, status="withdrawn"),
    )
    prefs.one_application_per_company = False

    queue = Queue(tmp_path / "queue.json")
    added = queue.build(conn, profile, prefs, limit=10)

    assert [item.job_id for item in added] == [available]


def test_build_is_idempotent_and_persists_atomically(tmp_path: Path):
    _, conn, profile, prefs = _seed(tmp_path)
    first = _add_job(conn, profile.id or 0, "Acme", "Engineer", 90)
    second = _add_job(conn, profile.id or 0, "Beta", "Engineer", 80)
    path = tmp_path / "state" / "queue.json"
    queue = Queue(path)

    added = queue.build(conn, profile, prefs, limit=10)
    assert [item.job_id for item in added] == [first, second]
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(path.parent.glob(".queue-*")) == []

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert [item["job_id"] for item in payload["items"]] == [first, second]
    assert {item["status"] for item in payload["items"]} == {"pending"}

    loaded = Queue(path)
    assert [item.job_id for item in loaded.list_items("pending")] == [first, second]
    assert loaded.build(conn, profile, prefs, limit=10) == []
    assert loaded.summary() == {
        "total": 2,
        "pending": 2,
        "prepared": 0,
        "submitted": 0,
        "skipped": 0,
        "failed": 0,
    }


def test_prepare_next_marks_prepared_and_persists(tmp_path: Path, monkeypatch):
    config, conn, profile, prefs = _seed(tmp_path)
    job_id = _add_job(conn, profile.id or 0, "Acme", "Engineer", 90)
    resume = tmp_path / "output" / "acme.pdf"
    resume.parent.mkdir(parents=True, exist_ok=True)
    resume.write_bytes(b"%PDF-1.4")
    queue = Queue(tmp_path / "queue.json")
    queue.build(conn, profile, prefs, limit=10)
    calls = []

    def fake_tailor(cfg, connection, requested_job_id):
        calls.append((cfg, connection, requested_job_id))
        return {"pdf": str(resume)}

    monkeypatch.setattr(services, "tailor_job", fake_tailor)

    item = queue.prepare_next(config, conn)

    assert calls == [(config, conn, job_id)]
    assert item.job_id == job_id
    assert item.status == "prepared"
    assert item.resume_path == str(resume)
    assert item.error == ""
    assert queue.next_item() is None
    reloaded = Queue(tmp_path / "queue.json").list_items("prepared")
    assert len(reloaded) == 1
    assert reloaded[0].resume_path == str(resume)


def test_prepare_next_records_failure(tmp_path: Path, monkeypatch):
    config, conn, profile, prefs = _seed(tmp_path)
    job_id = _add_job(conn, profile.id or 0, "Acme", "Engineer", 90)
    queue = Queue(tmp_path / "queue.json")
    queue.build(conn, profile, prefs, limit=10)

    def fake_tailor(*args, **kwargs):
        raise RuntimeError("no base resume")

    monkeypatch.setattr(services, "tailor_job", fake_tailor)

    item = queue.prepare_next(config, conn)

    assert item.job_id == job_id
    assert item.status == "failed"
    assert item.error == "no base resume"
    assert Queue(tmp_path / "queue.json").list_items("failed")[0].error == "no base resume"


def test_apply_next_marks_submitted_when_service_reports_it(tmp_path: Path, monkeypatch):
    config, conn, profile, prefs = _seed(tmp_path)
    job_id = _add_job(conn, profile.id or 0, "Acme", "Engineer", 90)
    queue = Queue(tmp_path / "queue.json")
    queue.build(conn, profile, prefs, limit=10)
    queue.mark_prepared(job_id, str(tmp_path / "resume.pdf"))
    calls = []

    def fake_apply(cfg, connection, requested_job_id, *, hold_seconds=0):
        calls.append((cfg, connection, requested_job_id, hold_seconds))
        return {"submitted": True, "notes": ["filled"]}

    monkeypatch.setattr(services, "apply_job", fake_apply)

    item = queue.apply_next(config, conn, hold_seconds=5)

    assert calls == [(config, conn, job_id, 5)]
    assert item.status == "submitted"
    assert item.note == "submission reported by apply_job"
    assert Queue(tmp_path / "queue.json").list_items("submitted")[0].job_id == job_id


def test_apply_next_forces_review_mode_even_if_config_allows_submit(tmp_path: Path, monkeypatch):
    config, conn, profile, prefs = _seed(tmp_path)
    config.browser.allow_submit = True
    job_id = _add_job(conn, profile.id or 0, "Acme", "Engineer", 90)
    queue = Queue(tmp_path / "queue.json")
    queue.build(conn, profile, prefs, limit=10)
    queue.mark_prepared(job_id, str(tmp_path / "resume.pdf"))
    seen = {}

    def fake_apply(cfg, connection, requested_job_id, *, hold_seconds=0):
        seen["allow_submit"] = cfg.browser.allow_submit
        return {"submitted": False}

    monkeypatch.setattr(services, "apply_job", fake_apply)

    queue.apply_next(config, conn)

    assert seen["allow_submit"] is False
    assert config.browser.allow_submit is True


def test_apply_next_confirm_false_keeps_item_prepared(tmp_path: Path, monkeypatch):
    config, conn, profile, prefs = _seed(tmp_path)
    job_id = _add_job(conn, profile.id or 0, "Acme", "Engineer", 90)
    queue = Queue(tmp_path / "queue.json")
    queue.build(conn, profile, prefs, limit=10)
    queue.mark_prepared(job_id, str(tmp_path / "resume.pdf"))
    asked = []

    monkeypatch.setattr(services, "apply_job", lambda *args, **kwargs: {"submitted": False})

    def confirm():
        asked.append(True)
        return False

    item = queue.apply_next(config, conn, confirm=confirm)

    assert asked == [True]
    assert item.status == "prepared"
    assert item.note == "human reported no submission"
    assert Queue(tmp_path / "queue.json").list_items("prepared")[0].note == item.note


def test_apply_next_confirm_true_marks_submitted(tmp_path: Path, monkeypatch):
    config, conn, profile, prefs = _seed(tmp_path)
    job_id = _add_job(conn, profile.id or 0, "Acme", "Engineer", 90)
    queue = Queue(tmp_path / "queue.json")
    queue.build(conn, profile, prefs, limit=10)
    queue.mark_prepared(job_id, str(tmp_path / "resume.pdf"))

    monkeypatch.setattr(services, "apply_job", lambda *args, **kwargs: {"submitted": False})

    item = queue.apply_next(config, conn, confirm=lambda: True)

    assert item.status == "submitted"
    assert item.note == "human confirmed submission"
    assert item.error == ""


def test_apply_next_rejects_malformed_submitted_value(tmp_path: Path, monkeypatch):
    config, conn, profile, prefs = _seed(tmp_path)
    job_id = _add_job(conn, profile.id or 0, "Acme", "Engineer", 90)
    queue = Queue(tmp_path / "queue.json")
    queue.build(conn, profile, prefs, limit=10)
    queue.mark_prepared(job_id, str(tmp_path / "resume.pdf"))

    monkeypatch.setattr(services, "apply_job", lambda *args, **kwargs: {"submitted": "false"})

    item = queue.apply_next(config, conn)

    assert item.status == "failed"
    assert "malformed" in item.error


def test_apply_next_blocks_on_quality_before_browser(tmp_path: Path, monkeypatch):
    config, conn, profile, prefs = _seed(tmp_path)
    db.save_preferences(conn, profile.id or 0, Preferences(one_application_per_company=True, min_match=60))
    prefs = db.get_preferences(conn, profile.id or 0)
    job_id = _add_job(conn, profile.id or 0, "Acme", "Engineer", 10)
    queue = Queue(tmp_path / "queue.json")
    queue.build(conn, profile, prefs, limit=10, min_score=0)
    queue.mark_prepared(job_id, str(tmp_path / "resume.pdf"))
    called = []

    monkeypatch.setattr(services, "apply_job", lambda *args, **kwargs: called.append(True))

    with pytest.raises(quality.QualityBlocked) as excinfo:
        queue.apply_next(config, conn)

    assert called == []
    assert any(check.name == "match_score" for check in excinfo.value.blocking)
    assert Queue(tmp_path / "queue.json").list_items("prepared")[0].job_id == job_id


def test_prepare_next_records_quality_note_without_failing(tmp_path: Path, monkeypatch):
    config, conn, profile, prefs = _seed(tmp_path)
    job_id = _add_job(conn, profile.id or 0, "Acme", "Engineer", 90)
    queue = Queue(tmp_path / "queue.json")
    queue.build(conn, profile, prefs, limit=10)
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(services, "tailor_job", lambda *args, **kwargs: {"pdf": str(resume)})

    item = queue.prepare_next(config, conn)

    assert item.status == "prepared"
    assert "quality blockers" in item.note
    assert "application_prepared" in item.note


def test_next_actionable_prefers_prepared(tmp_path: Path):
    config, conn, profile, prefs = _seed(tmp_path)
    first = _add_job(conn, profile.id or 0, "Acme", "Engineer", 90)
    _add_job(conn, profile.id or 0, "Beta", "Engineer", 80)
    queue = Queue(tmp_path / "queue.json")
    queue.build(conn, profile, prefs, limit=10)

    assert queue.next_actionable().status == "pending"
    queue.mark_prepared(first, str(tmp_path / "resume.pdf"))
    assert queue.next_actionable().job_id == first
    assert queue.next_actionable().status == "prepared"


def test_apply_next_hold_without_confirm_keeps_prepared_and_notes_it(tmp_path: Path, monkeypatch):
    config, conn, profile, prefs = _seed(tmp_path)
    job_id = _add_job(conn, profile.id or 0, "Acme", "Engineer", 90)
    queue = Queue(tmp_path / "queue.json")
    queue.build(conn, profile, prefs, limit=10)
    queue.mark_prepared(job_id, str(tmp_path / "resume.pdf"))
    seen = {}

    def fake_apply(cfg, connection, requested_job_id, *, hold_seconds=0):
        seen["hold_seconds"] = hold_seconds
        return {"submitted": False}

    monkeypatch.setattr(services, "apply_job", fake_apply)

    item = queue.apply_next(config, conn, hold_seconds=600)

    assert seen["hold_seconds"] == 600
    assert item.status == "prepared"
    assert "600" in item.note
    assert "not inferred" in item.note
    assert Queue(tmp_path / "queue.json").list_items("prepared")[0].note == item.note


def test_apply_next_exception_marks_failed_without_raising(tmp_path: Path, monkeypatch):
    config, conn, profile, prefs = _seed(tmp_path)
    job_id = _add_job(conn, profile.id or 0, "Acme", "Engineer", 90)
    queue = Queue(tmp_path / "queue.json")
    queue.build(conn, profile, prefs, limit=10)
    queue.mark_prepared(job_id, str(tmp_path / "resume.pdf"))

    def fake_apply(*args, **kwargs):
        raise RuntimeError("browser crashed")

    monkeypatch.setattr(services, "apply_job", fake_apply)

    item = queue.apply_next(config, conn)

    assert item.status == "failed"
    assert item.error == "browser crashed"
    assert Queue(tmp_path / "queue.json").list_items("failed")[0].job_id == job_id


def test_skip_and_fail_transitions_persist(tmp_path: Path):
    _, conn, profile, prefs = _seed(tmp_path)
    first = _add_job(conn, profile.id or 0, "Acme", "Engineer", 90)
    second = _add_job(conn, profile.id or 0, "Beta", "Engineer", 80)
    queue = Queue(tmp_path / "queue.json")
    queue.build(conn, profile, prefs, limit=10)

    assert queue.next_item().job_id == first
    skipped = queue.mark_skipped(first)
    failed = queue.mark_failed(second, "no apply URL")

    assert skipped.status == "skipped"
    assert skipped.error == ""
    assert failed.status == "failed"
    assert failed.error == "no apply URL"
    assert queue.next_item() is None
    assert queue.summary()["skipped"] == 1
    assert queue.summary()["failed"] == 1

    reloaded = Queue(tmp_path / "queue.json").summary()
    assert reloaded == {
        "total": 2,
        "pending": 0,
        "prepared": 0,
        "submitted": 0,
        "skipped": 1,
        "failed": 1,
    }


def test_empty_queue_actions_raise_lookup_error(tmp_path: Path):
    config, conn, _, _ = _seed(tmp_path)
    queue = Queue(tmp_path / "queue.json")

    with pytest.raises(LookupError):
        queue.prepare_next(config, conn)
    with pytest.raises(LookupError):
        queue.apply_next(config, conn)


def test_unknown_status_is_rejected(tmp_path: Path):
    queue = Queue(tmp_path / "queue.json")
    with pytest.raises(ValueError):
        queue.list_items("bogus")
