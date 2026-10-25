from __future__ import annotations

import argparse
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


def cmd_setup(args) -> int:
    from .paths import ensure_config

    config_path = ensure_config()
    cfg = load_config(args.config or (str(config_path) if config_path.exists() else None))
    cfg.ensure_dirs()
    db.init_db(cfg.db_path)

    print("JobForge setup")
    print(f"  config: {cfg.config_path or '(defaults)'}")
    print(f"  data:   {cfg.data_dir}")

    if not args.skip_browser:
        print("Installing Playwright Chromium (first run only)...")
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=False,
        )
        if result.returncode != 0:
            print(
                "Chromium install failed. JobForge will fall back to system Chrome if installed.",
                file=sys.stderr,
            )

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
            "No Chromium or Chrome found. Run `jobforge setup` with network access, "
            "or install Google Chrome.",
            file=sys.stderr,
        )
        return 1
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
        profile, prefs = services.ensure_profile(cfg, conn)
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
    profile, prefs = services.ensure_profile(cfg, conn)
    rows = db.list_jobs(
        conn,
        profile.id,
        q=args.query,
        min_score=args.min_score,
        one_per_company=prefs.one_application_per_company,
        limit=args.limit,
    )
    for row in rows:
        print(
            f"{row['id']:>4}  {row.get('score') or 0:>5.1f}  {row['company'][:24]:24}  "
            f"{row['title'][:52]:52}  {row.get('location','')[:24]}"
        )
    return 0


def cmd_status(args) -> int:
    cfg, conn = _connect(args)
    profile, _ = services.ensure_profile(cfg, conn)
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


def cmd_queue(args) -> int:
    cfg, conn = _connect(args)
    from . import services
    from .queue import Queue
    from .risk import RiskError

    queue = Queue(cfg.data_dir / "queue.json")
    profile, prefs = services.ensure_profile(cfg, conn)

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
    p.add_argument("--allow-submit", action="store_true", help="click Submit automatically (default: human clicks)")
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
    r.add_argument("--flag", required=True, help="unattended_submit")
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

    p = sub.add_parser("app", help="start the dashboard and open it in a browser")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--no-browser", action="store_true", help="do not open a browser")
    p.set_defaults(func=cmd_app)

    p = sub.add_parser("setup", help="first-run setup: config, data dirs, browser")
    p.add_argument("--skip-browser", action="store_true", help="do not download Chromium")
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
