from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

import yaml
from fastapi.testclient import TestClient

from jobforge import db
from jobforge.config import BrowserConfig, Config, EmailConfig, LLMConfig, ServerConfig
from jobforge.models import Preferences, Profile
from jobforge.web.app import create_app
from types import SimpleNamespace

from jobforge.web.setup_presets import build_sources, custom_boards, selected_presets, source_board_count


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
    )
    cfg.ensure_dirs()
    cfg.config_path.write_text(
        yaml.safe_dump(
            {"data_dir": str(cfg.data_dir), "output_dir": str(cfg.output_dir)},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return cfg


def test_preset_detection_and_custom_boards():
    cfg = SimpleNamespace(
        sources={
            "greenhouse": {"boards": ["anthropic", "scaleai", "databricks", "acme"]},
            "ashby": {"boards": ["baseten", "perplexity", "cohere", "fireworks", "modal", "decagon", "sierra", "elevenlabs", "browserbase", "cognition"]},
        }
    )

    assert "ai" in selected_presets(cfg)
    assert custom_boards(cfg)["greenhouse"] == "acme"
    built = build_sources(["ai"], {"greenhouse": "acme", "workday": "acme|wd5|External"})
    assert "acme" in built["greenhouse"]["boards"]
    assert built["workday"]["boards"] == ["acme|wd5|External"]


def test_fresh_install_redirects_to_setup(tmp_path: Path):
    cfg = _config(tmp_path)
    client = TestClient(create_app(cfg))

    home = client.get("/", follow_redirects=False)
    assert home.status_code == 303
    assert home.headers["location"] == "/setup"

    page = client.get("/setup")
    assert page.status_code == 200
    assert "Set up JobForge" in page.text
    assert "Upload a resume" in page.text


def test_all_setup_steps_render(tmp_path: Path):
    cfg = _config(tmp_path)
    client = TestClient(create_app(cfg))

    for step in (1, 2, 3):
        page = client.get(f"/setup?step={step}")
        assert page.status_code == 200, (step, page.text[:400])
        assert "Set up JobForge" in page.text
    clamped = client.get("/setup?step=5")
    assert clamped.status_code == 200
    assert "3 · Start" in clamped.text


def test_setup_preferences_and_sources_are_saved_and_cfg_updates(tmp_path: Path):
    cfg = _config(tmp_path)
    client = TestClient(create_app(cfg))

    prefs = client.post(
        "/setup/preferences",
        data={
            "titles": "Software Engineer, Backend Engineer",
            "locations": "New York, NY; Remote",
            "work_modes": "remote, hybrid",
            "employment_types": "full-time",
            "experience_levels": "entry, junior",
            "salary_min": "90000",
            "min_match": "45",
        },
        follow_redirects=False,
    )
    assert prefs.status_code == 303
    assert prefs.headers["location"].startswith("/setup?step=3")

    conn = db.connect(cfg.db_path)
    profile = db.get_active_profile(conn)
    saved = db.get_preferences(conn, profile.id or 0)
    assert saved.titles == ["Software Engineer", "Backend Engineer"]
    assert saved.locations == ["New York, NY", "Remote"]
    conn.close()

    sources = client.post(
        "/setup/sources",
        data={
            "preset_ai": "1",
            "preset_public": "1",
            "scan_keywords": "software engineer, backend",
            "scan_exclude": "senior, staff",
            "max_new": "500",
        },
        follow_redirects=False,
    )
    assert sources.status_code == 303
    assert sources.headers["location"].startswith("/setup?step=3")

    data = yaml.safe_load(cfg.config_path.read_text(encoding="utf-8"))
    assert "anthropic" in data["sources"]["greenhouse"]["boards"]
    assert data["sources"]["nyc_opendata"]["enabled"] is True
    assert data["scan"]["keywords"] == ["software engineer", "backend"]
    assert data["scan"]["exclude_keywords"] == ["senior", "staff"]
    assert cfg.sources["greenhouse"]["boards"] == data["sources"]["greenhouse"]["boards"]
    assert source_board_count(cfg.sources) >= 10
    assert "ai" in selected_presets(cfg)
    assert "public" in selected_presets(cfg)


def test_setup_github_lists_preset_writes_repos(tmp_path: Path):
    cfg = _config(tmp_path)
    client = TestClient(create_app(cfg))

    response = client.post(
        "/setup/sources",
        data={"preset_github_lists": "1"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    data = yaml.safe_load(cfg.config_path.read_text(encoding="utf-8"))
    assert data["sources"]["github_jobs"]["repos"] == [
        "SimplifyJobs/New-Grad-Positions",
        "SimplifyJobs/Summer2026-Internships",
    ]
    assert "github_lists" in selected_presets(cfg)


def test_setup_sources_requires_a_choice(tmp_path: Path):
    cfg = _config(tmp_path)
    client = TestClient(create_app(cfg))

    response = client.post("/setup/sources", data={}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/setup?step=3")
    assert "Choose at least one" in unquote(response.headers["location"])


def test_setup_custom_boards_are_written(tmp_path: Path):
    cfg = _config(tmp_path)
    client = TestClient(create_app(cfg))

    response = client.post(
        "/setup/sources",
        data={"greenhouse_custom": "acme, beta", "workday_custom": "acme|wd5|External"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    data = yaml.safe_load(cfg.config_path.read_text(encoding="utf-8"))
    assert data["sources"]["greenhouse"]["boards"] == ["acme", "beta"]
    assert data["sources"]["workday"]["boards"] == ["acme|wd5|External"]


def test_setup_profile_upload_and_finish(tmp_path: Path):
    cfg = _config(tmp_path)
    client = TestClient(create_app(cfg))
    profile_yaml = """
name: ada
first_name: Ada
last_name: Lovelace
email: ada@example.com
phone: '+1 555 0100'
location: London, UK
skills: [Python, SQL]
"""

    upload = client.post(
        "/setup/profile",
        files={"file": ("profile.yml", profile_yaml, "application/x-yaml")},
        data={"action": "upload"},
        follow_redirects=False,
    )
    assert upload.status_code == 303
    assert upload.headers["location"].startswith("/setup?step=2")

    conn = db.connect(cfg.db_path)
    profile = db.get_active_profile(conn)
    assert profile.email == "ada@example.com"
    assert "Python" in profile.skills
    conn.close()

    client.post(
        "/setup/sources",
        data={"preset_product": "1"},
        follow_redirects=False,
    )
    finish = client.post("/setup/finish", follow_redirects=False)
    assert finish.status_code == 303
    assert finish.headers["location"].startswith("/?")

    conn = db.connect(cfg.db_path)
    assert db.setting_get(conn, "setup_complete") == "1"
    conn.close()

    home = client.get("/", follow_redirects=False)
    assert home.status_code == 200
    assert "Setup is incomplete" not in home.text


def test_setup_models_moved_to_models_page(tmp_path: Path):
    cfg = _config(tmp_path)
    client = TestClient(create_app(cfg))

    response = client.post(
        "/setup/models",
        data={"action": "skip"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/setup?step=3")


def test_setup_skip_marks_complete(tmp_path: Path):
    cfg = _config(tmp_path)
    client = TestClient(create_app(cfg))

    response = client.post("/setup/skip", follow_redirects=False)

    assert response.status_code == 303
    conn = db.connect(cfg.db_path)
    assert db.setting_get(conn, "setup_complete") == "1"
    conn.close()
    assert client.get("/", follow_redirects=False).status_code == 200


def test_existing_profile_and_sources_do_not_redirect(tmp_path: Path):
    cfg = _config(tmp_path)
    cfg.sources = {"greenhouse": {"boards": ["acme"]}}
    db.init_db(cfg.db_path)
    conn = db.connect(cfg.db_path)
    profile = Profile(name="existing", first_name="Ada", email="ada@example.com")
    profile.id = db.save_profile(conn, profile)
    db.set_active_profile(conn, profile.id)
    db.save_preferences(conn, profile.id, Preferences(titles=["Software Engineer"]))
    conn.close()
    client = TestClient(create_app(cfg))

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 200
    assert "Setup is incomplete" not in response.text


def test_setup_preferences_applies_recommended_sources_by_default(tmp_path: Path):
    cfg = _config(tmp_path)
    client = TestClient(create_app(cfg))

    response = client.post(
        "/setup/preferences",
        data={
            "titles": "Software Engineer",
            "locations": "Remote",
            "source_mix": "recommended",
            "min_match": "45",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/setup?step=3")

    data = yaml.safe_load(cfg.config_path.read_text(encoding="utf-8"))
    assert "anthropic" in data["sources"]["greenhouse"]["boards"]
    assert data["sources"]["github_jobs"]["repos"]
    assert "nyc_opendata" not in data["sources"]
    assert cfg.sources["greenhouse"]["boards"] == data["sources"]["greenhouse"]["boards"]


def test_setup_finish_applies_default_sources_when_empty(tmp_path: Path):
    cfg = _config(tmp_path)
    client = TestClient(create_app(cfg))

    response = client.post("/setup/finish", follow_redirects=False)
    assert response.status_code == 303

    data = yaml.safe_load(cfg.config_path.read_text(encoding="utf-8"))
    assert "anthropic" in data["sources"]["greenhouse"]["boards"]
    assert data["sources"]["github_jobs"]["repos"]
    conn = db.connect(cfg.db_path)
    assert db.setting_get(conn, "setup_complete") == "1"
    conn.close()


def test_source_mix_switch_drops_old_preset_boards_but_keeps_custom(tmp_path: Path):
    cfg = _config(tmp_path)
    client = TestClient(create_app(cfg))

    client.post(
        "/setup/preferences",
        data={"source_mix": "broad", "titles": "Software Engineer"},
        follow_redirects=False,
    )
    broad = yaml.safe_load(cfg.config_path.read_text(encoding="utf-8"))
    assert "anthropic" in broad["sources"]["greenhouse"]["boards"]

    client.post(
        "/setup/preferences",
        data={"source_mix": "minimal", "titles": "Software Engineer"},
        follow_redirects=False,
    )
    minimal = yaml.safe_load(cfg.config_path.read_text(encoding="utf-8"))
    assert "greenhouse" not in minimal["sources"]
    assert minimal["sources"]["github_jobs"]["repos"]

    client.post(
        "/setup/preferences",
        data={
            "source_mix": "minimal",
            "titles": "Software Engineer",
            "greenhouse_custom": "acme",
        },
        follow_redirects=False,
    )
    custom = yaml.safe_load(cfg.config_path.read_text(encoding="utf-8"))
    assert custom["sources"]["greenhouse"]["boards"] == ["acme"]


def test_source_mix_keep_preserves_partial_existing_sources(tmp_path: Path):
    cfg = _config(tmp_path)
    cfg.sources = {"greenhouse": {"boards": ["anthropic"]}}
    client = TestClient(create_app(cfg))

    response = client.post(
        "/setup/preferences",
        data={"source_mix": "keep", "titles": "Software Engineer"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    data = yaml.safe_load(cfg.config_path.read_text(encoding="utf-8"))
    assert data["sources"]["greenhouse"]["boards"] == ["anthropic"]
    assert "ashby" not in data["sources"]
