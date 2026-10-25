from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from jobforge import db, risk
from jobforge.cli import build_parser, cmd_apply, cmd_queue, cmd_risk
from jobforge.models import Job, MatchResult, Preferences, Profile
from jobforge.queue import Queue


def _seed(tmp_path: Path) -> Path:
    cfg_path = tmp_path / "config.yml"
    data = tmp_path / "data"
    out = tmp_path / "output"
    cfg_path.write_text(
        f"""
data_dir: {data}
output_dir: {out}
sources: {{}}
preferences: {{titles: [Software Engineer], min_match: 0}}
llm: {{enabled: false}}
email: {{enabled: false}}
browser: {{headless: true, allow_submit: false}}
server: {{host: 127.0.0.1, port: 8765}}
""",
        encoding="utf-8",
    )
    db.init_db(data / "jobforge.sqlite3")
    conn = db.connect(data / "jobforge.sqlite3")
    profile_id = db.save_profile(conn, Profile(name="cli-test"))
    db.set_active_profile(conn, profile_id)
    db.upsert_job(
        conn,
        Job(
            source="lever",
            source_id="cli-1",
            company="Acme",
            title="Software Engineer",
            apply_url="https://jobs.example/apply",
            description="Requirements:\\n- Python",
        ),
    )
    conn.close()
    return cfg_path


def test_risk_and_queue_parsers_are_registered():
    parser = build_parser()
    assert parser.parse_args(["risk", "status"]).func.__name__ == "cmd_risk"
    assert parser.parse_args(["risk", "enable", "--flag", "unattended_submit", "--acknowledge", risk.CONSENT_PHRASE]).func.__name__ == "cmd_risk"
    assert parser.parse_args(["queue", "build", "--limit", "5"]).func.__name__ == "cmd_queue"
    assert parser.parse_args(["queue", "next"]).func.__name__ == "cmd_queue"
    assert parser.parse_args(["queue", "apply", "--hold", "600", "--confirm"]).confirm is True
    assert parser.parse_args(["queue", "apply", "--auto"]).auto is True


def test_risk_status_and_enable_require_phrase(tmp_path: Path):
    cfg_path = _seed(tmp_path)
    status_args = SimpleNamespace(config=str(cfg_path), risk_action="status")
    assert cmd_risk(status_args) == 0

    wrong = SimpleNamespace(
        config=str(cfg_path),
        risk_action="enable",
        flag="unattended_submit",
        acknowledge="yes",
    )
    assert cmd_risk(wrong) == 1

    good = SimpleNamespace(
        config=str(cfg_path),
        risk_action="enable",
        flag="unattended_submit",
        acknowledge=risk.CONSENT_PHRASE,
    )
    assert cmd_risk(good) == 0

    from jobforge.config import load_config

    assert risk.load_policy(load_config(cfg_path)).unattended_submit is True


def test_cmd_apply_quality_preflight_blocks_before_service(tmp_path: Path, monkeypatch):
    cfg_path = _seed(tmp_path)
    from jobforge.config import load_config

    cfg = load_config(cfg_path)
    conn = db.connect(cfg.db_path)
    profile = db.get_active_profile(conn)
    job_id = conn.execute("SELECT id FROM jobs LIMIT 1").fetchone()[0]
    db.set_match(
        conn,
        MatchResult(
            job_id=job_id,
            profile_id=profile.id,
            score=5,
            coverage=0.05,
            gate_survival=1.0,
        ),
    )
    db.save_preferences(conn, profile.id, Preferences(titles=["Software Engineer"], min_match=55))
    conn.close()
    called = []

    monkeypatch.setattr("jobforge.services.apply_job", lambda *args, **kwargs: called.append(True))
    args = SimpleNamespace(
        config=str(cfg_path), job=job_id, allow_submit=False, hold=0, resume=None
    )

    assert cmd_apply(args) == 1
    assert called == []


def test_queue_build_summary_and_auto_risk_gate(tmp_path: Path):
    cfg_path = _seed(tmp_path)
    build_args = SimpleNamespace(
        config=str(cfg_path),
        queue_action="build",
        limit=5,
        min_score=0,
    )
    assert cmd_queue(build_args) == 0

    summary_args = SimpleNamespace(config=str(cfg_path), queue_action="summary")
    assert cmd_queue(summary_args) == 0

    # Mark the built item prepared, then prove --auto still refuses without consent.
    from jobforge.config import load_config

    cfg = load_config(cfg_path)
    queue = Queue(cfg.data_dir / "queue.json")
    item = queue.list_items("pending")[0]
    queue.mark_prepared(item.job_id, str(tmp_path / "resume.pdf"))

    apply_args = SimpleNamespace(
        config=str(cfg_path),
        queue_action="apply",
        hold=0,
        auto=True,
    )
    assert cmd_queue(apply_args) == 1
