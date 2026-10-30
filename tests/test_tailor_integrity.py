from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from jobforge import db, services
from jobforge.config import BrowserConfig, Config, EmailConfig, LLMConfig, ServerConfig
from jobforge.matching.jd import parse_jd
from jobforge.models import Application, Job, Preferences, Profile
from jobforge.tailoring.ats_check import ats_check
from jobforge.tailoring.tailor import (
    TailorResult,
    _output_dir_for,
    _output_dir_name,
    _supported,
    _trim_render_data,
    build_data,
    cover_letter,
    summarize,
    tailor_resume,
)


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
    return cfg


def _seed(tmp_path: Path):
    cfg = _config(tmp_path)
    db.init_db(cfg.db_path)
    conn = db.connect(cfg.db_path)
    resume = tmp_path / "base.pdf"
    resume.write_bytes(b"%PDF-1.4")
    profile = Profile(
        name="tester",
        first_name="Jane",
        last_name="Doe",
        email="jane@example.com",
        phone="+1 555 0100",
        resume_path=str(resume),
    )
    profile.id = db.save_profile(conn, profile)
    db.set_active_profile(conn, profile.id)
    db.save_preferences(conn, profile.id, Preferences())
    job = Job(
        source="lever",
        source_id="integrity-1",
        company="Acme",
        title="Software Engineer",
        location="Remote",
        description="Requirements:\n- Python",
    )
    job.id = db.upsert_job(conn, job)
    return cfg, conn, job


def _result(*, ok: bool = True) -> TailorResult:
    return TailorResult(
        pdf_path="new.pdf",
        typ_path="new.typ",
        data_path="new.json",
        cover_letter_path="new-cover.txt",
        ats={"ok": ok, "errors": [] if ok else ["Resume is 2 pages"], "warnings": []},
    )


def test_output_dir_name_is_unique_for_same_company_and_title():
    first = Job(id=1, source="greenhouse", source_id="1", company="Affirm", title="Software Engineer", location="Remote US")
    second = Job(id=2, source="greenhouse", source_id="2", company="Affirm", title="Software Engineer", location="Remote Canada")
    assert _output_dir_name(first) != _output_dir_name(second)
    assert _output_dir_name(first).endswith("-1")
    assert _output_dir_name(second).endswith("-2")


def test_artifact_directory_is_scoped_by_profile():
    job = Job(
        id=7,
        source="lever",
        source_id="7",
        company="Acme",
        title="Software Engineer",
        location="Remote",
    )
    first = Profile(id=1, name="first")
    second = Profile(id=2, name="second")

    first_dir = _output_dir_for(first, job, "/tmp/output")
    second_dir = _output_dir_for(second, job, "/tmp/output")

    assert first_dir != second_dir
    assert "profile-1" in first_dir.parts
    assert "profile-2" in second_dir.parts


def test_tailor_resume_writes_separate_artifacts_per_profile(
    tmp_path: Path, monkeypatch
):
    from jobforge.tailoring import tailor as tailor_module

    seen: list[Path] = []

    def fake_render(data, out_dir, template=None):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        seen.append(out_dir)
        pdf = out_dir / "resume.pdf"
        typ = out_dir / "resume.typ"
        payload = out_dir / "resume.json"
        for path in (pdf, typ, payload):
            path.write_text("x", encoding="utf-8")
        return pdf, typ, payload

    monkeypatch.setattr(tailor_module, "render", fake_render)
    monkeypatch.setattr(
        tailor_module, "ats_check", lambda path: {"ok": True, "pages": 1}
    )
    job = Job(
        id=9,
        source="lever",
        source_id="9",
        company="Acme",
        title="Software Engineer",
        description="Requirements:\n- Python",
    )

    for profile_id, name in ((1, "First"), (2, "Second")):
        profile = Profile(
            id=profile_id,
            name=name,
            first_name=name,
            last_name="Candidate",
            email=f"{name.lower()}@example.com",
            phone="+1 555 0100",
            skills=["Python"],
            facts=[
                {
                    "id": f"fact-{profile_id}",
                    "section": "experience",
                    "text": "Built Python services.",
                    "skills": ["Python"],
                }
            ],
        )
        tailor_resume(profile, job, tmp_path, write_cover_letter=False)

    assert len(seen) == 2
    assert seen[0] != seen[1]
    assert "profile-1" in seen[0].parts
    assert "profile-2" in seen[1].parts


def test_trim_render_data_deep_copies_without_error():
    data = {
        "experience": [{"title": "Engineer", "bullets": ["a", "b", "c", "d"]}],
        "projects": [{"title": "P", "bullets": ["x", "y"]}],
        "skill_groups": [{"name": "Languages", "items": ["Python", "Go", "Rust"]}],
        "skills": ["Python", "Go", "Rust"],
    }

    trimmed = _trim_render_data(
        data,
        experience_bullets=2,
        project_bullets=1,
        max_projects=1,
        max_groups=1,
        items_per_group=2,
    )

    assert trimmed["experience"][0]["bullets"] == ["a", "b"]
    assert data["experience"][0]["bullets"] == ["a", "b", "c", "d"]
    assert trimmed["skills"] == ["Python", "Go"]


def test_ats_check_blocks_two_pages_and_missing_contact(tmp_path: Path):
    pdf = tmp_path / "two-pages.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.new_page()
    for page in doc:
        page.insert_text((50, 50), "Python software engineer " + ("x" * 120))
    doc.save(pdf)
    doc.close()

    report = ats_check(pdf)

    assert report["ok"] is False
    assert report["pages"] == 2
    assert any("one page" in error for error in report["errors"])
    assert any("email" in error.lower() for error in report["errors"])
    assert any("phone" in error.lower() for error in report["errors"])


def test_cover_letter_fallback_does_not_claim_missing_skills():
    profile = Profile(
        first_name="Jane",
        last_name="Doe",
        email="jane@example.com",
        skills=["Python"],
        facts=[{"id": "f1", "text": "Built Python services."}],
    )
    job = Job(
        source="lever",
        source_id="1",
        company="Acme",
        title="Backend Engineer",
        description="Requirements:\n- Go\n- Kubernetes",
    )
    jd = parse_jd(job.description)

    text = cover_letter(profile, job, jd, None)

    assert "My background includes Python" in text
    assert "My background includes Go" not in text
    assert "My background includes Kubernetes" not in text


def test_llm_claims_are_rejected_when_skill_is_not_owned():
    class FakeLLM:
        available = True

        def chat(self, *args, **kwargs):
            return "Expert in Go and Kubernetes."

    profile = Profile(
        first_name="Jane",
        last_name="Doe",
        email="jane@example.com",
        summary="Python engineer.",
        skills=["Python"],
        facts=[{"id": "f1", "text": "Built Python services."}],
    )
    job = Job(
        source="lever",
        source_id="1",
        company="Acme",
        title="Backend Engineer",
        description="Requirements:\n- Go\n- Kubernetes",
    )
    jd = parse_jd(job.description)

    assert summarize(profile, job, jd, FakeLLM()) == profile.summary
    assert "Expert in Go" not in cover_letter(profile, job, jd, FakeLLM())
    assert _supported("Expert in Go", ["Built Python"], {"Python"}) is False
    assert _supported("Expert in Python", ["Built Python"], {"Python"}) is True


def test_tailor_job_preserves_prepared_application_state(tmp_path: Path, monkeypatch):
    cfg, conn, job = _seed(tmp_path)
    conn.execute(
        "INSERT INTO applications (job_id, profile_id, status, ats, resume_path, cover_letter_path, "
        "answers_json, screenshot_path, notes, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (job.id, 1, "prepared", "lever", "old.pdf", "old-cover.txt", '{"Q": "A"}', "shot.png", "", "2026-01-01", "2026-01-01"),
    )
    conn.commit()
    app_id = conn.execute("SELECT id FROM applications").fetchone()["id"]
    monkeypatch.setattr(services, "tailor_resume", lambda *args, **kwargs: _result())

    stats = services.tailor_job(cfg, conn, job.id)

    row = conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    assert stats["reused_application"] is True
    assert row["status"] == "prepared"
    assert row["resume_path"] == "new.pdf"
    assert row["cover_letter_path"] == "new-cover.txt"
    assert row["answers_json"] == '{"Q": "A"}'
    assert row["screenshot_path"] == "shot.png"
    conn.close()


def test_tailor_job_refuses_submitted_application(tmp_path: Path, monkeypatch):
    cfg, conn, job = _seed(tmp_path)
    conn.execute(
        "INSERT INTO applications (job_id, profile_id, status, ats, resume_path, answers_json, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (job.id, 1, "submitted", "lever", "sent.pdf", '{"Q": "A"}', "2026-01-01", "2026-01-01"),
    )
    conn.commit()
    monkeypatch.setattr(services, "tailor_resume", lambda *args, **kwargs: _result())

    with pytest.raises(ValueError, match="already submitted"):
        services.tailor_job(cfg, conn, job.id)

    row = conn.execute("SELECT * FROM applications").fetchone()
    assert row["status"] == "submitted"
    assert row["resume_path"] == "sent.pdf"
    assert row["answers_json"] == '{"Q": "A"}'
    conn.close()


def test_tailor_job_rejects_failed_ats_and_leaves_application_untouched(tmp_path: Path, monkeypatch):
    cfg, conn, job = _seed(tmp_path)
    monkeypatch.setattr(
        services, "tailor_resume", lambda *args, **kwargs: _result(ok=False)
    )

    with pytest.raises(ValueError, match="failed ATS checks"):
        services.tailor_job(cfg, conn, job.id)

    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0
    conn.close()


def test_tailor_job_refuses_before_llm_or_render(tmp_path: Path, monkeypatch):
    cfg, conn, job = _seed(tmp_path)
    sent = tmp_path / "sent.pdf"
    sent.write_bytes(b"ORIGINAL SENT BYTES")
    conn.execute(
        "INSERT INTO applications (job_id, profile_id, status, ats, resume_path, answers_json, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (job.id, 1, "submitted", "lever", str(sent), '{"Q": "A"}', "2026-01-01", "2026-01-01"),
    )
    conn.commit()
    called: list[str] = []

    monkeypatch.setattr(
        services, "LLMClient", lambda *args, **kwargs: called.append("llm")
    )
    monkeypatch.setattr(
        services, "tailor_resume", lambda *args, **kwargs: called.append("render")
    )

    with pytest.raises(ValueError, match="already submitted"):
        services.tailor_job(cfg, conn, job.id)

    assert called == []
    assert sent.read_bytes() == b"ORIGINAL SENT BYTES"
    conn.close()


def test_tailor_job_protects_a_prior_submission_event(tmp_path: Path, monkeypatch):
    cfg, conn, job = _seed(tmp_path)
    conn.execute(
        "INSERT INTO applications (job_id, profile_id, status, ats, resume_path, answers_json, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (job.id, 1, "prepared", "lever", "sent.pdf", '{"Q": "A"}', "2026-01-01", "2026-01-01"),
    )
    conn.commit()
    app_id = int(conn.execute("SELECT id FROM applications").fetchone()["id"])
    db.log_event(conn, app_id, "applied", "https://jobs.example/apply")
    called: list[str] = []
    monkeypatch.setattr(
        services, "LLMClient", lambda *args, **kwargs: called.append("llm")
    )
    monkeypatch.setattr(
        services, "tailor_resume", lambda *args, **kwargs: called.append("render")
    )

    with pytest.raises(ValueError, match="prior submission event"):
        services.tailor_job(cfg, conn, job.id)
    assert called == []
    conn.close()


def test_build_data_keeps_four_most_recent_experience_entries():
    profile = Profile(
        name="Four Roles",
        first_name="Four",
        last_name="Roles",
        email="four@example.com",
        phone="+1 555 010 0000",
        experience=[
            {
                "title": f"Engineer {index}",
                "company": f"Company {index}",
                "start": f"202{index}-01",
                "end": "Present" if index == 4 else f"202{index}-12",
                "bullets": [f"Built system {index} in Python."],
            }
            for index in range(5)
        ],
        facts=[],
    )
    data = build_data(profile, parse_jd("Python backend engineer"), [])
    assert len(data["experience"]) == 4
    assert [entry["company"] for entry in data["experience"]] == [
        "Company 0",
        "Company 1",
        "Company 2",
        "Company 3",
    ]
