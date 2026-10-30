from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from jobforge import db
from jobforge.config import BrowserConfig, Config, EmailConfig, LLMConfig, ServerConfig
from jobforge.installer import (
    InstallerError,
    apply_plan,
    build_plan,
    detect_hosts,
    doctor,
)
from jobforge.installer import core
from jobforge.models import Preferences, Profile


def _config(tmp_path: Path) -> tuple[Config, sqlite3.Connection, int]:
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
    pid = db.save_profile(
        conn,
        Profile(
            name="installer-test",
            first_name="Installer",
            last_name="Test",
            email="installer@example.com",
        ),
    )
    db.set_active_profile(conn, pid)
    db.save_preferences(conn, pid, Preferences())
    return cfg, conn, pid


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / ".pi" / "agent").mkdir(parents=True)
    (home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"existing": {"command": "old"}}}, indent=2) + "\n",
        encoding="utf-8",
    )
    (home / ".pi" / "agent" / "mcp.json").write_text(
        json.dumps({"mcpServers": {}}, indent=2) + "\n",
        encoding="utf-8",
    )
    return home


def _no_host_cli(monkeypatch) -> None:
    real_which = core.shutil.which
    host_names = {"claude", "codex", "pi", "hermes", "omp", "google-chrome", "chromium", "chrome"}

    def fake_which(name: str) -> str | None:
        if name in host_names:
            return None
        return real_which(name)

    monkeypatch.setattr(core.shutil, "which", fake_which)
    monkeypatch.setattr(core, "_browser_ready", lambda home: (False, ""))


def test_host_detection_and_plan_are_reviewable(tmp_path: Path, monkeypatch):
    cfg, conn, pid = _config(tmp_path)
    home = _home(tmp_path)
    _no_host_cli(monkeypatch)

    hosts = {item["id"]: item for item in detect_hosts(home)}
    assert hosts["claude_code"]["detected"] is True
    assert hosts["pi"]["detected"] is True
    assert hosts["generic"]["detected"] is True

    plan = build_plan(cfg, conn, ["claude_code", "pi", "omp"], home=home)
    conn.close()

    assert plan["ok"] is True
    assert plan["profile"]["id"] == pid
    assert [item["id"] for item in plan["hosts"]] == ["claude_code", "pi", "omp"]
    assert all(item["install_method"] == "json" for item in plan["hosts"])
    assert plan["scopes"] == list(core.DEFAULT_SCOPES)
    assert plan["optional"][0]["id"] == "browser"
    assert plan["optional"][0]["installed"] is False
    assert plan["optional"][0]["consent_required"] is True
    assert "api_key" not in json.dumps(plan).lower()
    assert plan["hosts"][0]["backup"].startswith(str(home / ".claude.json.jobforge.bak."))


def test_apply_writes_configs_skills_and_backups(tmp_path: Path, monkeypatch):
    cfg, conn, pid = _config(tmp_path)
    home = _home(tmp_path)
    _no_host_cli(monkeypatch)

    result = apply_plan(cfg, conn, ["claude_code", "pi", "omp"], home=home)

    assert result["ok"] is True
    claude = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    assert claude["mcpServers"]["existing"]["command"] == "old"
    assert claude["mcpServers"]["jobforge"]["command"]
    assert claude["mcpServers"]["jobforge"]["args"][:2] == ["-m", "jobforge"]
    assert "mcp" in claude["mcpServers"]["jobforge"]["args"]
    pi = json.loads((home / ".pi" / "agent" / "mcp.json").read_text(encoding="utf-8"))
    assert pi["mcpServers"]["jobforge"]["command"]
    omp = json.loads((home / ".omp" / "agent" / "mcp.json").read_text(encoding="utf-8"))
    assert omp["mcpServers"]["jobforge"]["command"]

    assert list(home.glob(".claude.json.jobforge.bak.*"))
    assert list((home / ".pi" / "agent").glob("mcp.json.jobforge.bak.*"))
    assert (home / ".claude" / "skills" / "jobforge" / "SKILL.md").is_file()
    assert (home / ".pi" / "agent" / "skills" / "jobforge" / "SKILL.md").is_file()
    assert (home / ".agents" / "skills" / "jobforge" / "SKILL.md").is_file()

    connections = db.list_agent_connections(conn)
    assert {item["client"] for item in connections} == {"claude_code", "pi", "omp"}
    assert all(item["profile_id"] == pid for item in connections)
    assert all(item["enabled"] for item in connections)

    report = doctor(cfg, conn, ["claude_code", "pi", "omp"], home=home)
    conn.close()

    assert report["ok"] is True
    assert "typst" in report["runtime"]
    assert report["issues"] == []


def test_apply_is_idempotent_and_does_not_duplicate_connections(tmp_path: Path, monkeypatch):
    cfg, conn, _pid = _config(tmp_path)
    home = _home(tmp_path)
    _no_host_cli(monkeypatch)

    first = apply_plan(cfg, conn, ["claude_code", "pi"], home=home)
    second = apply_plan(cfg, conn, ["claude_code", "pi"], home=home)
    connections = db.list_agent_connections(conn)
    conn.close()

    assert first["ok"] is True
    assert second["ok"] is True
    assert len(connections) == 2
    assert {item["client"] for item in connections} == {"claude_code", "pi"}
    assert all(item["connection"] == "existing" for item in second["hosts"])
    assert (home / ".claude" / "skills" / "jobforge.jobforge.bak" / "SKILL.md").is_file()


def test_missing_codex_cli_is_reported_without_partial_install(tmp_path: Path, monkeypatch):
    cfg, conn, _pid = _config(tmp_path)
    home = _home(tmp_path)
    _no_host_cli(monkeypatch)

    plan = build_plan(cfg, conn, ["codex"], home=home)
    assert plan["hosts"][0]["install_method"] == "unavailable"
    assert plan["warnings"]

    result = apply_plan(cfg, conn, ["codex"], home=home)
    conn.close()

    assert result["ok"] is False
    assert result["hosts"][0]["error"]["code"] == "HOST_CLI_REQUIRED"
    assert not (home / ".codex" / "skills" / "jobforge").exists()


def test_optional_browser_requires_explicit_consent(tmp_path: Path, monkeypatch):
    cfg, conn, _pid = _config(tmp_path)
    home = _home(tmp_path)
    _no_host_cli(monkeypatch)
    called: list[str] = []

    def fake_browser(component):
        called.append(str(component["id"]))
        return {"id": "browser", "ok": True, "installed": True}

    monkeypatch.setattr(core, "_install_browser", fake_browser)

    apply_plan(cfg, conn, ["claude_code"], home=home)
    assert called == []

    result = apply_plan(cfg, conn, ["claude_code"], home=home, install_browser=True)
    conn.close()

    assert called == ["browser"]
    assert result["ok"] is True
    assert result["optional"][0]["id"] == "browser"


def test_unknown_host_is_rejected(tmp_path: Path):
    cfg, conn, _pid = _config(tmp_path)
    with pytest.raises(InstallerError) as excinfo:
        build_plan(cfg, conn, ["nope"], home=tmp_path / "home")
    conn.close()
    assert excinfo.value.code == "UNKNOWN_HOST"
