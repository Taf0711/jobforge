from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import yaml

from jobforge import db
from jobforge.cli import cmd_import_resume_forge
from jobforge.config import load_config
from jobforge.integrations.resume_forge import (
    import_applications,
    load_applications,
    load_network,
    load_profile,
    preferences_from_profile,
)
from jobforge.matching.jd import parse_jd
from jobforge.models import Profile
from jobforge.tailoring.tailor import select_projects


PROFILE = {
    "candidate": {
        "name": "Test User",
        "email": "test@example.com",
        "phone": "+1 555 0100",
        "linkedin": "https://linkedin.com/in/test",
        "github": "https://github.com/test",
        "current_location": "New York City, NY",
    },
    "education": {
        "institution": "Hunter College",
        "degree": "Bachelor of Arts",
        "major": "Computer Science",
        "graduation": "June 2026",
        "relevant_courses": ["Databases"],
    },
    "authorization": {
        "us_citizen": True,
        "work_authorized_us": True,
        "sponsorship_needed": False,
        "itar_eligible": True,
        "clearance_eligible": True,
    },
    "preferences": {
        "preferred_locations": ["New York City", "Remote USA"],
        "open_to_relocation": True,
        "target_domains": ["Backend/platform", "AI/ML"],
    },
    "scoring_inputs": {"gpa_for_scoring": 3.03},
    "experience": [
        {
            "id": "exp_acme",
            "organization": "Acme",
            "display_organization": "Acme",
            "title": "Software Engineer",
            "location": "New York City, NY",
            "start": "2024-01",
            "end": "2025-01",
            "evidence_tags": ["Python", "SQL"],
            "bullet_facts": ["Built Python pipelines processing 1M rows."],
        }
    ],
}

PROJECTS = {
    "projects": [
        {
            "name": "Alpha",
            "tech_line": "Python, FastAPI",
            "tags": ["Python", "backend"],
            "best_for": ["backend"],
            "bullets": ["Built a FastAPI service for job ingestion."],
        },
        {
            "name": "Beta",
            "tech_line": "React, TypeScript",
            "tags": ["React", "frontend"],
            "best_for": ["frontend"],
            "bullets": ["Built a React dashboard."],
        },
        {
            "name": "Gamma",
            "tech_line": "Go, Docker",
            "tags": ["Go", "Docker"],
            "best_for": ["backend"],
            "bullets": ["Built a Go worker with Docker."],
        },
        {
            "name": "Delta",
            "tech_line": "Rust",
            "tags": ["Rust"],
            "best_for": ["systems"],
            "bullets": ["Built a Rust CLI."],
        },
    ]
}

EVIDENCE = {"skills": {"languages": {"Python": {}, "Go": {}}, "technologies": {"FastAPI": {}}}}

NETWORK = {
    "connections": [
        {
            "name": "Riley Recruiter",
            "company": "Acme",
            "title": "Technical Recruiter",
            "relationship": "strong",
            "how": "personal friend who recruits at Acme",
            "last_contacted": "2026-01-01",
        }
    ]
}

APPLICATIONS = {
    "applications": [
        {
            "id": 7,
            "company": "Acme",
            "role": "Software Engineer",
            "location": "New York City, NY",
            "salary": "$100k-$120k",
            "applied_at": "2026-02-01",
            "status": "Applied",
            "source_url": "https://jobs.example/acme/7",
            "notes": "submitted",
            "outcome": {
                "channel": "cold_portal",
                "email_used": "test@example.com",
                "current_stage": "Applied",
                "notes_outcome": "no response yet",
            },
        }
    ]
}


def _write(tmp_path: Path, name: str, payload: dict) -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def test_load_profile_maps_verified_data(tmp_path: Path):
    profile = load_profile(
        _write(tmp_path, "profile.yml", PROFILE),
        projects_path=_write(tmp_path, "projects.yml", PROJECTS),
        evidence_path=_write(tmp_path, "evidence.yml", EVIDENCE),
    )

    assert profile.full_name == "Test User"
    assert profile.email == "test@example.com"
    assert profile.work_auth["gpa"] == 3.03
    assert profile.work_auth["us_citizen"] is True
    assert len(profile.experience) == 1
    assert len(profile.projects) == 4
    assert len(profile.facts) == 5
    assert {"Python", "Go", "FastAPI", "React", "Docker", "Rust"} <= set(profile.skills)
    assert profile.facts[0]["id"].startswith("rf-exp-")


def test_preferences_and_network_mapping(tmp_path: Path):
    profile_data = yaml.safe_load(_write(tmp_path, "profile.yml", PROFILE).read_text())
    prefs = preferences_from_profile(profile_data)
    assert prefs.locations == ["New York City", "Remote USA"]
    assert prefs.open_to_relocation is True
    assert prefs.needs_sponsorship is False

    network = load_network(_write(tmp_path, "network.yml", NETWORK))
    connection = network["connections"][0]
    assert connection["strength"] == "strong"
    assert connection["relationship"] == "recruiter"
    assert connection["role"] == "Technical Recruiter"
    assert connection["last_contact"] == "2026-01-01"


def test_select_projects_prefers_jd_relevant_projects(tmp_path: Path):
    profile = load_profile(
        _write(tmp_path, "profile.yml", PROFILE),
        projects_path=_write(tmp_path, "projects.yml", PROJECTS),
    )
    jd = parse_jd("Backend engineer. Requirements: Python, FastAPI, SQL. Nice to have: Docker.")

    selected = select_projects(profile, jd, max_projects=3)

    names = [item["name"] for item in selected]
    assert len(selected) == 3
    assert names[0] == "Alpha"
    assert "Gamma" in names  # backend/Docker project beats the zero-score tie


def test_import_applications_creates_tracker_rows(tmp_path: Path):
    conn = db.connect(tmp_path / "test.sqlite3")
    db.init_db(tmp_path / "test.sqlite3")
    conn = db.connect(tmp_path / "test.sqlite3")
    profile_id = db.save_profile(conn, Profile(name="test"))
    path = _write(tmp_path, "applications.yml", APPLICATIONS)

    counts = import_applications(conn, profile_id, path)
    rows = conn.execute("SELECT status, notes FROM applications").fetchall()
    jobs = conn.execute("SELECT company, title FROM jobs").fetchall()
    conn.close()

    assert counts == {"imported": 1, "skipped": 0}
    assert rows[0]["status"] == "submitted"
    assert "cold_portal" in rows[0]["notes"]
    assert jobs[0]["company"] == "Acme"
    assert load_applications(path)[0]["status"] == "submitted"


def test_cli_import_resume_forge_writes_profile_and_network(tmp_path: Path):
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        f"data_dir: {tmp_path / 'data'}\noutput_dir: {tmp_path / 'output'}\n"
        "sources: {}\npreferences: {}\nbrowser: {headless: true}\n",
        encoding="utf-8",
    )
    args = SimpleNamespace(
        config=str(cfg_path),
        profile=str(_write(tmp_path, "profile.yml", PROFILE)),
        projects=str(_write(tmp_path, "projects.yml", PROJECTS)),
        evidence=str(_write(tmp_path, "evidence.yml", EVIDENCE)),
        network=str(_write(tmp_path, "network.yml", NETWORK)),
        archetypes=str(_write(tmp_path, "archetypes.yml", {"archetypes": {"systems": {"label": "Systems", "jd_signals": ["C++"], "strongest_projects": ["Alpha"]}}})),
        applications=str(_write(tmp_path, "applications.yml", APPLICATIONS)),
        resume=None,
        include_unstarted=False,
        force_network=False,
    )

    assert cmd_import_resume_forge(args) == 0

    cfg = load_config(cfg_path)
    conn = db.connect(cfg.db_path)
    active = db.get_active_profile(conn)
    app_count = conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
    conn.close()
    assert active is not None
    assert active.full_name == "Test User"
    assert (cfg.data_dir / "network.yml").is_file()
    assert (cfg.data_dir / "role_archetypes.yml").is_file()
    assert app_count == 1
