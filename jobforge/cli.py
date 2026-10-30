from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

import yaml

from . import db, quality, services
from .config import load_config
from .email.code_bridge import redact_code, redact_code_in_text
from .models import RunStatus


def _connect(args):
    cfg = load_config(args.config)
    db.init_db(cfg.db_path)
    return cfg, db.connect(cfg.db_path)


def _print(value) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def _package_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("jobforge")
    except PackageNotFoundError:
        return "0.1.0-dev"


def _playwright_browsers() -> list[str]:
    candidates = [
        Path.home() / "Library" / "Caches" / "ms-playwright",
        Path.home() / ".cache" / "ms-playwright",
        Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ms-playwright",
    ]
    found: list[str] = []
    for root in candidates:
        if root.is_dir():
            found.extend(sorted(p.name for p in root.glob("chromium-*")))
    return found


def _system_chrome() -> str:
    candidates = [
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        Path("/usr/bin/google-chrome"),
        Path("/usr/bin/chromium"),
        Path(os.environ.get("PROGRAMFILES", "C:\\Program Files")) / "Google/Chrome/Application/chrome.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return ""


def _doctor_report(cfg) -> dict:
    from .agent.providers import infer_provider
    from .paths import app_home, base_dir, source_root

    typst = shutil.which("typst")
    return {
        "jobforge": _package_version(),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "source_checkout": str(source_root()) if source_root() else None,
        "base_dir": str(base_dir()),
        "app_home": str(app_home()),
        "config": str(cfg.config_path) if cfg.config_path else "(defaults)",
        "data_dir": str(cfg.data_dir),
        "output_dir": str(cfg.output_dir),
        "database": str(cfg.db_path),
        "data_writable": os.access(cfg.data_dir, os.W_OK),
        "typst": typst or "",
        "chrome": _system_chrome(),
        "playwright_browsers": _playwright_browsers(),
        "browser_channel": cfg.browser.channel or "bundled",
        "llm_enabled": cfg.llm.enabled,
        "llm_provider": infer_provider(cfg.llm),
        "llm_model": cfg.llm.model,
    }


def _health_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/healthz", timeout=0.6) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return payload.get("status") == "ok"
    except Exception:
        return False


def _wait_for_health(url: str, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _health_ok(url):
            return True
        time.sleep(0.25)
    return False


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _tool_exit_code(payload: dict) -> int:
    if payload.get("ok"):
        return 0
    code = str((payload.get("error") or {}).get("code") or "")
    if code == "INVALID_ARGUMENT":
        return 2
    if code in {"SETUP_REQUIRED", "MIGRATION_REQUIRED", "CONNECTION_DISABLED", "SCOPE_DENIED"}:
        return 3
    if code in {"NOT_FOUND", "STALE_CONTEXT", "QUALITY_BLOCKED", "CONTEXT_TOO_LARGE"}:
        return 4
    return 5


def cmd_integrations(args) -> int:
    from .locking import action_lock
    from .workbench.authorization import load_query_context
    from .workbench.contracts import KNOWN_SCOPES, ToolError, envelope, error_envelope, request_id
    from .workbench.queries import call_tool

    cfg, conn = _connect(args)
    try:
        if args.integrations_action == "list":
            _print(
                {
                    "instances": db.list_agent_instances(conn),
                    "connections": db.list_agent_connections(conn),
                }
            )
            return 0
        if args.integrations_action == "setup":
            profile = db.get_profile(conn, int(args.profile))
            if profile is None:
                print("Profile not found.", file=sys.stderr)
                return 2
            requested = {item.strip() for item in (args.scopes or "").split(",") if item.strip()}
            unknown = sorted(requested - KNOWN_SCOPES)
            if unknown:
                print(f"Unknown scope(s): {', '.join(unknown)}", file=sys.stderr)
                return 2
            scopes = sorted({"jobs:read", "matches:read"} | requested)
            with action_lock(cfg):
                instance = db.get_agent_instance(conn, args.instance) if args.instance else None
                if args.instance and instance is None:
                    print("Instance not found.", file=sys.stderr)
                    return 2
                if instance is None:
                    existing = db.list_agent_instances(conn)
                    instance = existing[0] if existing else db.create_agent_instance(conn, "local")
                connection = db.create_agent_connection(
                    conn,
                    instance_id=str(instance["id"]),
                    profile_id=int(profile.id or 0),
                    client=args.client,
                    label=args.label or f"{args.client} · {profile.name}",
                    scopes=scopes,
                )
            _print(
                {
                    "ok": True,
                    "connection": connection,
                    "instance": instance,
                    "next": "jobforge tools call jobforge_status --connection "
                    + str(connection["id"]),
                }
            )
            return 0
        if args.integrations_action == "grant":
            requested = {item.strip() for item in (args.scopes or "").split(",") if item.strip()}
            unknown = sorted(requested - KNOWN_SCOPES)
            if not requested:
                print("At least one scope is required.", file=sys.stderr)
                return 2
            if unknown:
                print(f"Unknown scope(s): {', '.join(unknown)}", file=sys.stderr)
                return 2
            with action_lock(cfg):
                connection = db.update_agent_connection_scopes(conn, args.connection, sorted(requested))
            if connection is None:
                print("Connection not found.", file=sys.stderr)
                return 2
            _print({"ok": True, "connection": connection})
            return 0
        if args.integrations_action == "disable":
            with action_lock(cfg):
                connection = db.disable_agent_connection(conn, args.connection)
            if connection is None:
                print("Connection not found.", file=sys.stderr)
                return 2
            _print({"ok": True, "connection": connection})
            return 0
        if args.integrations_action == "config":
            connection = db.get_agent_connection(conn, args.connection)
            if connection is None:
                print("Connection not found.", file=sys.stderr)
                return 2
            command = [sys.executable, "-m", "jobforge"]
            if cfg.config_path:
                command.extend(["--config", str(Path(cfg.config_path).resolve())])
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
            _print(
                {
                    "mcpServers": {
                        "jobforge": {
                            "command": command[0],
                            "args": command[1:],
                        }
                    }
                }
            )
            return 0
        if args.integrations_action == "doctor":
            try:
                context = load_query_context(
                    cfg,
                    conn,
                    connection_id=args.connection,
                    instance_id=args.instance or None,
                )
                payload = call_tool(context, "jobforge_status", {})
            except ToolError as exc:
                payload = error_envelope(exc, request_id_value=request_id())
            _print(payload)
            return _tool_exit_code(payload)
    finally:
        conn.close()
    return 2


def _read_connect(args):
    cfg = load_config(args.config)
    try:
        conn = db.connect_readonly(cfg.db_path)
    except FileNotFoundError:
        conn = None
    return cfg, conn


def cmd_install(args) -> int:
    from .installer import InstallerError, apply_plan, build_plan, detect_hosts, doctor

    if args.install_action == "hosts":
        _print({"ok": True, "hosts": detect_hosts()})
        return 0

    hosts = [item.strip() for item in (args.hosts or "").split(",") if item.strip()]
    if not hosts:
        print("Select at least one host with --hosts.", file=sys.stderr)
        return 2
    cfg, conn = _connect(args)
    try:
        if args.install_action == "plan":
            _print(
                build_plan(
                    cfg,
                    conn,
                    hosts,
                    profile_id=args.profile,
                    install_browser=bool(args.install_browser),
                )
            )
            return 0
        if args.install_action == "doctor":
            result = doctor(cfg, conn, hosts, profile_id=args.profile)
            _print(result)
            return 0 if result["ok"] else 1
        if args.install_action == "apply":
            if not args.yes:
                _print(
                    {
                        "ok": False,
                        "error": {
                            "code": "CONFIRMATION_REQUIRED",
                            "message": "Review `jobforge install plan` first, then re-run with --yes.",
                        },
                    }
                )
                return 2
            result = apply_plan(
                cfg,
                conn,
                hosts,
                profile_id=args.profile,
                install_browser=bool(args.install_browser),
            )
            _print(result)
            return 0 if result["ok"] else 1
    except InstallerError as exc:
        _print({"ok": False, "error": exc.as_dict()})
        return 2
    finally:
        conn.close()
    return 2


def cmd_tools(args) -> int:
    from .workbench.actions import ACTION_TOOLS
    from .workbench.authorization import load_query_context
    from .workbench.contracts import (
        ToolError,
        envelope,
        error_envelope,
        parse_json_strict,
        request_id,
    )
    from .workbench.queries import call_tool, tool_list

    action_tool = args.tools_action == "call" and args.tool_name in ACTION_TOOLS
    if action_tool:
        cfg = load_config(args.config)
        if not cfg.db_path.exists():
            conn = None
        else:
            conn = db.connect(cfg.db_path)
    else:
        cfg, conn = _read_connect(args)
    if conn is None:
        from .workbench.contracts import ToolError, error_envelope, request_id

        payload = error_envelope(
            ToolError("SETUP_REQUIRED", "JobForge has not been initialized; run human setup first."),
            request_id_value=request_id(),
        )
        _print(payload)
        return _tool_exit_code(payload)
    try:
        try:
            context = load_query_context(
                cfg,
                conn,
                connection_id=args.connection,
                instance_id=args.instance or None,
            )
        except ToolError as exc:
            payload = error_envelope(exc, request_id_value=request_id())
            _print(payload)
            return _tool_exit_code(payload)
        if args.tools_action == "list":
            payload = envelope(tool_list(context), request_id_value=request_id())
            _print(payload)
            return 0
        if args.input != "-":
            payload = error_envelope(
                ToolError("INVALID_ARGUMENT", "Tool payloads must be read from stdin (`--input -`)."),
                request_id_value=request_id(),
            )
            _print(payload)
            return _tool_exit_code(payload)
        raw = sys.stdin.read()
        if not raw.strip():
            data = {}
        else:
            try:
                data = parse_json_strict(raw)
            except ToolError as exc:
                payload = error_envelope(exc, request_id_value=request_id())
                _print(payload)
                return _tool_exit_code(payload)
        payload = call_tool(context, args.tool_name, data)
        _print(payload)
        return _tool_exit_code(payload)
    finally:
        conn.close()


def cmd_operation(args) -> int:
    from .workbench.worker import run_operation

    cfg, conn = _connect(args)
    try:
        result = run_operation(cfg, conn, args.operation_id)
        _print(result)
        return 0 if (result.get("state") == "succeeded") else 1
    except Exception as exc:
        print(f"Operation failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def cmd_skills(args) -> int:
    source = Path(__file__).resolve().parent / "data" / "skills" / "jobforge"
    if args.skills_action != "export":
        return 2
    if not source.is_dir():
        print("Packaged JobForge skill is missing.", file=sys.stderr)
        return 5
    destination = Path(args.destination).expanduser().resolve() / "jobforge"
    if destination.exists() and not args.force:
        print(
            f"Destination already exists: {destination}. Re-run with --force to merge.",
            file=sys.stderr,
        )
        return 2
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, dirs_exist_ok=bool(args.force))
    exported = sorted(str(path.relative_to(destination)) for path in destination.rglob("*") if path.is_file())
    _print(
        {
            "ok": True,
            "destination": str(destination),
            "exported": exported,
            "next": "Point the host's Agent Skills directory at the destination, or move the jobforge folder there.",
        }
    )
    return 0


def cmd_mcp(args) -> int:
    try:
        from .integrations.agents.mcp_server import check_connection, create_server
    except ImportError:
        print(
            "The MCP server requires the optional agent extra: "
            "pip install 'jobforge[agent]'",
            file=sys.stderr,
        )
        return 5

    config_path = args.config
    error = check_connection(config_path, args.connection, args.instance or None)
    if error is not None:
        print(f"jobforge mcp: {error.code}: {error.message}", file=sys.stderr)
        return 3
    server = create_server(config_path, args.connection, args.instance or None)
    import asyncio

    try:
        asyncio.run(server.run_stdio_async())
    except KeyboardInterrupt:
        return 0
    return 0


def cmd_setup(args) -> int:
    from .paths import ensure_config

    config_path = ensure_config()
    cfg = load_config(args.config or (str(config_path) if config_path.exists() else None))
    cfg.ensure_dirs()
    db.init_db(cfg.db_path)

    print("JobForge setup")
    print(f"  config: {cfg.config_path or '(defaults)'}")
    print(f"  data:   {cfg.data_dir}")

    if getattr(args, "install_browser", False) and not args.skip_browser:
        print("Installing optional Playwright Chromium...")
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=False,
        )
        if result.returncode != 0:
            print(
                "Chromium install failed. JobForge will fall back to system Chrome if installed.",
                file=sys.stderr,
            )
    else:
        print("Skipping optional Chromium download (needed only for filling applications).")

    report = _doctor_report(cfg)
    _print(report)
    if not report["typst"]:
        print(
            "Typst is not on PATH. Install it before tailoring: "
            "macOS `brew install typst`, Windows `winget install Typst.Typst`, "
            "or see https://typst.app/docs/guides/installation/",
            file=sys.stderr,
        )
        return 1
    if not report["chrome"] and not report["playwright_browsers"]:
        print(
            "No Chromium or Chrome found. Filling applications will be unavailable "
            "until you run `jobforge setup --install-browser` or install Google Chrome.",
            file=sys.stderr,
        )
    return 0


def cmd_doctor(args) -> int:
    cfg = load_config(args.config)
    _print(_doctor_report(cfg))
    return 0 if shutil.which("typst") else 1


def cmd_app(args) -> int:
    from .paths import base_dir, ensure_config

    config_path = ensure_config()
    cfg = load_config(args.config or (str(config_path) if config_path.exists() else None))
    db.init_db(cfg.db_path)
    host = args.host or cfg.server.host
    port = args.port or cfg.server.port
    url = f"http://{host}:{port}"

    if _health_ok(url):
        if not args.no_browser:
            webbrowser.open(url)
        print(f"JobForge is already running at {url}")
        return 0
    if not _port_free(host, port):
        port = _free_port(host)
        url = f"http://{host}:{port}"

    cmd = [sys.executable, "-m", "jobforge"]
    if cfg.config_path:
        cmd.extend(["--config", str(cfg.config_path)])
    cmd.extend(["serve", "--host", host, "--port", str(port)])
    process = subprocess.Popen(cmd, cwd=str(base_dir()))
    if not _wait_for_health(url):
        process.terminate()
        print(f"JobForge failed to start at {url}", file=sys.stderr)
        return 1
    if not args.no_browser:
        webbrowser.open(url)
    print(f"JobForge running at {url}")
    print("Press Ctrl+C to stop.")
    try:
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        return 0


def cmd_init(args) -> int:
    cfg, conn = _connect(args)
    profile, prefs = services.ensure_profile(cfg, conn)
    _print(
        {
            "db": str(cfg.db_path),
            "profile_id": profile.id,
            "profile_name": profile.name,
            "resume": profile.resume_path,
            "skills": len(profile.skills),
            "facts": len(profile.facts),
            "preferences": prefs.to_dict(),
            "next": "Edit config.yml sources, then run: python -m jobforge scan",
        }
    )
    return 0


def cmd_scan(args) -> int:
    cfg, conn = _connect(args)
    if args.board:
        from .sources.registry import get_source

        run_id = db.start_run(conn, "scan")
        try:
            jobs = get_source(args.source).fetch(args.board)
            new = 0
            for job in jobs:
                before = conn.execute("SELECT id FROM jobs WHERE fingerprint=?", (job.fingerprint,)).fetchone()
                db.upsert_job(conn, job)
                new += 1 if before is None else 0
            stats = {"source": args.source, "board": args.board, "fetched": len(jobs), "new": new}
            db.finish_run(conn, run_id, RunStatus.OK.value, stats)
        except Exception as exc:
            db.finish_run(conn, run_id, RunStatus.FAILED.value, {"error": str(exc)})
            print(f"Scan failed: {exc}", file=sys.stderr)
            return 1
    else:
        stats = services.scan(cfg, conn, only=args.source, urls=args.url)
    _print(stats)
    return 0 if not stats.get("errors") else 2


def cmd_score(args) -> int:
    cfg, conn = _connect(args)
    _print(services.rescore(cfg, conn))
    return 0


def cmd_tailor(args) -> int:
    cfg, conn = _connect(args)
    try:
        _print(services.tailor_job(cfg, conn, args.job))
    except Exception as exc:
        print(f"Tailor failed: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_apply(args) -> int:
    cfg, conn = _connect(args)
    job = db.get_job(conn, args.job)
    if job is not None:
        profile, prefs = services.require_profile(cfg, conn)
        report = quality.evaluate(conn, job, profile, prefs, require_prepared=False)
        if not report.ready:
            print(str(quality.QualityBlocked(report)), file=sys.stderr)
            return 1
    try:
        _print(
            services.apply_job(
                cfg,
                conn,
                args.job,
                allow_submit=args.allow_submit,
                review_only=getattr(args, "review_only", False),
                hold_seconds=args.hold,
                resume_path=args.resume,
            )
        )
    except Exception as exc:
        print(f"Apply failed: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_jobs(args) -> int:
    cfg, conn = _connect(args)
    profile, prefs = services.require_profile(cfg, conn)
    rows = db.list_jobs(
        conn,
        profile.id,
        q=args.query,
        min_score=args.min_score,
        one_per_company=prefs.one_application_per_company,
        limit=args.limit,
    )
    for row in rows:
        pursuit = (row.get("meta") or {}).get("pursuit")
        comp = (row.get("meta") or {}).get("comp", {}).get("verdict", "")
        print(
            f"{row['id']:>4}  fit {row.get('score') or 0:>5.1f}  "
            f"pursuit {float(pursuit or 0):>5.1f}  {row['company'][:22]:22}  "
            f"{row['title'][:46]:46}  {row.get('location','')[:20]:20}  {comp}"
        )
    return 0


def cmd_status(args) -> int:
    cfg, conn = _connect(args)
    profile, _ = services.require_profile(cfg, conn)
    _print({"profile": profile.name, **db.stats(conn, profile.id), "runs": db.list_runs(conn, 5)})
    return 0


def cmd_email(args) -> int:
    cfg, conn = _connect(args)
    from .email.imap import poll

    try:
        _print(poll(cfg.email, conn))
    except Exception as exc:
        print(f"Email poll failed: {exc}", file=sys.stderr)
        return 1
    return 0


def _code_payload(result, reveal: bool = False) -> dict:
    payload = {
        "matched": result.matched,
        "redacted": redact_code(result.code),
        "message_id": redact_code_in_text(result.message_id, result.code),
        "sender": redact_code_in_text(result.sender, result.code),
        "subject": redact_code_in_text(result.subject, result.code),
        "received_at": result.received_at,
        "elapsed_s": round(result.elapsed_s, 2),
        "reason": result.reason,
    }
    if reveal and result.matched:
        payload["code"] = result.code
    return payload


def cmd_code(args) -> int:
    if not (args.company or args.sender or args.subject):
        print(
            "Provide --company, --sender, or --subject so the verification message can be matched.",
            file=sys.stderr,
        )
        return 2

    cfg, conn = _connect(args)
    from datetime import datetime, timedelta, timezone

    from .email.code_bridge import CodeRequest, record_bridge_event, wait_for_code

    if args.application_id is not None and db.get_application(conn, args.application_id) is None:
        print(f"Application {args.application_id} not found", file=sys.stderr)
        return 1

    since = args.since or (
        datetime.now(timezone.utc) - timedelta(minutes=15)
    ).isoformat(timespec="seconds")
    request = CodeRequest(
        company=args.company or "",
        sender_hints=args.sender or [],
        subject_hints=args.subject or [],
        since_iso=since,
        timeout_s=args.timeout,
        application_id=args.application_id,
    )
    try:
        result = wait_for_code(conn, cfg, request)
    except Exception as exc:
        print(f"Code wait failed: {exc}", file=sys.stderr)
        return 1
    if args.application_id is not None:
        record_bridge_event(conn, args.application_id, result)
    _print(_code_payload(result, args.reveal_code))
    return 0 if result.matched else 2


def cmd_risk(args) -> int:
    cfg, conn = _connect(args)
    from . import risk

    try:
        if args.risk_action == "status":
            _print(risk.status(cfg))
            return 0
        if args.risk_action == "enable":
            policy = risk.enable(cfg, args.flag, args.acknowledge, conn=conn)
            _print(
                {
                    "enabled": args.flag,
                    "policy": policy.to_dict(),
                    "warning": risk.warning_text(args.flag, policy.daily_submit_cap),
                }
            )
            return 0
        if args.risk_action == "disable":
            policy = risk.disable(cfg, args.flag, conn=conn)
            _print({"disabled": args.flag, "policy": policy.to_dict()})
            return 0
    except risk.RiskError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 2


def cmd_autopilot(args) -> int:
    cfg, conn = _connect(args)
    from . import risk
    from .autopilot import latest_digest, read_trace, run_once, status_payload

    try:
        if args.autopilot_action == "status":
            _print(
                {
                    "risk": risk.status(cfg),
                    **status_payload(cfg),
                }
            )
            return 0
        if args.autopilot_action == "digest":
            _print(latest_digest(cfg) or {"empty": True})
            return 0
        if args.autopilot_action == "trace":
            _print(read_trace(cfg, limit=args.limit))
            return 0
        if args.autopilot_action == "watch":
            seen = 0
            while True:
                payload = status_payload(cfg)
                state = payload.get("state") or {}
                counters = state.get("counters") or {}
                print(
                    f"{state.get('updated_at') or '-'}  {state.get('status') or 'idle':8}  "
                    f"{state.get('stage') or '-':12}  "
                    f"prepared={counters.get('prepared', 0)} submitted={counters.get('submitted', 0)} "
                    f"skipped={counters.get('skipped', 0)} failed={counters.get('failed', 0)}",
                    flush=True,
                )
                running = bool(payload.get("running"))
                while True:
                    batch = read_trace(cfg, limit=500, offset=seen)
                    for event in batch:
                        print(
                            "  " + str(event.get("ts") or "") + "  " + str(event.get("event") or ""),
                            flush=True,
                        )
                    seen += len(batch)
                    # A full batch means more events may be waiting. Drain while
                    # running; after a run stops, keep draining until short so the
                    # final events are not lost behind the 500-event window.
                    if len(batch) < 500:
                        break
                if not running:
                    return 0
                time.sleep(max(0.5, float(args.interval)))
        if args.autopilot_action == "run":
            if not args.dry_run:
                allowed, reason, _policy = risk.can_submit_autopilot(cfg)
                if not allowed:
                    print(reason, file=sys.stderr)
                    return 1
            while True:
                stats = run_once(
                    cfg,
                    conn,
                    limit=args.limit,
                    min_score=args.min_score,
                    scan=args.scan,
                    score=args.score,
                    dry_run=args.dry_run,
                    sleep_seconds=args.sleep,
                )
                _print(stats)
                if not args.loop:
                    return 0
                import time

                time.sleep(max(60.0, float(args.interval) * 60.0))
    except risk.RiskError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(str(exc), file=sys.stderr)
        return 1
    return 2


def cmd_decisions(args) -> int:
    if args.decisions_action == "shadow":
        cfg = load_config(args.config)
        from .automation.decision_shadow import read_shadow, shadow_path

        _print(read_shadow(shadow_path(cfg), limit=args.limit))
        return 0
    if args.decisions_action == "compare":
        from .audit.decision_compare import main as compare_main

        argv = ["--fixtures", str(args.fixtures), "--out", str(args.out)]
        if args.json:
            argv.append("--json")
        return compare_main(argv)
    from .audit.decision_eval import main as replay_main

    argv = ["--fixtures", str(args.fixtures)]
    if args.json:
        argv.append("--json")
    return replay_main(argv)


def cmd_queue(args) -> int:
    cfg, conn = _connect(args)
    from . import services
    from .queue import Queue
    from .risk import RiskError

    queue = Queue(cfg.data_dir / "queue.json")
    profile, prefs = services.require_profile(cfg, conn)

    try:
        if args.queue_action == "build":
            min_score = args.min_score if args.min_score is not None else prefs.min_match
            items = queue.build(conn, profile, prefs, limit=args.limit, min_score=min_score)
            _print(
                {
                    "added": len(items),
                    "items": [item.to_dict() for item in items],
                    "summary": queue.summary(),
                }
            )
            return 0
        if args.queue_action == "list":
            items = queue.list_items(args.status)
            for item in items:
                print(
                    f"{item.job_id:>5}  {item.status:10}  {item.score:>5.1f}  "
                    f"{item.company[:24]:24}  {item.title[:48]}"
                )
            return 0
        if args.queue_action == "summary":
            _print(queue.summary())
            return 0
        if args.queue_action == "next":
            item = queue.next_actionable()
            if item is None:
                print("Queue is empty", file=sys.stderr)
                return 1
            _print(item.to_dict())
            return 0
        if args.queue_action == "prepare":
            _print(queue.prepare_next(cfg, conn).to_dict())
            return 0
        if args.queue_action == "apply":
            confirm = None
            if not args.auto and (args.confirm or args.hold == 0):

                def confirm() -> bool:
                    return input("Did you submit this application? [y/N] ").strip().lower().startswith("y")

            item = queue.apply_next(
                cfg,
                conn,
                hold_seconds=args.hold,
                confirm=confirm,
                auto_submit=args.auto,
            )
            _print(item.to_dict())
            return 0
        if args.queue_action == "skip":
            _print(queue.mark_skipped(args.job).to_dict())
            return 0
    except RiskError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except quality.QualityBlocked as exc:
        print(str(exc), file=sys.stderr)
        for check in exc.blocking:
            print(f"  - {check.name}: {check.detail}", file=sys.stderr)
        return 1
    except LookupError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 2


def cmd_providers(args) -> int:
    cfg, conn = _connect(args)
    from .agent.providers import (
        ProviderCredentialStore,
        ProviderError,
        claude_code_status,
        get_provider,
        make_provider_client,
        persist_llm_config,
        provider_status,
        remember_provider_connection,
    )

    store = ProviderCredentialStore(cfg.data_dir)
    action = args.provider_action
    try:
        if action == "list":
            cards = provider_status(cfg, conn, store=store)
            if args.json:
                _print(
                    [
                        {
                            "provider": card["spec"].id,
                            "label": card["spec"].label,
                            "connected": card["connected"],
                            "backend": card["backend"],
                            "active": card["active"],
                            "active_model": card["active_model"],
                            "models": card["models"][:20],
                        }
                        for card in cards
                    ]
                )
            else:
                for card in cards:
                    mark = "*" if card["active"] else " "
                    status = "connected" if card["connected"] else "not connected"
                    print(
                        f"{mark} {card['spec'].id:12} {card['spec'].label:28} "
                        f"{status:14} {card['backend'] or '-':8} "
                        f"{card['active_model'] or '-'}"
                    )
            return 0

        spec = get_provider(args.provider)
        if action == "connect":
            if spec.auth_mode == "cli":
                status = claude_code_status()
                if not status.authenticated:
                    raise ProviderError(
                        status.error
                        or "Claude Code is not signed in. Run `claude auth login`."
                    )
                record = store.load(spec.id)
                remember_provider_connection(
                    conn,
                    spec,
                    backend="cli",
                    base_url="",
                    models=list(spec.fallback_models),
                    model=args.model or "",
                    display_name=spec.label,
                )
                if args.model:
                    persist_llm_config(
                        cfg, provider=spec.id, model=args.model, base_url=""
                    )
                _print(
                    {
                        "provider": spec.id,
                        "label": spec.label,
                        "backend": "cli",
                        "auth_method": status.auth_method,
                        "version": status.version,
                        "model": args.model or "",
                        "note": "Uses Claude Code's own sign-in; no credential is read or stored.",
                    }
                )
                return 0
            key = args.key or ""
            if not key and spec.auth_mode in {"api_key", "oauth"}:
                existing = store.load(spec.id)
                if existing and existing.backend == "env":
                    record = existing
                else:
                    key = getpass.getpass(f"{spec.label} API key: ")
                    record = store.save(spec.id, key) if key.strip() else None
            elif key:
                print(
                    "Warning: --key may remain in shell history; prefer the prompt or an environment variable.",
                    file=sys.stderr,
                )
                record = store.save(spec.id, key)
            else:
                record = store.load(spec.id)
            if record is None and spec.auth_mode not in {"local", "optional"}:
                raise ProviderError("No credential was provided.")
            base_url = args.base_url or spec.default_base_url
            remember_provider_connection(
                conn,
                spec,
                backend=record.backend if record else "local",
                base_url=base_url,
                models=list(spec.fallback_models),
                model=args.model or "",
                display_name=spec.label,
            )
            if args.model:
                persist_llm_config(cfg, provider=spec.id, model=args.model, base_url=base_url)
            _print(
                {
                    "provider": spec.id,
                    "label": spec.label,
                    "backend": record.backend if record else "local",
                    "base_url": base_url,
                    "model": args.model or "",
                    "warning": record.warning if record else "",
                }
            )
            return 0

        if action == "models":
            connection = db.get_connection(conn, f"llm:{spec.id}")
            meta = (connection or {}).get("meta") or {}
            record = store.load(spec.id, str((connection or {}).get("token_backend") or ""))
            if record is None and spec.auth_mode not in {"local", "optional", "cli"}:
                raise ProviderError("Connect the provider first.")
            if spec.auth_mode == "cli":
                status = claude_code_status()
                if not status.authenticated:
                    raise ProviderError(
                        status.error
                        or "Claude Code is not signed in. Run `claude auth login`."
                    )
                models = list(spec.fallback_models)
            else:
                client = make_provider_client(
                    spec,
                    base_url=str(meta.get("base_url") or spec.default_base_url),
                    api_key=record.token if record else "",
                    model=str(meta.get("model") or (spec.fallback_models[0] if spec.fallback_models else "model")),
                    timeout=cfg.llm.timeout,
                    temperature=cfg.llm.temperature,
                    max_tokens=cfg.llm.max_tokens,
                )
                models = client.list_models()
            remember_provider_connection(
                conn,
                spec,
                backend=record.backend if record else "local",
                base_url=str(meta.get("base_url") or spec.default_base_url),
                models=models,
                model=str(meta.get("model") or ""),
                display_name=spec.label,
            )
            _print(models)
            return 0

        if action == "use":
            connection = db.get_connection(conn, f"llm:{spec.id}")
            record = store.load(spec.id)
            if record is None and spec.auth_mode not in {"local", "optional", "cli"}:
                raise ProviderError("Connect the provider first.")
            if spec.auth_mode == "cli":
                status = claude_code_status()
                if not status.authenticated:
                    raise ProviderError(
                        status.error
                        or "Claude Code is not signed in. Run `claude auth login`."
                    )
            base_url = args.base_url or str(((connection or {}).get("meta") or {}).get("base_url") or spec.default_base_url)
            persist_llm_config(cfg, provider=spec.id, model=args.model, base_url=base_url)
            remember_provider_connection(
                conn,
                spec,
                backend=record.backend if record else "local",
                base_url=base_url,
                model=args.model,
                display_name=spec.label,
            )
            _print({"active": f"{spec.id}:{args.model}", "base_url": base_url})
            return 0

        if action == "test":
            connection = db.get_connection(conn, f"llm:{spec.id}")
            meta = (connection or {}).get("meta") or {}
            record = store.load(spec.id, str((connection or {}).get("token_backend") or ""))
            if record is None and spec.auth_mode not in {"local", "optional", "cli"}:
                raise ProviderError("Connect the provider first.")
            if spec.auth_mode == "cli":
                status = claude_code_status()
                if not status.authenticated:
                    raise ProviderError(
                        status.error
                        or "Claude Code is not signed in. Run `claude auth login`."
                    )
            model = (
                str(meta.get("model") or spec.fallback_models[0])
                if spec.auth_mode == "cli" and spec.fallback_models
                else str(
                    meta.get("model")
                    or cfg.llm.model
                    or (spec.fallback_models[0] if spec.fallback_models else "")
                )
            )
            client = make_provider_client(
                spec,
                base_url=str(meta.get("base_url") or spec.default_base_url),
                api_key=record.token if record else "",
                model=model,
                timeout=cfg.llm.timeout,
                temperature=cfg.llm.temperature,
                max_tokens=cfg.llm.max_tokens,
            )
            result = client.test_chat()
            reply = result.content or ""
            if record and record.token:
                reply = reply.replace(record.token, "[redacted]")
            _print({"provider": spec.id, "model": model, "reply": reply, "usage": result.usage})
            return 0

        if action == "disconnect":
            store.delete(spec.id)
            db.delete_connection(conn, f"llm:{spec.id}")
            _print({"disconnected": spec.id})
            return 0
    except (ProviderError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 2


def cmd_agent(args) -> int:
    cfg, conn = _connect(args)
    from .agent.providers import ProviderCredentialStore
    from .agent.runtime import AgentError, AgentRuntime

    profile, prefs = services.require_profile(cfg, conn)
    runtime = AgentRuntime(
        cfg,
        conn,
        profile,
        prefs,
        provider_id=args.provider or None,
        model=args.model or None,
        allow_writes=bool(args.allow_writes),
        store=ProviderCredentialStore(cfg.data_dir),
    )
    thread_id = args.thread
    try:
        if args.message:
            result = runtime.run(args.message, thread_id=thread_id)
            if args.json:
                _print(
                    {
                        "thread_id": result.thread_id,
                        "content": result.content,
                        "provider": result.provider,
                        "model": result.model,
                        "steps": result.steps,
                        "usage": result.usage,
                        "tool_trace": result.tool_trace,
                        "stopped_reason": result.stopped_reason,
                    }
                )
            else:
                print(result.content)
                if result.tool_trace:
                    print("\nTools:")
                    for item in result.tool_trace:
                        print(f"  - {item['name']}: {item['summary']}")
            return 0

        if thread_id is None:
            thread_id = db.create_agent_thread(
                conn,
                profile.id or 0,
                provider=args.provider or cfg.llm.provider,
                model=args.model or cfg.llm.model,
            )
        print(f"JobForge agent · thread {thread_id} · Ctrl-D to exit", file=sys.stderr)
        while True:
            try:
                message = input("you> ")
            except EOFError:
                print(file=sys.stderr)
                return 0
            except KeyboardInterrupt:
                print(file=sys.stderr)
                return 0
            if not message.strip():
                continue
            try:
                result = runtime.run(message, thread_id=thread_id)
            except AgentError as exc:
                print(f"error: {exc}", file=sys.stderr)
                continue
            print(f"\njobforge> {result.content}\n")
            for item in result.tool_trace:
                print(f"  [tool] {item['name']}: {item['summary']}")
    except AgentError as exc:
        print(f"Agent failed: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_export(args) -> int:
    cfg, conn = _connect(args)
    path = services.export_data(cfg, conn)
    print(path)
    return 0


def cmd_import_profile(args) -> int:
    cfg, conn = _connect(args)
    path = Path(args.file)
    if path.suffix.lower() in {".pdf", ".docx", ".txt", ".md"}:
        profile = services.refresh_profile_from_resume(cfg, conn, path)
    else:
        from .matching.resume import load_profile_file

        profile = load_profile_file(path)
    profile.id = db.save_profile(conn, profile)
    db.set_active_profile(conn, profile.id)
    print(f"Imported profile {profile.id}: {profile.full_name or profile.name}")
    return 0


def cmd_import_resume_forge(args) -> int:
    cfg, conn = _connect(args)
    from .integrations.resume_forge import (
        import_applications,
        load_network,
        load_profile,
        preferences_from_profile,
    )

    profile_data = yaml.safe_load(Path(args.profile).read_text(encoding="utf-8")) or {}
    profile = load_profile(
        args.profile,
        projects_path=args.projects,
        evidence_path=args.evidence,
        resume_path=args.resume or "",
    )
    email_override = getattr(args, "email", None)
    if email_override:
        profile.email = email_override
    profile.id = db.save_profile(conn, profile)
    db.set_active_profile(conn, profile.id)
    db.save_preferences(conn, profile.id, preferences_from_profile(profile_data))

    summary: dict[str, object] = {
        "profile_id": profile.id,
        "name": profile.full_name or profile.name,
        "experience": len(profile.experience),
        "projects": len(profile.projects),
        "facts": len(profile.facts),
        "skills": len(profile.skills),
    }

    if args.network:
        network_path = cfg.data_dir / "network.yml"
        if network_path.exists() and not args.force_network:
            summary["network"] = f"exists, skipped: {network_path}"
        else:
            network = load_network(args.network)
            network_path.write_text(yaml.safe_dump(network, sort_keys=False), encoding="utf-8")
            try:
                network_path.chmod(0o600)
            except OSError:
                pass
            summary["network"] = f"{len(network.get('connections', []))} connections -> {network_path}"

    archetypes_arg = getattr(args, "archetypes", None)
    if archetypes_arg:
        archetype_path = cfg.data_dir / "role_archetypes.yml"
        archetype_path.write_text(Path(archetypes_arg).read_text(encoding="utf-8"), encoding="utf-8")
        try:
            archetype_path.chmod(0o600)
        except OSError:
            pass
        summary["archetypes"] = str(archetype_path)

    if args.applications:
        summary["applications"] = import_applications(
            conn,
            profile.id,
            args.applications,
            include_unstarted=args.include_unstarted,
        )

    db.log_event(conn, None, "resume_forge_import", str(args.profile))
    _print(summary)
    return 0


def cmd_serve(args) -> int:
    cfg = load_config(args.config)
    db.init_db(cfg.db_path)
    from .web.app import run

    run(cfg, host=args.host or cfg.server.host, port=args.port or cfg.server.port)
    return 0


def cmd_audit(args) -> int:
    cfg, conn = _connect(args)
    from .audit.parity import run_parity
    from .audit.security import run_security

    _print({"parity": run_parity(cfg, conn), "security": run_security()})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobforge", description="Local-first job search workbench")
    parser.add_argument("--config", help="path to config.yml")
    sub = parser.add_subparsers(dest="command")
    parser.set_defaults(func=cmd_app, host=None, port=None, no_browser=False)

    p = sub.add_parser("init", help="initialize the database and profile")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("scan", help="fetch jobs from configured sources or URLs")
    p.add_argument("--source", help="only this source")
    p.add_argument("--board", help="a single board slug for --source")
    p.add_argument("--url", action="append", help="fetch a single job URL (repeatable)")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("score", help="rescore every job")
    p.set_defaults(func=cmd_score)

    p = sub.add_parser("tailor", help="generate a tailored resume for a job")
    p.add_argument("--job", type=int, required=True)
    p.set_defaults(func=cmd_tailor)

    p = sub.add_parser("apply", help="fill an application in a headed browser")
    p.add_argument("--job", type=int, required=True)
    p.add_argument("--resume", help="override the resume PDF")
    submit_group = p.add_mutually_exclusive_group()
    submit_group.add_argument(
        "--allow-submit",
        action="store_true",
        help="click Submit automatically (default: human clicks)",
    )
    submit_group.add_argument(
        "--review-only",
        action="store_true",
        help="force review mode even if config.browser.allow_submit is true",
    )
    p.add_argument("--hold", type=int, default=0, help="keep the browser open for N seconds instead of waiting for Enter")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("jobs", help="list jobs by match score")
    p.add_argument("--query", default="")
    p.add_argument("--min-score", type=float, default=None)
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_jobs)

    p = sub.add_parser("status", help="show counts and recent runs")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("email", help="email tools")
    p.add_argument("action", choices=["poll"])
    p.set_defaults(func=cmd_email)

    p = sub.add_parser("code", help="wait for an email verification code")
    p.add_argument("--company", default="", help="company name hint")
    p.add_argument("--sender", action="append", help="sender hint (repeatable)")
    p.add_argument("--subject", action="append", help="subject hint (repeatable)")
    p.add_argument("--since", default="", help="UTC ISO timestamp; default 15 minutes ago")
    p.add_argument("--timeout", type=int, default=120, help="seconds to wait")
    p.add_argument("--application-id", type=int, help="record a redacted event for this application")
    p.add_argument(
        "--reveal-code",
        action="store_true",
        help="print the full code to the terminal (default: redacted)",
    )
    p.set_defaults(func=cmd_code)

    p = sub.add_parser("risk", help="inspect or change opt-in risk settings")
    rsub = p.add_subparsers(dest="risk_action", required=True)
    r = rsub.add_parser("status", help="show risk flags, warnings, and usage")
    r.set_defaults(func=cmd_risk)
    r = rsub.add_parser("enable", help="enable a risky flag after explicit acknowledgement")
    r.add_argument("--flag", required=True, help="unattended_submit or autopilot_submit")
    r.add_argument("--acknowledge", required=True, help='type exactly: I UNDERSTAND THE RISK')
    r.set_defaults(func=cmd_risk)
    r = rsub.add_parser("disable", help="disable a risky flag")
    r.add_argument("--flag", required=True)
    r.set_defaults(func=cmd_risk)

    p = sub.add_parser("queue", help="supervised application queue")
    qsub = p.add_subparsers(dest="queue_action", required=True)
    q = qsub.add_parser("build", help="fill the queue from scored matches")
    q.add_argument("--limit", type=int, default=10)
    q.add_argument("--min-score", type=float, default=None)
    q.set_defaults(func=cmd_queue)
    q = qsub.add_parser("list", help="list queue items")
    q.add_argument("--status", choices=["pending", "prepared", "submitted", "skipped", "failed"])
    q.set_defaults(func=cmd_queue)
    q = qsub.add_parser("summary", help="queue counts")
    q.set_defaults(func=cmd_queue)
    q = qsub.add_parser("next", help="show the next actionable item")
    q.set_defaults(func=cmd_queue)
    q = qsub.add_parser("prepare", help="tailor the next pending item")
    q.set_defaults(func=cmd_queue)
    q = qsub.add_parser("apply", help="open the next prepared item for review")
    q.add_argument("--hold", type=int, default=0, help="keep the browser open N seconds")
    q.add_argument(
        "--confirm",
        action="store_true",
        help="ask whether you submitted after the browser closes (also implied when --hold is 0)",
    )
    q.add_argument("--auto", action="store_true", help="risk-consented unattended submit (off by default)")
    q.set_defaults(func=cmd_queue)
    q = qsub.add_parser("skip", help="skip a queue item")
    q.add_argument("--job", type=int, required=True)
    q.set_defaults(func=cmd_queue)

    p = sub.add_parser("autopilot", help="sequential discovery, tailoring, and bounded submission")
    asub = p.add_subparsers(dest="autopilot_action", required=True)
    a = asub.add_parser("run", help="run one autopilot pass")
    a.add_argument("--limit", type=int, default=5, help="max prepared/submitted jobs (hard max 10)")
    a.add_argument("--min-score", type=float, default=None)
    a.add_argument("--scan", action="store_true", help="scan configured sources first")
    a.add_argument("--score", action="store_true", help="rescore stored jobs first")
    a.add_argument("--dry-run", action="store_true", help="tailor and prepare only; never submit")
    a.add_argument("--sleep", type=float, default=0.0, help="seconds between submissions")
    a.add_argument("--loop", action="store_true", help="keep running between passes")
    a.add_argument("--interval", type=float, default=30.0, help="minutes between loop passes (min 1)")
    a.set_defaults(func=cmd_autopilot)
    a = asub.add_parser("status", help="show risk, digest, and queue status")
    a.set_defaults(func=cmd_autopilot)
    a = asub.add_parser("digest", help="show the last autopilot digest")
    a.set_defaults(func=cmd_autopilot)
    a = asub.add_parser("trace", help="show the autopilot event trace")
    a.add_argument("--limit", type=int, default=80)
    a.set_defaults(func=cmd_autopilot)
    a = asub.add_parser("watch", help="follow live autopilot progress")
    a.add_argument("--interval", type=float, default=2.0, help="seconds between polls")
    a.set_defaults(func=cmd_autopilot)

    p = sub.add_parser("decisions", help="decision-contract replay and shadow diagnostics")
    dsub = p.add_subparsers(dest="decisions_action", required=True)
    d = dsub.add_parser("replay", help="replay frozen observation cases")
    d.add_argument("--fixtures", type=Path, default=Path("tests/fixtures/decisions"))
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=cmd_decisions)
    d = dsub.add_parser("shadow", help="show recent shadow decisions")
    d.add_argument("--limit", type=int, default=50)
    d.set_defaults(func=cmd_decisions)
    d = dsub.add_parser("compare", help="compare the policy to the current filler on ATS pages")
    d.add_argument("--fixtures", type=Path, default=Path("tests/fixtures/ats"))
    d.add_argument("--out", type=Path, default=Path("data/decision_compare_shadow.jsonl"))
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=cmd_decisions)

    p = sub.add_parser("export", help="export all data as JSON")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("import-profile", help="import a resume or profile YAML/JSON")
    p.add_argument("--file", required=True)
    p.set_defaults(func=cmd_import_profile)

    p = sub.add_parser("import-resume-forge", help="import a resume-forge profile/projects/evidence/tracker")
    p.add_argument("--profile", required=True, help="resume-forge profile.yml")
    p.add_argument("--projects", help="resume-forge projects.yml")
    p.add_argument("--evidence", help="resume-forge evidence_matrix.yml")
    p.add_argument("--network", help="resume-forge network.yml")
    p.add_argument("--archetypes", help="resume-forge role_archetypes.yml")
    p.add_argument("--applications", help="resume-forge applications.yml")
    p.add_argument("--resume", help="base resume PDF; its curated bullets are preferred")
    p.add_argument("--email", help="override the candidate email in the resume header")
    p.add_argument("--include-unstarted", action="store_true", help="also import Not Started tracker rows")
    p.add_argument("--force-network", action="store_true", help="overwrite data/network.yml")
    p.set_defaults(func=cmd_import_resume_forge)

    p = sub.add_parser("providers", help="connect model providers and choose models")
    psub = p.add_subparsers(dest="provider_action", required=True)
    pr = psub.add_parser("list", help="show provider connection and active-model status")
    pr.add_argument("--json", action="store_true")
    pr.set_defaults(func=cmd_providers)
    pr = psub.add_parser("connect", help="connect a provider (prompts for a key)")
    pr.add_argument("provider")
    pr.add_argument("--key", help="API key (prefer the prompt or an environment variable)")
    pr.add_argument("--base-url", default="")
    pr.add_argument("--model", default="")
    pr.set_defaults(func=cmd_providers)
    pr = psub.add_parser("models", help="discover models for a connected provider")
    pr.add_argument("provider")
    pr.set_defaults(func=cmd_providers)
    pr = psub.add_parser("use", help="set the active provider and model")
    pr.add_argument("provider")
    pr.add_argument("model")
    pr.add_argument("--base-url", default="")
    pr.set_defaults(func=cmd_providers)
    pr = psub.add_parser("test", help="send a tiny billable test chat")
    pr.add_argument("provider")
    pr.set_defaults(func=cmd_providers)
    pr = psub.add_parser("disconnect", help="remove the stored provider key and metadata")
    pr.add_argument("provider")
    pr.set_defaults(func=cmd_providers)

    p = sub.add_parser("agent", help="ask the local agent (one-shot or interactive)")
    p.add_argument("message", nargs="?", default="", help="one-shot message; omit for an interactive session")
    p.add_argument("--thread", type=int, help="continue a stored conversation")
    p.add_argument("--provider", default="", help="override the active provider")
    p.add_argument("--model", default="", help="override the active model")
    p.add_argument("--allow-writes", action="store_true", help="allow tailor/scan/queue tools for this run")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_agent)

    p = sub.add_parser("app", help="start the dashboard and open it in a browser")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--no-browser", action="store_true", help="do not open a browser")
    p.set_defaults(func=cmd_app)

    p = sub.add_parser("install", help="install the JobForge MCP server and skill into local agents")
    isub = p.add_subparsers(dest="install_action", required=True)
    i = isub.add_parser("hosts", help="list supported hosts and detection state")
    i.set_defaults(func=cmd_install)
    i = isub.add_parser("plan", help="show the exact install plan without writing")
    i.add_argument("--hosts", required=True, help="comma-separated host ids")
    i.add_argument("--profile", type=int)
    i.add_argument("--install-browser", action="store_true", help="include optional Chromium consent")
    i.set_defaults(func=cmd_install)
    i = isub.add_parser("apply", help="apply a reviewed install plan")
    i.add_argument("--hosts", required=True, help="comma-separated host ids")
    i.add_argument("--profile", type=int)
    i.add_argument("--install-browser", action="store_true", help="install optional Chromium")
    i.add_argument("--yes", action="store_true", help="confirm the reviewed plan")
    i.set_defaults(func=cmd_install)
    i = isub.add_parser("doctor", help="verify connections, skills, and host entries")
    i.add_argument("--hosts", required=True, help="comma-separated host ids")
    i.add_argument("--profile", type=int)
    i.set_defaults(func=cmd_install)

    p = sub.add_parser("integrations", help="manage read-only local agent connections")
    isub = p.add_subparsers(dest="integrations_action", required=True)
    i = isub.add_parser("list", help="list instances and connections")
    i.set_defaults(func=cmd_integrations)
    i = isub.add_parser("setup", help="register a read-only connection for a profile")
    i.add_argument("--profile", type=int, required=True)
    i.add_argument("--client", choices=["generic", "claude-code", "codex"], default="generic")
    i.add_argument("--label", default="")
    i.add_argument("--instance", default="")
    i.add_argument("--scopes", default="", help="comma-separated optional scopes")
    i.set_defaults(func=cmd_integrations)
    i = isub.add_parser("grant", help="add scopes to an existing connection")
    i.add_argument("--connection", required=True)
    i.add_argument("--scopes", required=True, help="comma-separated scopes")
    i.set_defaults(func=cmd_integrations)
    i = isub.add_parser("disable", help="revoke a connection")
    i.add_argument("--connection", required=True)
    i.set_defaults(func=cmd_integrations)
    i = isub.add_parser("config", help="print an MCP host config for a connection")
    i.add_argument("--connection", required=True)
    i.add_argument("--client", choices=["generic", "claude-code", "codex"], default="generic")
    i.set_defaults(func=cmd_integrations)
    i = isub.add_parser("doctor", help="check a connection and its read scopes")
    i.add_argument("--connection", required=True)
    i.add_argument("--instance", default="")
    i.set_defaults(func=cmd_integrations)

    p = sub.add_parser("tools", help="read-only machine interface for local agents")
    tsub = p.add_subparsers(dest="tools_action", required=True)
    t = tsub.add_parser("list", help="list tools and scopes for a connection")
    t.add_argument("--connection", required=True)
    t.add_argument("--instance", default="")
    t.set_defaults(func=cmd_tools)
    t = tsub.add_parser("call", help="call one read-only tool with JSON from stdin")
    t.add_argument("tool_name", choices=[
        "jobforge_status",
        "jobforge_search_jobs",
        "jobforge_get_job",
        "jobforge_explain_fit",
        "jobforge_get_readiness",
        "jobforge_get_profile",
        "jobforge_list_applications",
        "jobforge_remember",
        "jobforge_recall",
        "jobforge_forget",
        "jobforge_dashboard",
        "jobforge_open_dashboard",
        "jobforge_request_resume",
        "jobforge_get_operation",
        "jobforge_list_operations",
    ])
    t.add_argument("--connection", required=True)
    t.add_argument("--instance", default="")
    t.add_argument("--input", default="-", help="must be -; JSON payload is read from stdin")
    t.set_defaults(func=cmd_tools)

    p = sub.add_parser("operation", help="run one approved external operation")
    osub = p.add_subparsers(dest="operation_action", required=True)
    op = osub.add_parser("run", help="claim and execute an approved operation")
    op.add_argument("--id", dest="operation_id", required=True)
    op.set_defaults(func=cmd_operation)

    p = sub.add_parser("skills", help="export the generic JobForge Agent Skill")
    ssub = p.add_subparsers(dest="skills_action", required=True)
    sk = ssub.add_parser("export", help="copy the packaged skill to a human-selected directory")
    sk.add_argument("--destination", required=True, help="parent directory; jobforge/ is created inside")
    sk.add_argument("--force", action="store_true", help="merge into an existing jobforge/ directory")
    sk.set_defaults(func=cmd_skills)

    p = sub.add_parser("mcp", help="run the read-only MCP stdio server")
    msub = p.add_subparsers(dest="mcp_action", required=True)
    m = msub.add_parser("serve", help="serve read-only tools over stdio")
    m.add_argument("--connection", required=True)
    m.add_argument("--instance", default="")
    m.set_defaults(func=cmd_mcp)

    p = sub.add_parser("setup", help="first-run setup: config, data dirs, optional browser")
    p.add_argument("--skip-browser", action="store_true", help="do not download Chromium")
    p.add_argument(
        "--install-browser",
        action="store_true",
        help="download Playwright Chromium for application filling (optional)",
    )
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("doctor", help="check paths, Typst, and browser availability")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("serve", help="start the local dashboard")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("audit", help="run parity and security audits")
    p.set_defaults(func=cmd_audit)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        return cmd_app(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
