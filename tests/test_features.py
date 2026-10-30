from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from jobforge import db
from jobforge.config import (
    BrowserConfig,
    Config,
    EmailConfig,
    FeatureFlags,
    LLMConfig,
    ServerConfig,
    load_config,
)
from jobforge.web.app import create_app


def _config(tmp_path: Path, features: FeatureFlags | None = None) -> Config:
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
        features=features or FeatureFlags(),
    )
    cfg.ensure_dirs()
    db.init_db(cfg.db_path)
    return cfg


def test_feature_flags_default_on_and_load_from_yaml(tmp_path: Path):
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        "features:\n  setup_wizard: false\n  github_connections: false\n  scoring_v2: false\n",
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    assert cfg.features.setup_wizard is False
    assert cfg.features.github_connections is False
    assert cfg.features.scoring_v2 is False

    config_path.write_text("features: {}\n", encoding="utf-8")
    cfg = load_config(config_path)
    assert cfg.features.to_dict() == {
        "setup_wizard": True,
        "github_connections": True,
        "scoring_v2": True,
        "agent": True,
        "agent_integrations": True,
    }


def test_feature_env_overrides_yaml(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.yml"
    config_path.write_text("features:\n  scoring_v2: false\n", encoding="utf-8")
    monkeypatch.setenv("JOBFORGE_FEATURE_SCORING_V2", "1")
    assert load_config(config_path).features.scoring_v2 is True
    monkeypatch.setenv("JOBFORGE_FEATURE_SCORING_V2", "0")
    assert load_config(config_path).features.scoring_v2 is False


def test_agent_budget_config_and_env_overrides(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        "agent:\n  max_steps: 4\n  max_seconds: 120\n  max_tool_calls: 3\n",
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    assert cfg.agent.max_steps == 4
    assert cfg.agent.max_seconds == 120
    assert cfg.agent.max_tool_calls == 3

    config_path.write_text("features: {}\n", encoding="utf-8")
    assert load_config(config_path).agent.max_tool_calls == 6

    monkeypatch.setenv("JOBFORGE_AGENT_MAX_TOOL_CALLS", "2")
    assert load_config(config_path).agent.max_tool_calls == 2
    monkeypatch.setenv("JOBFORGE_AGENT_MAX_TOOL_CALLS", "999")
    assert load_config(config_path).agent.max_tool_calls == 64


def test_disabled_setup_and_github_routes_are_hidden(tmp_path: Path):
    cfg = _config(
        tmp_path,
        FeatureFlags(setup_wizard=False, github_connections=False, scoring_v2=True, agent=False),
    )
    client = TestClient(create_app(cfg))

    home = client.get("/")
    assert home.status_code == 200
    assert "Connections" not in home.text
    assert ">Setup<" not in home.text
    assert ">Agent<" not in home.text
    assert ">Models<" not in home.text

    assert client.get("/setup").status_code == 404
    assert client.get("/setup?step=2").status_code == 404
    assert client.get("/connections").status_code == 404
    assert client.get("/connections/github").status_code == 404
    assert client.get("/agent").status_code == 404
    assert client.get("/providers").status_code == 404
    assert client.get("/api/agent/threads/1").status_code == 404
    settings_page = client.get("/settings")
    assert settings_page.status_code == 200
    assert "Feature flags" in settings_page.text
    assert 'name="scoring_v2"' in settings_page.text
    assert 'href="/setup"' not in settings_page.text


def test_agent_flag_hides_models_step_but_keeps_setup(tmp_path: Path):
    cfg = _config(
        tmp_path,
        FeatureFlags(setup_wizard=True, github_connections=True, scoring_v2=True, agent=False),
    )
    client = TestClient(create_app(cfg))

    page = client.get("/setup?step=4")
    assert page.status_code == 200
    assert "3 · Start" in page.text
    assert "Models" not in page.text
    assert client.get("/providers").status_code == 404
    assert client.get("/agent").status_code == 404


def test_enabled_setup_redirects_first_run(tmp_path: Path):
    cfg = _config(tmp_path, FeatureFlags(setup_wizard=True, github_connections=True))
    client = TestClient(create_app(cfg))

    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/setup"
    assert client.get("/setup").status_code == 200
    assert client.get("/connections").status_code == 200


def test_settings_feature_flags_write_config_and_take_effect(tmp_path: Path):
    cfg = _config(tmp_path)
    client = TestClient(create_app(cfg))

    saved = client.post("/settings/features", data={}, follow_redirects=False)
    assert saved.status_code == 303
    assert cfg.features.to_dict() == {
        "setup_wizard": False,
        "github_connections": False,
        "scoring_v2": False,
        "agent": False,
        "agent_integrations": False,
    }
    written = yaml.safe_load(Path(cfg.config_path).read_text(encoding="utf-8"))
    assert written["features"] == {
        "setup_wizard": False,
        "github_connections": False,
        "scoring_v2": False,
        "agent": False,
        "agent_integrations": False,
    }
    assert client.get("/setup").status_code == 404
    assert client.get("/connections").status_code == 404
    assert client.get("/agent").status_code == 404
    assert client.get("/providers").status_code == 404

    enabled = client.post(
        "/settings/features",
        data={
            "setup_wizard": "1",
            "github_connections": "1",
            "scoring_v2": "1",
            "agent": "1",
            "agent_integrations": "1",
        },
        follow_redirects=False,
    )
    assert enabled.status_code == 303
    assert all(cfg.features.to_dict().values())
    assert client.get("/setup").status_code == 200
    assert client.get("/connections").status_code == 200


def test_tailoring_mode_config_and_env(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.yml"
    config_path.write_text("tailoring:\n  mode: llm\n", encoding="utf-8")
    assert load_config(config_path).tailoring.mode == "llm"

    config_path.write_text("tailoring:\n  mode: nonsense\n", encoding="utf-8")
    assert load_config(config_path).tailoring.mode == "auto"

    monkeypatch.setenv("JOBFORGE_TAILORING_MODE", "deterministic")
    assert load_config(config_path).tailoring.mode == "deterministic"
