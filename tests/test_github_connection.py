from __future__ import annotations

import json
import stat
from pathlib import Path

from fastapi.testclient import TestClient

from jobforge import db
from jobforge.config import BrowserConfig, Config, EmailConfig, LLMConfig, ServerConfig
from jobforge.integrations import github as gh
from jobforge.integrations.github import (
    GitHubTokenStore,
    bullets_from_readme,
    import_repo_project,
    normalize_repo,
    remove_repo_project,
)
from jobforge.models import Profile
from jobforge.web.app import create_app

RAW_REPO = {
    "id": 12345,
    "name": "splice",
    "full_name": "taf/splice",
    "html_url": "https://github.com/taf/splice",
    "description": "A Python job ingestion service",
    "homepage": "",
    "language": "Python",
    "topics": ["fastapi", "postgresql", "job-search"],
    "stargazers_count": 12,
    "forks_count": 2,
    "private": False,
    "fork": False,
    "archived": False,
    "pushed_at": "2026-10-20T12:00:00Z",
    "created_at": "2026-01-01T12:00:00Z",
    "owner": {"login": "taf"},
}

README = """# Splice

A service for collecting job postings.

## Features

- Built a FastAPI ingestion pipeline that normalizes 40+ public job sources.
- Added PostgreSQL fingerprint deduplication to avoid repeat postings.
- **Bold feature** with a short explanation.

## Installation

- Run `npm install` and `npm run dev` at http://localhost:3000
- See https://example.com for details
"""


class FakeGitHubClient:
    def __init__(self, token: str, **_kwargs):
        if not token:
            raise gh.GitHubAuthError("missing token")
        self.token = token

    def user(self):
        return {
            "account_id": "99",
            "login": "taf",
            "display_name": "Taf Test",
            "avatar_url": "",
            "html_url": "https://github.com/taf",
            "scopes": "read:user",
            "public_repos": 1,
            "total_private_repos": 0,
            "plan": "free",
        }

    def repos(self):
        return [normalize_repo(RAW_REPO)]

    def repo_context(self, owner: str, name: str):
        assert (owner, name) == ("taf", "splice")
        return {"Python": 100, "FastAPI": 50, "PostgreSQL": 25}, README


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
    conn = db.connect(cfg.db_path)
    profile = Profile(
        name="taf",
        first_name="Taf",
        last_name="Test",
        email="taf@example.com",
        phone="+1 555 0100",
        skills=["Python"],
    )
    profile.id = db.save_profile(conn, profile)
    db.set_active_profile(conn, profile.id)
    conn.close()
    return cfg


def test_token_store_file_fallback_is_0600(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("JOBFORGE_GITHUB_TOKEN", raising=False)
    store = GitHubTokenStore(tmp_path, keyring_module=False)
    record = store.save("ghp_test_token")
    assert record.backend == "file"
    loaded = store.load()
    assert loaded is not None
    assert loaded.token == "ghp_test_token"
    assert loaded.backend == "file"
    assert stat.S_IMODE(store.token_path.stat().st_mode) == 0o600
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_env_token")
    assert store.load("file").token == "ghp_test_token"
    assert store.load("env").token == "ghp_env_token"
    store.delete()
    assert store.load("file") is None


def test_env_token_is_a_connection_without_storage(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_env_token")
    store = GitHubTokenStore(tmp_path, keyring_module=False)
    loaded = store.load()
    assert loaded is not None
    assert loaded.backend == "env"
    assert not store.token_path.exists()


def test_readme_suggestions_skip_install_steps_and_strip_markdown():
    bullets = bullets_from_readme(README)
    assert len(bullets) == 3
    assert "FastAPI ingestion pipeline" in bullets[0]
    assert "PostgreSQL fingerprint deduplication" in bullets[1]
    assert "Bold feature" in bullets[2]
    assert not any("npm install" in bullet for bullet in bullets)


def test_readme_cleanup_does_not_eat_sections_around_literal_angles():
    readme = """# Tool

## Setup

1. Go to <https://example.com> and click Apply.

## What it actually watches

1. **Signals** — a long signal description
   that continues on an indented line.
2. **Second signal** — another useful description.

## Installation

- npm install everything
"""
    bullets = bullets_from_readme(readme)
    assert bullets == [
        "Signals — a long signal description that continues on an indented line",
        "Second signal — another useful description",
    ]


def test_import_update_and_remove_project_are_truthful_and_idempotent():
    profile = Profile(name="taf", skills=["Python"])
    repo = normalize_repo(RAW_REPO)
    project, added = import_repo_project(
        profile,
        repo,
        title="Splice",
        tech_line="Python, FastAPI, PostgreSQL",
        bullets=["Built a FastAPI ingestion pipeline.", "Added PostgreSQL deduplication."],
        tags=["Python", "FastAPI", "PostgreSQL"],
    )
    assert project["source"] == "github"
    assert project["source_id"] == "github-12345"
    assert project["url"] == "https://github.com/taf/splice"
    assert "FastAPI" in added
    assert "FastAPI" in profile.skills
    assert len([f for f in profile.facts if f["section"] == "projects"]) == 2

    updated, _ = import_repo_project(
        profile,
        repo,
        title="Splice",
        tech_line="Python, FastAPI",
        bullets=["Built a FastAPI ingestion pipeline."],
        tags=["Python", "FastAPI"],
        add_skills=False,
    )
    assert updated["tech_line"] == "Python, FastAPI"
    assert len([f for f in profile.facts if f["section"] == "projects"]) == 1
    assert len([p for p in profile.projects if p["source_id"] == "github-12345"]) == 1

    assert remove_repo_project(profile, 12345) is True
    assert not profile.projects
    assert not [f for f in profile.facts if f.get("source_id") == "github-12345"]


def test_connect_browse_import_and_disconnect_routes(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("JOBFORGE_DISABLE_KEYRING", "1")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("JOBFORGE_GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(gh, "GitHubClient", FakeGitHubClient)
    cfg = _config(tmp_path)
    client = TestClient(create_app(cfg))

    page = client.get("/connections")
    assert page.status_code == 200
    assert "GitHub" in page.text
    assert "Connect GitHub" in page.text

    connected = client.post(
        "/connections/github/connect",
        data={"token": "ghp_test_token"},
        follow_redirects=False,
    )
    assert connected.status_code == 303
    assert connected.headers["location"].startswith("/connections/github")

    conn = db.connect(cfg.db_path)
    connection = db.get_connection(conn, "github")
    assert connection and connection["login"] == "taf"
    assert db.get_github_repo(conn, 12345) is not None
    conn.close()

    connected_page = client.get("/connections")
    assert connected_page.status_code == 200
    assert "@taf" in connected_page.text
    assert "Browse repositories" in connected_page.text
    assert "ghp_test_token" not in connected_page.text
    assert b"ghp_test_token" not in cfg.db_path.read_bytes()
    conn = db.connect(cfg.db_path)
    exported = json.dumps(db.export_all(conn))
    assert "connections" in exported and "github_repos" in exported
    assert "ghp_test_token" not in exported
    conn.close()

    synced = client.post("/connections/github/sync", follow_redirects=False)
    assert synced.status_code == 303
    assert synced.headers["location"].startswith("/connections/github")

    repos = client.get("/connections/github")
    assert repos.status_code == 200
    assert "taf/splice" in repos.text

    detail = client.get("/connections/github/repos/12345")
    assert detail.status_code == 200
    assert "FastAPI ingestion pipeline" in detail.text
    assert "PostgreSQL fingerprint deduplication" in detail.text
    assert "README" in detail.text

    imported = client.post(
        "/connections/github/repos/12345/import",
        data={
            "title": "Splice",
            "tech_line": "Python, FastAPI, PostgreSQL",
            "bullets": "Built a FastAPI ingestion pipeline.\nAdded PostgreSQL deduplication.",
            "tags": "Python, FastAPI, PostgreSQL",
            "add_skills": "1",
        },
        follow_redirects=False,
    )
    assert imported.status_code == 303
    conn = db.connect(cfg.db_path)
    profile = db.get_active_profile(conn)
    assert profile is not None
    assert any(p.get("source_id") == "github-12345" for p in profile.projects)
    assert "FastAPI" in profile.skills
    assert any(f.get("source_id") == "github-12345" for f in profile.facts)
    conn.close()

    profiles = client.get("/profiles")
    assert profiles.status_code == 200
    assert "Project bank" in profiles.text
    assert "Splice" in profiles.text

    removed = client.post(
        "/connections/github/repos/12345/remove", follow_redirects=False
    )
    assert removed.status_code == 303
    conn = db.connect(cfg.db_path)
    profile = db.get_active_profile(conn)
    assert not any(p.get("source_id") == "github-12345" for p in profile.projects)
    conn.close()

    disconnected = client.post(
        "/connections/github/disconnect", follow_redirects=False
    )
    assert disconnected.status_code == 303
    conn = db.connect(cfg.db_path)
    assert db.get_connection(conn, "github") is None
    assert db.get_github_repo(conn, 12345) is None
    conn.close()
    assert client.get("/connections").status_code == 200
