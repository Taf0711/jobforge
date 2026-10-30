"""Host discovery and installation for the JobForge Agent Skill/MCP surface.

This module is deliberately separate from the dashboard and the built-in agent.
It answers one question: which local agents can be given the JobForge MCP
server and skill, what exactly will change, and did the change work?

Nothing here configures a model provider and nothing here submits an
application. The installer is a local setup surface, not a second runtime.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import yaml

from .. import db
from ..config import Config
from ..models import Preferences, Profile
from ..workbench.contracts import KNOWN_SCOPES

SERVER_NAME = "jobforge"
HOST_ORDER = ("claude_code", "codex", "pi", "hermes", "omp", "generic")

# The connection is read-mostly: proposals create pending requests, not domain
# writes. Submission is never a scope.
DEFAULT_SCOPES = (
    "jobs:read",
    "matches:read",
    "profile:resume_context",
    "applications:summary",
    "memory:read_write",
    "proposals:create",
)

_OPTIONAL_BROWSER = "browser"


class InstallerError(RuntimeError):
    """A typed installer failure with a safe code for the TUI."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


@dataclass(frozen=True)
class HostSpec:
    id: str
    label: str
    config_path: Path
    skill_dir: Path
    config_format: str
    mcp_key: str
    cli: str | None = None
    manual: bool = False
    notes: str = ""


def host_specs(home: str | Path | None = None) -> tuple[HostSpec, ...]:
    root = Path(home).expanduser() if home is not None else Path.home()
    return (
        HostSpec(
            "claude_code",
            "Claude Code",
            root / ".claude.json",
            root / ".claude" / "skills",
            "json",
            "mcpServers",
            "claude",
            notes="Uses the official `claude mcp add --scope user` command.",
        ),
        HostSpec(
            "codex",
            "Codex",
            root / ".codex" / "config.toml",
            root / ".codex" / "skills",
            "toml",
            "mcp_servers",
            "codex",
            notes="Uses `codex mcp add`; the CLI is required for safe TOML merging.",
        ),
        HostSpec(
            "pi",
            "Pi",
            root / ".pi" / "agent" / "mcp.json",
            root / ".pi" / "agent" / "skills",
            "json",
            "mcpServers",
            "pi",
            notes="Uses `pi mcp add` and reads the global ~/.pi/agent/mcp.json.",
        ),
        HostSpec(
            "hermes",
            "Hermes",
            root / ".hermes" / "config.yaml",
            root / ".hermes" / "skills",
            "yaml",
            "mcp_servers",
            "hermes",
            notes="Uses `hermes mcp add`; the CLI is required for safe YAML merging.",
        ),
        HostSpec(
            "omp",
            "OMP",
            root / ".omp" / "agent" / "mcp.json",
            root / ".agents" / "skills",
            "json",
            "mcpServers",
            None,
            notes="OMP reads a JSON mcp.json; JobForge merges that file directly.",
        ),
        HostSpec(
            "generic",
            "Generic MCP",
            root / ".config" / "jobforge" / "mcp.json",
            root / ".agents" / "skills",
            "json",
            "mcpServers",
            None,
            manual=True,
            notes="Writes a portable mcpServers JSON file for any other host.",
        ),
    )


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _backup_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.jobforge.bak.{_timestamp()}")


def _safe_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def _read_configuration(spec: HostSpec) -> dict[str, Any]:
    path = spec.config_path
    if not _safe_exists(path):
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InstallerError("CONFIG_UNREADABLE", f"Could not read {path}: {exc}") from exc
    if not raw.strip():
        return {}
    try:
        if spec.config_format == "json":
            data = json.loads(raw)
        elif spec.config_format == "toml":
            data = tomllib.loads(raw)
        elif spec.config_format == "yaml":
            data = yaml.safe_load(raw) or {}
        else:  # pragma: no cover - defensive
            raise InstallerError("UNSUPPORTED_CONFIG", f"Unsupported config format: {spec.config_format}")
    except InstallerError:
        raise
    except Exception as exc:
        raise InstallerError("CONFIG_INVALID", f"Could not parse {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise InstallerError("CONFIG_INVALID", f"{path} does not contain a mapping at the top level.")
    return data


def _server_entry(spec: HostSpec, data: dict[str, Any]) -> Any:
    servers = data.get(spec.mcp_key) or {}
    return servers.get(SERVER_NAME) if isinstance(servers, dict) else None


def _has_entry(spec: HostSpec, data: dict[str, Any]) -> bool:
    return _server_entry(spec, data) is not None


def _host_cli(spec: HostSpec) -> str | None:
    if not spec.cli:
        return None
    return shutil.which(spec.cli)


def _add_command(spec: HostSpec, server_command: Sequence[str]) -> list[str] | None:
    if spec.id == "claude_code":
        return ["claude", "mcp", "add", "--scope", "user", SERVER_NAME, "--", *server_command]
    if spec.id == "codex":
        return ["codex", "mcp", "add", SERVER_NAME, "--", *server_command]
    if spec.id == "pi":
        return ["pi", "mcp", "add", SERVER_NAME, "--", *server_command]
    if spec.id == "hermes":
        return [
            "hermes",
            "mcp",
            "add",
            SERVER_NAME,
            "--command",
            server_command[0],
            "--args",
            *server_command[1:],
        ]
    return None


def _remove_command(spec: HostSpec) -> list[str] | None:
    if spec.id == "claude_code":
        return ["claude", "mcp", "remove", "--scope", "user", SERVER_NAME]
    if spec.id == "codex":
        return ["codex", "mcp", "remove", SERVER_NAME]
    if spec.id == "pi":
        return ["pi", "mcp", "remove", SERVER_NAME]
    if spec.id == "hermes":
        return ["hermes", "mcp", "remove", SERVER_NAME]
    return None


def _run(command: Sequence[str], *, timeout: int = 90) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise InstallerError("COMMAND_NOT_FOUND", f"Command not found: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise InstallerError("COMMAND_TIMEOUT", f"Command timed out: {' '.join(command)}") from exc


def _require_profile(
    conn: Any, profile_id: int | None
) -> tuple[Profile, Preferences]:
    profile = db.get_profile(conn, profile_id) if profile_id is not None else db.get_active_profile(conn)
    if profile is None or profile.id is None:
        raise InstallerError(
            "SETUP_REQUIRED",
            "JobForge has no profile yet. Run `jobforge setup` and import a resume first.",
        )
    return profile, db.get_preferences(conn, profile.id)


def _find_connection(conn: Any, profile_id: int, host_id: str) -> dict[str, Any] | None:
    for connection in db.list_agent_connections(conn):
        if (
            int(connection.get("profile_id") or 0) == profile_id
            and str(connection.get("client") or "") == host_id
            and connection.get("enabled")
            and not connection.get("revoked_at")
        ):
            return connection
    return None


def _connection_command(config: Config, connection: dict[str, Any]) -> list[str]:
    command = [sys.executable, "-m", "jobforge"]
    if config.config_path:
        command.extend(["--config", str(Path(config.config_path).resolve())])
    command.extend(
        [
            "mcp",
            "serve",
            "--connection",
            str(connection["id"]),
            "--instance",
            str(connection["instance_id"]),
        ]
    )
    return command


def _browser_ready(home: Path) -> tuple[bool, str]:
    for name in ("google-chrome", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return True, found
    candidates = [
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        home / "Library" / "Caches" / "ms-playwright",
        home / ".cache" / "ms-playwright",
        Path(os.environ.get("LOCALAPPDATA", str(home))) / "ms-playwright",
    ]
    for candidate in candidates:
        try:
            if candidate.is_file():
                return True, str(candidate)
            if candidate.is_dir() and any(candidate.glob("chromium-*")):
                return True, str(candidate)
        except OSError:
            continue
    return False, ""


def _optional_components(home: Path) -> list[dict[str, Any]]:
    browser_ready, browser_source = _browser_ready(home)
    return [
        {
            "id": _OPTIONAL_BROWSER,
            "label": "Chromium for application filling",
            "required": False,
            "installed": browser_ready,
            "source": browser_source,
            "consent_required": True,
            "command": [sys.executable, "-m", "playwright", "install", "chromium"],
            "note": (
                "Only needed to open and fill application forms. JobForge can "
                "scan, score, tailor, and review without it. The download is "
                "roughly 150 MB and is never installed without consent."
            ),
        }
    ]


def detect_hosts(home: str | Path | None = None) -> list[dict[str, Any]]:
    """Return every supported host with detection and install state."""
    root = Path(home).expanduser() if home is not None else Path.home()
    out: list[dict[str, Any]] = []
    for spec in host_specs(root):
        cli_path = _host_cli(spec)
        config_exists = _safe_exists(spec.config_path)
        skill_path = spec.skill_dir / SERVER_NAME
        detected = bool(cli_path or config_exists)
        if spec.id == "generic":
            detected = True
        entry_present = False
        if config_exists:
            try:
                entry_present = _has_entry(spec, _read_configuration(spec))
            except InstallerError:
                entry_present = False
        out.append(
            {
                "id": spec.id,
                "label": spec.label,
                "detected": detected,
                "manual": spec.manual,
                "cli": cli_path or "",
                "config_path": str(spec.config_path),
                "config_exists": config_exists,
                "config_entry_present": entry_present,
                "skill_dir": str(spec.skill_dir),
                "skill_installed": _safe_exists(skill_path / "SKILL.md"),
                "notes": spec.notes,
            }
        )
    return out


def _plan_host(
    conn: Any,
    profile_id: int,
    spec: HostSpec,
) -> dict[str, Any]:
    connection = _find_connection(conn, profile_id, spec.id)
    try:
        config = _read_configuration(spec)
        entry_present = _has_entry(spec, config)
    except InstallerError:
        entry_present = False
    cli_path = _host_cli(spec)
    install_method = "host-cli" if (cli_path and _add_command(spec, ["python"])) else "json"
    if spec.id in {"codex", "hermes"} and not cli_path:
        install_method = "unavailable"
    return {
        "id": spec.id,
        "label": spec.label,
        "detected": bool(cli_path or _safe_exists(spec.config_path) or spec.id == "generic"),
        "manual": spec.manual,
        "config_path": str(spec.config_path),
        "config_exists": _safe_exists(spec.config_path),
        "config_entry_present": entry_present,
        "skill_dir": str(spec.skill_dir),
        "skill_installed": _safe_exists(spec.skill_dir / SERVER_NAME / "SKILL.md"),
        "host_cli": cli_path or "",
        "install_method": install_method,
        "connection": "existing" if connection else "new",
        "connection_id": str(connection["id"]) if connection else "",
        "backup": str(_backup_path(spec.config_path)) if _safe_exists(spec.config_path) else "",
        "notes": spec.notes,
    }


def build_plan(
    config: Config,
    conn: Any,
    host_ids: Iterable[str],
    *,
    home: str | Path | None = None,
    profile_id: int | None = None,
    install_browser: bool = False,
) -> dict[str, Any]:
    """Build a reviewable, side-effect-free install plan."""
    root = Path(home).expanduser() if home is not None else Path.home()
    selected_ids = [str(item) for item in host_ids]
    specs = {spec.id: spec for spec in host_specs(root)}
    unknown = [item for item in selected_ids if item not in specs]
    if unknown:
        raise InstallerError("UNKNOWN_HOST", f"Unknown host(s): {', '.join(unknown)}")
    if not selected_ids:
        raise InstallerError("NO_HOST", "Select at least one host.")
    profile, _prefs = _require_profile(conn, profile_id)
    hosts = [_plan_host(conn, int(profile.id or 0), specs[item]) for item in selected_ids]
    optional = _optional_components(root)
    browser = next(item for item in optional if item["id"] == _OPTIONAL_BROWSER)
    warnings: list[str] = []
    for host in hosts:
        if host["install_method"] == "unavailable":
            warnings.append(
                f"{host['label']}: the host CLI is not available, so the installer "
                "cannot safely merge its config. Install or upgrade the host CLI and retry."
            )
    return {
        "plan_version": 1,
        "ok": True,
        "profile": {"id": int(profile.id or 0), "label": profile.name, "name": profile.full_name},
        "hosts": hosts,
        "selected_hosts": selected_ids,
        "scopes": list(DEFAULT_SCOPES),
        "optional": optional,
        "install_browser": bool(install_browser and not browser["installed"]),
        "warnings": warnings,
        "next": [
            "Restart the selected host after applying.",
            "Ask the host agent to use JobForge.",
        ],
    }


def _ensure_connection(
    config: Config, conn: Any, profile: Profile, spec: HostSpec
) -> tuple[dict[str, Any], str]:
    profile_id = int(profile.id or 0)
    existing = _find_connection(conn, profile_id, spec.id)
    if existing is not None:
        missing = sorted(set(DEFAULT_SCOPES) - set(existing.get("scopes") or []))
        if missing:
            existing = db.update_agent_connection_scopes(conn, str(existing["id"]), missing) or existing
        return existing, "existing"
    instances = db.list_agent_instances(conn)
    instance = instances[0] if instances else db.create_agent_instance(conn, "local")
    connection = db.create_agent_connection(
        conn,
        instance_id=str(instance["id"]),
        profile_id=profile_id,
        client=spec.id,
        label=f"{spec.label} · {profile.name}",
        scopes=list(DEFAULT_SCOPES),
    )
    return connection, "created"


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        tmp.unlink(missing_ok=True)


def _backup_file(path: Path) -> str:
    if not _safe_exists(path):
        return ""
    backup = _backup_path(path)
    shutil.copy2(path, backup)
    return str(backup)


def _export_skill(spec: HostSpec) -> tuple[str, str]:
    source = Path(__file__).resolve().parent.parent / "data" / "skills" / "jobforge"
    if not source.is_dir():
        raise InstallerError("SKILL_MISSING", "The packaged JobForge skill is missing from this install.")
    destination = spec.skill_dir / SERVER_NAME
    skill_backup = ""
    if destination.exists():
        backup = destination.with_name(f"{SERVER_NAME}.jobforge.bak")
        if backup.exists():
            shutil.rmtree(backup)
        shutil.copytree(destination, backup)
        skill_backup = str(backup)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, dirs_exist_ok=True)
    return str(destination), skill_backup


def _configure_host(spec: HostSpec, server_command: Sequence[str]) -> str:
    cli_path = _host_cli(spec)
    add = _add_command(spec, server_command)
    if cli_path and add:
        remove = _remove_command(spec)
        if remove:
            _run(remove)
        result = _run(add)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown error").strip()[:500]
            raise InstallerError("HOST_CLI_FAILED", f"{spec.label} rejected the MCP entry: {detail}")
        return "host-cli"
    if spec.config_format == "json":
        data = _read_configuration(spec)
        servers = data.setdefault(spec.mcp_key, {})
        if not isinstance(servers, dict):
            raise InstallerError("CONFIG_INVALID", f"{spec.config_path} has a non-object {spec.mcp_key}.")
        servers[SERVER_NAME] = {"command": server_command[0], "args": list(server_command[1:])}
        _write_json_atomic(spec.config_path, data)
        return "json"
    raise InstallerError(
        "HOST_CLI_REQUIRED",
        f"{spec.label} needs its CLI to merge {spec.config_path} safely.",
    )


def _install_browser(component: dict[str, Any]) -> dict[str, Any]:
    if component["installed"]:
        return {"id": _OPTIONAL_BROWSER, "ok": True, "installed": True, "skipped": True}
    result = _run(component["command"])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()[:500]
        return {
            "id": _OPTIONAL_BROWSER,
            "ok": False,
            "installed": False,
            "error": {"code": "BROWSER_INSTALL_FAILED", "message": detail},
        }
    return {"id": _OPTIONAL_BROWSER, "ok": True, "installed": True, "skipped": False}


def apply_plan(
    config: Config,
    conn: Any,
    host_ids: Iterable[str],
    *,
    home: str | Path | None = None,
    profile_id: int | None = None,
    install_browser: bool = False,
) -> dict[str, Any]:
    """Apply the install plan. The caller must have shown the plan to a human."""
    root = Path(home).expanduser() if home is not None else Path.home()
    plan = build_plan(
        config,
        conn,
        host_ids,
        home=root,
        profile_id=profile_id,
        install_browser=install_browser,
    )
    profile, _prefs = _require_profile(conn, plan["profile"]["id"])
    specs = {spec.id: spec for spec in host_specs(root)}
    results: list[dict[str, Any]] = []
    for host in plan["hosts"]:
        spec = specs[host["id"]]
        if host["install_method"] == "unavailable":
            results.append(
                {
                    "id": spec.id,
                    "label": spec.label,
                    "ok": False,
                    "error": {
                        "code": "HOST_CLI_REQUIRED",
                        "message": (
                            f"{spec.label} needs its CLI to merge {spec.config_path} safely. "
                            "Install or upgrade the host CLI and retry."
                        ),
                    },
                }
            )
            continue
        try:
            connection, connection_state = _ensure_connection(config, conn, profile, spec)
            backup = _backup_file(spec.config_path)
            skill_path, skill_backup = _export_skill(spec)
            method = _configure_host(spec, _connection_command(config, connection))
            if not _has_entry(spec, _read_configuration(spec)):
                raise InstallerError(
                    "CONFIG_NOT_VERIFIED",
                    f"{spec.label} was configured but JobForge could not verify the entry in {spec.config_path}.",
                )
            results.append(
                {
                    "id": spec.id,
                    "label": spec.label,
                    "ok": True,
                    "connection_id": str(connection["id"]),
                    "connection": connection_state,
                    "config_path": str(spec.config_path),
                    "backup": backup,
                    "skill_path": skill_path,
                    "skill_backup": skill_backup,
                    "method": method,
                }
            )
        except InstallerError as exc:
            results.append(
                {
                    "id": spec.id,
                    "label": spec.label,
                    "ok": False,
                    "error": exc.as_dict(),
                }
            )
        except Exception as exc:  # noqa: BLE001 - installer boundary
            results.append(
                {
                    "id": spec.id,
                    "label": spec.label,
                    "ok": False,
                    "error": {"code": "INSTALL_FAILED", "message": str(exc)[:500]},
                }
            )
    optional_results: list[dict[str, Any]] = []
    browser = next(item for item in plan["optional"] if item["id"] == _OPTIONAL_BROWSER)
    if install_browser:
        try:
            optional_results.append(_install_browser(browser))
        except InstallerError as exc:
            optional_results.append(
                {"id": _OPTIONAL_BROWSER, "ok": False, "installed": False, "error": exc.as_dict()}
            )
    return {
        "ok": all(item["ok"] for item in results)
        and all(item.get("ok", True) for item in optional_results),
        "profile": plan["profile"],
        "hosts": results,
        "optional": optional_results,
        "next": [
            "Restart the selected host(s) if the JobForge tools do not appear.",
            "Ask the host agent to use JobForge.",
        ],
    }


def doctor(
    config: Config,
    conn: Any,
    host_ids: Iterable[str],
    *,
    home: str | Path | None = None,
    profile_id: int | None = None,
) -> dict[str, Any]:
    """Check the installed hosts, skill copies, connections, and runtime."""
    root = Path(home).expanduser() if home is not None else Path.home()
    specs = {spec.id: spec for spec in host_specs(root)}
    selected = [str(item) for item in host_ids] or [spec.id for spec in specs.values() if spec.id != "generic"]
    unknown = [item for item in selected if item not in specs]
    if unknown:
        raise InstallerError("UNKNOWN_HOST", f"Unknown host(s): {', '.join(unknown)}")
    profile, _prefs = _require_profile(conn, profile_id)
    hosts: list[dict[str, Any]] = []
    all_ok = True
    for host_id in selected:
        spec = specs[host_id]
        connection = _find_connection(conn, int(profile.id or 0), spec.id)
        try:
            config_present = _has_entry(spec, _read_configuration(spec))
        except InstallerError:
            config_present = False
        skill_present = _safe_exists(spec.skill_dir / SERVER_NAME / "SKILL.md")
        ok = bool(connection and config_present and skill_present)
        all_ok = all_ok and ok
        hosts.append(
            {
                "id": spec.id,
                "label": spec.label,
                "ok": ok,
                "detected": bool(_host_cli(spec) or _safe_exists(spec.config_path) or spec.id == "generic"),
                "connection": "enabled" if connection else "missing",
                "connection_id": str(connection["id"]) if connection else "",
                "config_entry": config_present,
                "config_path": str(spec.config_path),
                "skill_installed": skill_present,
                "skill_path": str(spec.skill_dir / SERVER_NAME),
            }
        )
    browser_ready, browser_source = _browser_ready(root)
    typst = shutil.which("typst")
    issues: list[str] = []
    for host in hosts:
        if not host["connection"] == "enabled":
            issues.append(f"{host['label']}: no enabled JobForge connection.")
        if not host["config_entry"]:
            issues.append(f"{host['label']}: MCP entry missing from {host['config_path']}.")
        if not host["skill_installed"]:
            issues.append(f"{host['label']}: skill missing from {host['skill_path']}.")
    return {
        "ok": all_ok,
        "profile": {"id": int(profile.id or 0), "label": profile.name},
        "runtime": {
            "python": sys.version.split()[0],
            "typst": typst or "",
            "browser": browser_ready,
            "browser_source": browser_source,
        },
        "hosts": hosts,
        "issues": issues,
        "next": (
            ["Run `npx jobforge` and apply the install plan."]
            if issues
            else ["Restart the host if its JobForge tools are not visible."]
        ),
    }
