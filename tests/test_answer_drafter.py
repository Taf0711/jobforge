from __future__ import annotations

from pathlib import Path

import pytest

from jobforge import db
from jobforge.automation.answers import AnswerBank
from jobforge.automation.drafter import (
    DraftError,
    answers_path,
    approve_draft,
    draft_answer,
    drafts_path,
    list_drafts,
    reject_draft,
)
from jobforge.config import BrowserConfig, Config, EmailConfig, LLMConfig, ServerConfig
from jobforge.models import Job, Preferences, Profile


class FakeLLM:
    available = True
    error = ""
    model = "fake-model"

    def __init__(self, payload):
        self.payload = payload

    def json_chat(self, system, user, *, max_tokens=None):
        return self.payload


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


def _seed(cfg: Config) -> tuple[object, Job, Profile, Preferences]:
    conn = db.connect(cfg.db_path)
    profile = Profile(
        name="drafter",
        first_name="Ada",
        last_name="Lovelace",
        email="ada@example.com",
        summary="Backend engineer.",
        skills=["Python"],
        experience=[
            {
                "title": "Engineer",
                "company": "Analytical Engines",
                "bullets": ["Built Python services reducing latency by 40%."],
            }
        ],
    )
    profile.id = db.save_profile(conn, profile)
    db.set_active_profile(conn, profile.id)
    prefs = Preferences()
    db.save_preferences(conn, profile.id, prefs)
    job = Job(
        source="ashby",
        source_id="n1",
        company="N1",
        title="New Grad Backend Engineer",
        location="New York City",
        description=(
            "About N1: N1 is a high-performance blockchain network.\n"
            "Requirements:\n- Python\n- Backend systems\n"
        ),
    )
    job.id = db.upsert_job(conn, job)
    return conn, job, profile, prefs


def _payload(answer: str, ids: list[str] | None = None):
    return {
        "answer": answer,
        "used_fact_ids": ["exp:0:b:0"] if ids is None else ids,
        "gap": "",
    }


def test_draft_is_saved_but_not_in_answer_bank(tmp_path: Path):
    cfg = _config(tmp_path)
    conn, job, _profile, _prefs = _seed(cfg)
    draft = draft_answer(
        cfg,
        conn,
        job.id,
        "Why N1?",
        llm=FakeLLM(
            _payload(
                "I want to work at N1 because I built Python services that reduced latency by 40% "
                "and I want to work on backend systems at internet scale."
            )
        ),
    )

    assert draft.status == "draft"
    assert draft.text.startswith("I want to work at N1")
    assert draft.basis and draft.basis[0]["id"] == "exp:0:b:0"
    assert drafts_path(cfg).exists()
    assert not answers_path(cfg).exists()
    assert list_drafts(cfg)[0].id == draft.id
    conn.close()


def test_approval_writes_answer_bank_and_is_usable(tmp_path: Path):
    cfg = _config(tmp_path)
    conn, job, profile, prefs = _seed(cfg)
    draft = draft_answer(
        cfg,
        conn,
        job.id,
        "Why N1?",
        llm=FakeLLM(
            _payload(
                "I want to work at N1 because I built Python services that reduced latency by 40% "
                "and I want to work on backend systems at internet scale."
            )
        ),
    )
    approved = approve_draft(cfg, draft.id)

    assert approved.status == "approved"
    assert approved.approved_key == "why n1"
    bank = AnswerBank(answers_path(cfg))
    answer = bank.answer("Why N1?", profile, prefs)
    assert answer.value == approved.text
    assert answer.category == "override"
    conn.close()


def test_rejection_does_not_write_answer_bank(tmp_path: Path):
    cfg = _config(tmp_path)
    conn, job, _profile, _prefs = _seed(cfg)
    draft = draft_answer(cfg, conn, job.id, "Why N1?", llm=FakeLLM(_payload("I build Python services.")))
    rejected = reject_draft(cfg, draft.id)

    assert rejected.status == "rejected"
    assert not answers_path(cfg).exists()
    conn.close()


def test_sensitive_question_is_refused(tmp_path: Path):
    cfg = _config(tmp_path)
    conn, job, _profile, _prefs = _seed(cfg)
    with pytest.raises(DraftError, match="Sensitive"):
        draft_answer(cfg, conn, job.id, "Are you a veteran?", llm=FakeLLM(_payload("No.")))
    conn.close()


def test_uncited_answer_is_refused(tmp_path: Path):
    cfg = _config(tmp_path)
    conn, job, _profile, _prefs = _seed(cfg)
    with pytest.raises(DraftError, match="cite"):
        draft_answer(cfg, conn, job.id, "Why N1?", llm=FakeLLM(_payload("I build Python services.", [])))
    conn.close()


def test_unsupported_number_is_refused(tmp_path: Path):
    cfg = _config(tmp_path)
    conn, job, _profile, _prefs = _seed(cfg)
    with pytest.raises(DraftError, match="number"):
        draft_answer(
            cfg,
            conn,
            job.id,
            "Why N1?",
            llm=FakeLLM(_payload("I built Python services and improved latency by 70%.")),
        )
    conn.close()


def test_unsupported_skill_is_refused(tmp_path: Path):
    cfg = _config(tmp_path)
    conn, job, _profile, _prefs = _seed(cfg)
    with pytest.raises(DraftError, match="skill"):
        draft_answer(
            cfg,
            conn,
            job.id,
            "Why N1?",
            llm=FakeLLM(_payload("I have Kubernetes experience and build Python services.")),
        )
    conn.close()


def test_read_profile_does_not_create_rows(tmp_path: Path):
    from jobforge import services

    cfg = _config(tmp_path)
    conn = db.connect(cfg.db_path)
    try:
        profile, prefs = services.read_profile(cfg, conn)
        assert profile is None
        assert prefs is None
        assert conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM preferences").fetchone()[0] == 0
        with pytest.raises(services.SetupRequired):
            services.require_profile(cfg, conn)
    finally:
        conn.close()
