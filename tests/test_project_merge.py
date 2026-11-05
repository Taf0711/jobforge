from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from jobforge import db, services
from jobforge.config import (
    BrowserConfig,
    Config,
    EmailConfig,
    LLMConfig,
    ResumeImportConfig,
    ServerConfig,
)
from jobforge.matching.projects import merge_project_bank
from jobforge.models import Profile
from jobforge.web.app import create_app


def _existing() -> list[dict]:
    return [
        {
            "title": "N-Body Galaxy Simulator",
            "tech_line": "C++",
            "description": "C++",
            "bullets": ["old bullet"],
            "source": "resume-forge",
            "source_id": "proj-12",
            "tags": ["physics", "c++"],
        },
        {
            "title": "Keeper",
            "tech_line": "Python",
            "bullets": ["keep this"],
            "source": "github",
            "repo_id": 42,
        },
    ]


def _facts() -> list[dict]:
    return [
        {
            "id": "rf-proj-12-01",
            "section": "projects",
            "title": "N-Body Galaxy Simulator",
            "text": "old bullet",
            "skills": ["C++"],
        },
        {
            "id": "gh-proj-02-01",
            "section": "projects",
            "title": "Keeper",
            "text": "keep this",
            "skills": ["Python"],
        },
        {
            "id": "rf-exp-01-01",
            "section": "experience",
            "title": "Engineer",
            "text": "keep experience",
            "skills": ["Python"],
        },
    ]


def test_merge_updates_matches_appends_new_and_preserves_the_rest():
    result = merge_project_bank(
        _existing(),
        [
            {
                "title": "N-Body Galaxy Simulator",
                "tech_line": "C++, SIMD, WebGPU",
                "bullets": ["new one", "new two"],
            },
            {"title": "New Project", "tech_line": "Go", "bullets": ["new project"]},
        ],
        existing_facts=_facts(),
        mode="merge",
    )

    assert result.mode == "merge"
    assert result.updated == 1
    assert result.added == 1
    assert result.preserved == 1
    assert [project["title"] for project in result.projects] == [
        "N-Body Galaxy Simulator",
        "Keeper",
        "New Project",
    ]
    nbody = result.projects[0]
    assert nbody["tech_line"] == "C++, SIMD, WebGPU"
    assert nbody["description"] == "C++, SIMD, WebGPU"
    assert nbody["bullets"] == ["new one", "new two"]
    assert nbody["source"] == "resume-forge"
    assert nbody["source_id"] == "proj-12"
    assert result.projects[1]["repo_id"] == 42
    assert [fact["id"] for fact in result.project_facts] == [
        "resume-proj-01-01",
        "resume-proj-01-02",
        "gh-proj-02-01",
        "resume-proj-03-01",
    ]
    assert result.project_facts[2]["text"] == "keep this"


def test_replace_mirrors_the_resume_and_drops_other_projects():
    result = merge_project_bank(
        _existing(),
        [{"title": "Only Project", "tech_line": "Rust", "bullets": ["only"]}],
        existing_facts=_facts(),
        mode="replace",
    )

    assert result.mode == "replace"
    assert [project["title"] for project in result.projects] == ["Only Project"]
    assert result.added == 1
    assert result.preserved == 0
    assert [fact["id"] for fact in result.project_facts] == ["resume-proj-01-01"]
    assert all(fact["section"] == "projects" for fact in result.project_facts)


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
        config_path=tmp_path / "config.yml",
        resume_import=ResumeImportConfig(project_mode="merge"),
    )
    cfg.ensure_dirs()
    db.init_db(cfg.db_path)
    return cfg


def test_reparse_service_updates_only_projects_and_facts(tmp_path: Path):
    cfg = _config(tmp_path)
    resume = tmp_path / "resume.md"
    resume.write_text(
        """
Ada Lovelace
ada@example.com

Experience
Analytical Engines | 2024 - 2025
• Wrote notes

Projects
N-Body Galaxy Simulator | C++, SIMD
• New bullet
""",
        encoding="utf-8",
    )
    conn = db.connect(cfg.db_path)
    profile = Profile(
        name="ada",
        resume_path=str(resume),
        experience=[{"title": "Engineer", "company": "Analytical Engines", "bullets": ["keep"]}],
        projects=_existing(),
        facts=_facts(),
        skills=["Python", "C++"],
    )
    profile.id = db.save_profile(conn, profile)
    db.set_active_profile(conn, profile.id)

    dry = services.reparse_profile_projects(cfg, conn, profile_id=profile.id, dry_run=True)
    assert dry["projects_before"] == 2
    assert dry["updated"] == 1
    assert dry["preserved"] == 1
    assert db.get_profile(conn, profile.id).projects[0]["bullets"] == ["old bullet"]

    result = services.reparse_profile_projects(cfg, conn, profile_id=profile.id)
    saved = db.get_profile(conn, profile.id)
    assert result["projects_after"] == 2
    assert [project["title"] for project in saved.projects] == [
        "N-Body Galaxy Simulator",
        "Keeper",
    ]
    assert saved.projects[0]["bullets"] == ["New bullet"]
    assert saved.experience[0]["bullets"] == ["keep"]
    assert saved.skills == ["Python", "C++"]
    assert any(fact["id"] == "gh-proj-02-01" for fact in saved.facts)
    assert any(fact["section"] == "experience" for fact in saved.facts)
    assert not any(
        fact.get("id") == "rf-proj-12-01" for fact in saved.facts
    )
    first_ids = [fact["id"] for fact in saved.facts]
    services.reparse_profile_projects(cfg, conn, profile_id=profile.id)
    again = db.get_profile(conn, profile.id)
    assert [fact["id"] for fact in again.facts] == first_ids
    conn.close()



def test_resume_upload_honors_merge_mode(tmp_path: Path):
    cfg = _config(tmp_path)
    resume = tmp_path / "updated.md"
    resume.write_text(
        """
Ada Lovelace
ada@example.com

Experience
Analytical Engines
2024 - 2025
Software Engineer
New York, NY
• Updated experience

Projects
N-Body Galaxy Simulator | C++, SIMD
• Updated project
""",
        encoding="utf-8",
    )
    conn = db.connect(cfg.db_path)
    profile = Profile(
        name="ada",
        experience=[{"title": "Engineer", "company": "Old Co", "bullets": ["old"]}],
        projects=_existing(),
        facts=_facts(),
        skills=["Python"],
    )
    profile.id = db.save_profile(conn, profile)
    db.set_active_profile(conn, profile.id)

    merged = services.refresh_profile_from_resume(cfg, conn, resume)
    assert [project["title"] for project in merged.projects] == [
        "N-Body Galaxy Simulator",
        "Keeper",
    ]
    assert merged.projects[0]["bullets"] == ["Updated project"]
    assert merged.projects[1]["repo_id"] == 42
    assert merged.experience[0]["company"] == "Analytical Engines"
    assert any(fact["id"] == "gh-proj-02-01" for fact in merged.facts)
    conn.close()


def test_profiles_reparse_route_updates_active_profile(tmp_path: Path):
    cfg = _config(tmp_path)
    resume = tmp_path / "route.md"
    resume.write_text(
        """
Ada Lovelace
ada@example.com

Projects
N-Body Galaxy Simulator | C++, SIMD
• Route bullet
""",
        encoding="utf-8",
    )
    conn = db.connect(cfg.db_path)
    profile = Profile(
        name="ada",
        resume_path=str(resume),
        projects=_existing(),
        facts=_facts(),
    )
    profile.id = db.save_profile(conn, profile)
    db.set_active_profile(conn, profile.id)
    db.setting_set(conn, "setup_complete", "1")
    conn.close()

    client = TestClient(create_app(cfg))
    response = client.post("/profiles/reparse", follow_redirects=False)
    assert response.status_code == 303
    assert "/profiles?msg=" in response.headers["location"]

    conn = db.connect(cfg.db_path)
    saved = db.get_profile(conn, profile.id)
    assert saved.projects[0]["bullets"] == ["Route bullet"]
    assert saved.projects[1]["title"] == "Keeper"
    assert saved.experience == []
    conn.close()
