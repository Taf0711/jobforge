from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest
import yaml

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
    legacy = home / ".claude" / "skills" / "jobforge.jobforge.bak"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text("old backup", encoding="utf-8")
    second = apply_plan(cfg, conn, ["claude_code", "pi"], home=home)
    connections = db.list_agent_connections(conn)
    conn.close()

    assert first["ok"] is True
    assert second["ok"] is True
    assert len(connections) == 2
    assert {item["client"] for item in connections} == {"claude_code", "pi"}
    assert all(item["connection"] == "existing" for item in second["hosts"])
    assert (home / ".claude" / "skills.jobforge.bak" / "jobforge" / "SKILL.md").is_file()
    assert not (home / ".claude" / "skills" / "jobforge.jobforge.bak").exists()


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


def test_hermes_cli_prompt_is_answered_noninteractively(tmp_path: Path, monkeypatch):
    cfg, conn, _pid = _config(tmp_path)
    home = _home(tmp_path)
    hermes_dir = home / ".hermes"
    hermes_dir.mkdir(parents=True)
    config_path = hermes_dir / "config.yaml"
    config_path.write_text(
        "mcp_servers:\n  existing:\n    command: old\n    enabled: true\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_hermes = bin_dir / "hermes"
    fake_hermes.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "from pathlib import Path\n"
        "import yaml\n"
        f"config = Path({str(config_path)!r})\n"
        "data = yaml.safe_load(config.read_text()) or {}\n"
        "args = sys.argv[1:]\n"
        "servers = data.setdefault('mcp_servers', {})\n"
        "if args[:2] == ['mcp', 'remove']:\n"
        "    servers.pop('jobforge', None)\n"
        "    config.write_text(yaml.safe_dump(data, sort_keys=False))\n"
        "    raise SystemExit(0)\n"
        "if args[:2] == ['mcp', 'add']:\n"
        "    answer = sys.stdin.readline().strip().lower()\n"
        "    if answer not in {'y', 'yes', ''}:\n"
        "        print('Cancelled.')\n"
        "        raise SystemExit(0)\n"
        "    name = args[2]\n"
        "    command = args[args.index('--command') + 1]\n"
        "    tail = args[args.index('--args') + 1:]\n"
        "    servers[name] = {'command': command, 'args': tail, 'enabled': True}\n"
        "    config.write_text(yaml.safe_dump(data, sort_keys=False))\n"
        "    print('Saved')\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    fake_hermes.chmod(0o755)
    real_which = core.shutil.which

    def fake_which(name: str) -> str | None:
        if name == "hermes":
            return str(fake_hermes)
        return real_which(name)

    monkeypatch.setattr(core.shutil, "which", fake_which)
    monkeypatch.setattr(core, "_browser_ready", lambda home: (False, ""))

    result = apply_plan(cfg, conn, ["hermes"], home=home)
    written = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    conn.close()

    assert result["ok"] is True, result
    assert written["mcp_servers"]["jobforge"]["enabled"] is True
    assert written["mcp_servers"]["jobforge"]["command"]
    assert written["mcp_servers"]["existing"]["command"] == "old"
    assert result["hosts"][0]["method"] == "host-cli"


def test_unknown_host_is_rejected(tmp_path: Path):
    cfg, conn, _pid = _config(tmp_path)
    with pytest.raises(InstallerError) as excinfo:
        build_plan(cfg, conn, ["nope"], home=tmp_path / "home")
    conn.close()
    assert excinfo.value.code == "UNKNOWN_HOST"
