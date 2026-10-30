"""FastAPI dashboard. Local-first: binds to 127.0.0.1 by default."""

from __future__ import annotations

import os
import secrets
import shutil
import sqlite3
import tempfile
import uuid
from functools import wraps
from pathlib import Path
from typing import Any, Generator
from urllib.parse import urlsplit

import yaml

import uvicorn
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import db, quality, risk, services
from ..autopilot import latest_digest, launch_subprocess as launch_autopilot, status_payload
from ..agent.providers import (
    ProviderCredentialStore,
    infer_provider,
    provider_status,
    resolve_provider,
)
from ..automation.base import is_manual_only_url
from ..config import Config, FeatureFlags, load_config
from ..integrations import github as github_integration
from ..locking import ActionBusy, action_lock
from ..matching.resume import load_profile_file
from ..models import ApplicationStatus, Preferences, Profile
from ..paths import default_config_path
from ..queue import Queue
from ..sources.registry import SOURCES, configured_boards
from ..workbench.worker import launch_worker
from . import setup_presets
from .agent_routes import create_agent_router
from .answers_routes import create_answers_router
from .provider_routes import create_provider_router

WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
templates.env.filters["money"] = lambda v: f"${int(v):,}" if v else ""
templates.env.filters["pct"] = lambda v: f"{float(v or 0) * 100:.0f}%"
templates.env.filters["short"] = lambda v, n=140: (str(v or "")[:n] + ("..." if len(str(v or "")) > n else ""))


def _msg(text: str) -> str:
    from urllib.parse import quote

    return quote(text, safe="")


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in (value or "").replace("\n", ",").split(",") if item.strip()]


def _split_locations(value: str) -> list[str]:
    # A comma is part of "New York, NY"; use semicolons/newlines between places.
    return [item.strip() for item in (value or "").replace("\n", ";").split(";") if item.strip()]


def _split_lines(value: str, limit: int = 5) -> list[str]:
    return [
        item.strip(" \t-*•")
        for item in (value or "").replace("\r", "\n").split("\n")
        if item.strip(" \t-*•")
    ][:limit]


def _github_store(cfg: Config) -> github_integration.GitHubTokenStore:
    # CI and headless smoke runs cannot touch an OS keychain. The file fallback
    # is still 0600 and is exercised by the same code path users get when no
    # keychain backend is available.
    if os.environ.get("JOBFORGE_DISABLE_KEYRING") == "1":
        return github_integration.GitHubTokenStore(cfg.data_dir, keyring_module=False)
    return github_integration.GitHubTokenStore(cfg.data_dir)


def _github_imported(profile: Any) -> set[int]:
    return github_integration.imported_repo_ids(profile) if profile else set()


def _preferences_from_form(
    *,
    titles: str,
    locations: str,
    work_modes: str,
    employment_types: str,
    experience_levels: str,
    salary_min: int,
    min_match: int,
    needs_sponsorship: str,
    open_to_relocation: str,
    excluded_companies: str = "",
    excluded_keywords: str = "",
    included_keywords: str = "",
) -> Preferences:
    return Preferences(
        titles=_split_csv(titles)[:5],
        locations=_split_locations(locations),
        work_modes=_split_csv(work_modes),
        employment_types=_split_csv(employment_types),
        experience_levels=_split_csv(experience_levels),
        salary_min=max(0, int(salary_min or 0)),
        min_match=max(0, min(100, int(min_match or 0))),
        needs_sponsorship=bool(needs_sponsorship),
        open_to_relocation=bool(open_to_relocation),
        excluded_companies=_split_csv(excluded_companies),
        excluded_keywords=_split_csv(excluded_keywords),
        included_keywords=_split_csv(included_keywords),
    )


SOURCE_MIXES: dict[str, list[str]] = {
    "recommended": ["ai", "product", "github_lists"],
    "broad": ["ai", "fintech", "product", "public", "github_lists"],
    "minimal": ["github_lists"],
}


def _source_mix_value(selected: list[str]) -> str:
    current = set(selected)
    if current == set(SOURCE_MIXES["recommended"]):
        return "recommended"
    if current == set(SOURCE_MIXES["broad"]):
        return "broad"
    if current == set(SOURCE_MIXES["minimal"]):
        return "minimal"
    return "keep" if current else "recommended"


def _setup_state(cfg: Config, conn: sqlite3.Connection, profile: Any) -> dict[str, Any]:
    complete = db.setting_get(conn, "setup_complete") == "1"
    has_profile = bool(profile and (profile.facts or profile.resume_path or profile.email))
    has_sources = any(configured_boards(cfg, name) for name in SOURCES) or setup_presets.public_source_enabled(cfg)
    jobs = int(db.stats(conn, profile.id or 0).get("jobs", 0)) if profile else 0
    if not cfg.features.setup_wizard:
        return {
            "complete": complete,
            "has_profile": has_profile,
            "has_sources": has_sources,
            "jobs": jobs,
            "incomplete": False,
            "first_run": False,
        }
    incomplete = not complete and not (has_profile and has_sources)
    first_run = not complete and not has_profile and not has_sources and jobs == 0
    return {
        "complete": complete,
        "has_profile": has_profile,
        "has_sources": has_sources,
        "jobs": jobs,
        "incomplete": incomplete,
        "first_run": first_run,
    }


def _browser_ready(cfg: Config) -> bool:
    if cfg.browser.channel and shutil.which(cfg.browser.channel):
        return True
    mac_chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if mac_chrome.is_file():
        return True
    if shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chrome"):
        return True
    for root in (Path.home() / "Library/Caches/ms-playwright", Path.home() / ".cache/ms-playwright"):
        try:
            if root.is_dir() and any(root.glob("chromium-*")):
                return True
        except OSError:
            continue
    return False


def _store_upload(cfg: Config, file: UploadFile) -> tuple[Path, str]:
    name = Path(file.filename or "upload").name
    suffix = Path(name).suffix.lower()
    if suffix not in {".pdf", ".docx", ".txt", ".md", ".yaml", ".yml", ".json"}:
        raise HTTPException(status_code=400, detail="Unsupported file type")
    dest = cfg.data_dir / "uploads" / f"{uuid.uuid4().hex}-{name}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as fh:
        shutil.copyfileobj(file.file, fh)
    if dest.stat().st_size > 10 * 1024 * 1024:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=413, detail="File larger than 10 MB")
    if dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    return dest, suffix


def _import_profile_upload(cfg: Config, conn: sqlite3.Connection, file: UploadFile):
    dest, suffix = _store_upload(cfg, file)
    if suffix in {".pdf", ".docx", ".txt", ".md"}:
        return services.refresh_profile_from_resume(cfg, conn, dest)
    profile = load_profile_file(dest)
    profile.id = db.save_profile(conn, profile)
    db.set_active_profile(conn, profile.id)
    return profile


def _write_config_sections(cfg: Config, updates: dict[str, Any]) -> Path:
    path = Path(cfg.config_path) if cfg.config_path else default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, yaml.YAMLError):
            data = {}
    data.update(updates)
    fd, temp_name = tempfile.mkstemp(prefix=".config-", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True)
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    for key, value in updates.items():
        setattr(cfg, key, value)
    cfg.config_path = path
    return path


def _write_setup_config(cfg: Config, sources: dict[str, Any], scan: dict[str, Any]) -> Path:
    return _write_config_sections(cfg, {"sources": sources, "scan": scan})


def _write_feature_config(cfg: Config, flags: FeatureFlags) -> Path:
    path = _write_config_sections(cfg, {"features": flags.to_dict()})
    cfg.features = flags
    return path


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or load_config()
    db.init_db(cfg.db_path)

    app = FastAPI(title="JobForge", docs_url="/api/docs", redoc_url=None)

    @app.exception_handler(ActionBusy)
    async def action_busy_handler(request: Request, exc: ActionBusy):
        detail = "Another JobForge action is running. Wait for it to finish and try again."
        path = request.url.path
        if path.startswith("/api/") or "application/json" in request.headers.get(
            "accept", ""
        ):
            return JSONResponse({"error": "busy", "detail": detail}, status_code=409)
        if path.startswith("/setup/"):
            target = "/setup"
        elif path.startswith("/connections"):
            target = "/connections"
        elif path.startswith("/providers"):
            target = "/providers"
        elif path.startswith("/settings") or path == "/export":
            target = "/settings"
        elif path.startswith("/email"):
            target = "/inbox"
        elif path.startswith("/jobs/"):
            target = path.rsplit("/", 1)[0]
        elif path.startswith("/applications"):
            target = "/applications"
        elif path.startswith("/profiles"):
            target = "/profiles"
        elif path.startswith("/autopilot"):
            target = "/autopilot"
        else:
            target = "/"
        return RedirectResponse(f"{target}?msg={_msg(detail)}", status_code=303)

    @app.exception_handler(services.SetupRequired)
    async def setup_required_handler(request: Request, exc: services.SetupRequired):
        wants_json = request.url.path.startswith("/api/") or "application/json" in request.headers.get(
            "accept", ""
        )
        if wants_json or not cfg.features.setup_wizard:
            return JSONResponse(
                {"error": "setup_required", "detail": str(exc)}, status_code=409
            )
        return RedirectResponse("/setup", status_code=303)

    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
    app.include_router(create_answers_router(cfg))
    app.include_router(create_agent_router(cfg))
    app.include_router(create_provider_router(cfg))

    def get_conn() -> Generator[sqlite3.Connection, None, None]:
        conn = db.connect(cfg.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def locked_write(fn):
        """Serialize a dashboard mutation with other domain actions."""

        @wraps(fn)
        def wrapper(*args, **kwargs):
            with action_lock(cfg, blocking=False):
                return fn(*args, **kwargs)

        return wrapper

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            host = request.headers.get("host", "")
            if origin:
                parsed = urlsplit(origin)
                if not host or parsed.scheme != request.url.scheme or parsed.netloc != host:
                    return JSONResponse({"error": "cross-origin request blocked"}, status_code=403)

        if request.url.path.startswith("/connections") and not cfg.features.github_connections:
            return JSONResponse({"error": "not found"}, status_code=404)
        if request.url.path.startswith("/setup") and not cfg.features.setup_wizard:
            return JSONResponse({"error": "not found"}, status_code=404)
        if request.url.path.startswith(("/agent", "/providers", "/api/agent")) and not cfg.features.agent:
            return JSONResponse({"error": "not found"}, status_code=404)

        # The optional bearer token protects every route except the health check
        # and the token login endpoint. This covers unsafe writes and the
        # answer-bank GETs, which can expose salary/authorization answers.
        public_paths = {
            "/healthz",
            "/auth",
            # OAuth callback is reached by a cross-site redirect; the random
            # state and PKCE verifier are the authorization proof. The token
            # cookie is SameSite=strict and may not survive that redirect.
            "/providers/oauth/openrouter/callback",
        }
        if cfg.server.token and request.url.path not in public_paths:
            supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            cookie = request.cookies.get("jf_token", "")
            if not (
                (supplied and secrets.compare_digest(supplied, cfg.server.token))
                or (cookie and secrets.compare_digest(cookie, cfg.server.token))
            ):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Chromium serializes a same-origin form POST's Origin as "null" when
        # the document is served with Referrer-Policy: no-referrer, which the
        # strict origin check above would reject. Same-origin still leaks no
        # referrer off-host and keeps a real Origin on dashboard writes.
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    def context(
        request: Request, conn: sqlite3.Connection, *, allow_missing: bool = False
    ) -> dict[str, Any]:
        profile, prefs = services.read_profile(cfg, conn)
        if profile is None:
            if not allow_missing and cfg.features.setup_wizard:
                raise services.SetupRequired(
                    "JobForge setup is incomplete; run `jobforge setup` first"
                )
            # The setup page, and an installation with the setup wizard
            # disabled, must render before a profile exists. This placeholder
            # is never persisted and never reaches a domain query.
            profile, prefs = Profile(name=""), Preferences()
        setup_state = _setup_state(cfg, conn, profile)
        agent_ready = False
        if cfg.features.agent:
            try:
                resolve_provider(cfg, conn, store=ProviderCredentialStore(cfg.data_dir))
                agent_ready = True
            except Exception:  # noqa: BLE001 - readiness is display-only
                agent_ready = False
        return {
            "request": request,
            "profile": profile,
            "prefs": prefs,
            "cfg": cfg,
            "msg": request.query_params.get("msg", ""),
            "setup_state": setup_state,
            "setup_incomplete": setup_state["incomplete"],
            "setup_complete": setup_state["complete"],
            "features": cfg.features,
            "agent_ready": agent_ready,
            "active_provider": infer_provider(cfg.llm),
            "active_model": cfg.llm.model,
        }

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        ctx = context(request, conn)
        if ctx["setup_state"]["first_run"]:
            return RedirectResponse("/setup", status_code=303)
        profile = ctx["profile"]
        stats = db.stats(conn, profile.id)
        jobs = db.list_jobs(
            conn,
            profile.id,
            min_score=ctx["prefs"].min_match,
            one_per_company=ctx["prefs"].one_application_per_company,
            limit=6,
        )
        graph = services.load_network(cfg)
        if graph:
            for job in jobs:
                warm = services.warm_summary(
                    graph, job["company"], float(job.get("score") or 0)
                )
                job["warm_channel"] = warm["channel"]
                job["warm_score"] = warm["score"]
        apps = db.list_applications(conn, profile.id, limit=6)
        runs = db.list_runs(conn, 5)
        agent_connections = [
            item
            for item in db.list_agent_connections(conn)
            if int(item.get("profile_id") or 0) == int(profile.id or 0)
        ]
        pending_operations = [
            item
            for item in db.list_agent_operations(conn, profile_id=profile.id, limit=20)
            if item.get("state") == "pending_approval"
        ]
        for operation in pending_operations:
            job_id = int((operation.get("payload") or {}).get("job_id") or 0)
            job = db.get_job(conn, job_id) if job_id else None
            operation["job_company"] = job.company if job else ""
            operation["job_title"] = job.title if job else ""
        github_lead_jobs = db.list_jobs(
            conn, profile.id, source="github_jobs", min_score=0, limit=3
        )
        github_lead_count = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE source='github_jobs'"
        ).fetchone()["n"]
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                **ctx,
                "stats": stats,
                "jobs": jobs,
                "apps": apps,
                "runs": runs,
                "agent_connections": agent_connections,
                "pending_operations": pending_operations,
                "github_lead_jobs": github_lead_jobs,
                "github_lead_count": github_lead_count,
            },
        )

    @app.get("/agents", response_class=HTMLResponse)
    def agents_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        from ..installer import detect_hosts

        ctx = context(request, conn)
        profile = ctx["profile"]
        connections = [
            item
            for item in db.list_agent_connections(conn)
            if int(item.get("profile_id") or 0) == int(profile.id or 0)
        ]
        try:
            hosts = detect_hosts()
        except Exception:  # noqa: BLE001 - display-only detection
            hosts = []
        return templates.TemplateResponse(
            request=request,
            name="agents.html",
            context={**ctx, "connections": connections, "hosts": hosts},
        )

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_page(
        request: Request,
        q: str = "",
        min_score: float | None = None,
        source: str = "",
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        ctx = context(request, conn)
        rows = db.list_jobs(
            conn,
            ctx["profile"].id,
            q=q,
            min_score=min_score,
            source=source,
            one_per_company=ctx["prefs"].one_application_per_company,
            limit=200,
        )
        return templates.TemplateResponse(
            request=request,
            name="jobs.html",
            context={**ctx, "jobs": rows, "q": q, "min_score": min_score, "source": source},
        )

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(request: Request, step: int = 1, conn: sqlite3.Connection = Depends(get_conn)):
        ctx = context(request, conn, allow_missing=True)
        profile = ctx["profile"]
        prefs = ctx["prefs"]
        ready_step = 3
        step = max(1, min(ready_step, step))
        selected = setup_presets.selected_presets(cfg)
        custom = setup_presets.custom_boards(cfg)
        source_count = setup_presets.source_board_count(cfg.sources or {})
        scan = cfg.scan or {}
        profile_titles = [
            str(item.get("title") or "")
            for item in (profile.experience or [])
            if isinstance(item, dict) and str(item.get("title") or "").strip()
        ]
        default_titles = prefs.titles or profile_titles[:1]
        default_locations = prefs.locations or ([profile.location] if profile.location else [])
        cards = (
            provider_status(cfg, conn, store=ProviderCredentialStore(cfg.data_dir))
            if step == 3 and cfg.features.agent
            else []
        )
        return templates.TemplateResponse(
            request=request,
            name="setup.html",
            context={
                **ctx,
                "step": step,
                "ready_step": ready_step,
                "presets": setup_presets.PRESETS,
                "selected_presets": selected,
                "custom": custom,
                "source_count": source_count,
                "source_mix": _source_mix_value(selected),
                "default_titles": default_titles,
                "default_locations": default_locations,
                "github_connected": bool(db.get_connection(conn, "github")),
                "github_repo_count": db.count_github_repos(conn, include_ignored=True),
                "typst_ready": bool(shutil.which("typst")),
                "browser_ready": _browser_ready(cfg),
                "usa_jobs_key": bool(os.environ.get("USAJOBS_API_KEY")),
                "scan_keywords": ", ".join(str(v) for v in (scan.get("keywords") or [])),
                "scan_exclude": ", ".join(str(v) for v in (scan.get("exclude_keywords") or [])),
                "max_new": int(scan.get("max_new") or 500),
                "cards": cards,
                "active_provider": infer_provider(cfg.llm),
                "active_model": cfg.llm.model,
                "agent_ready": ctx["agent_ready"],
            },
        )

    @app.post("/setup/profile")
    @locked_write
    def setup_profile(
        action: str = Form("upload"),
        file: UploadFile | None = File(None),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        if action != "skip" and file is not None and file.filename:
            try:
                _import_profile_upload(cfg, conn, file)
            except HTTPException as exc:
                return RedirectResponse(
                    f"/setup?step=1&msg={_msg(str(exc.detail))}", status_code=303
                )
            except Exception as exc:  # pragma: no cover - defensive for parser errors
                return RedirectResponse(
                    f"/setup?step=1&msg={_msg('Could not import that file: ' + str(exc))}",
                    status_code=303,
                )
            message = "Profile imported. Review it on the Profiles page."
        else:
            message = "Using the existing profile. You can import or edit it later."
        return RedirectResponse(f"/setup?step=2&msg={_msg(message)}", status_code=303)

    @app.post("/setup/preferences")
    @locked_write
    def setup_preferences(
        titles: str = Form(""),
        locations: str = Form(""),
        work_modes: str = Form(""),
        employment_types: str = Form(""),
        experience_levels: str = Form(""),
        salary_min: int = Form(0),
        min_match: int = Form(45),
        needs_sponsorship: str = Form(""),
        open_to_relocation: str = Form(""),
        excluded_companies: str = Form(""),
        excluded_keywords: str = Form(""),
        included_keywords: str = Form(""),
        source_mix: str = Form("recommended"),
        greenhouse_custom: str = Form(""),
        lever_custom: str = Form(""),
        ashby_custom: str = Form(""),
        workable_custom: str = Form(""),
        workday_custom: str = Form(""),
        smartrecruiters_custom: str = Form(""),
        github_jobs_custom: str = Form(""),
        scan_keywords: str = Form(""),
        scan_exclude: str = Form(""),
        max_new: int = Form(500),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        # Setup is allowed to create the default profile when the human skipped
        # the resume upload. Normal dashboard writes still use require_profile.
        profile, _ = services.ensure_profile(cfg, conn)
        prefs = _preferences_from_form(
            titles=titles,
            locations=locations,
            work_modes=work_modes,
            employment_types=employment_types,
            experience_levels=experience_levels,
            salary_min=salary_min,
            min_match=min_match,
            needs_sponsorship=needs_sponsorship,
            open_to_relocation=open_to_relocation,
            excluded_companies=excluded_companies,
            excluded_keywords=excluded_keywords,
            included_keywords=included_keywords,
        )
        db.save_preferences(conn, profile.id or 0, prefs)
        mix = (source_mix or "").strip()
        custom = {
            "greenhouse": greenhouse_custom,
            "lever": lever_custom,
            "ashby": ashby_custom,
            "workable": workable_custom,
            "workday": workday_custom,
            "smartrecruiters": smartrecruiters_custom,
            "github_jobs": github_jobs_custom,
        }
        if mix == "keep" and (cfg.sources or {}):
            sources = setup_presets.merge_sources(
                cfg.sources, setup_presets.build_sources([], custom)
            )
        else:
            selected = SOURCE_MIXES.get(mix)
            if selected is None:
                selected = setup_presets.selected_presets(cfg)
                if not selected:
                    selected = SOURCE_MIXES["recommended"]
            sources = setup_presets.build_sources(selected, custom)
        scan = {
            "keywords": _split_csv(scan_keywords),
            "exclude_keywords": _split_csv(scan_exclude),
            "max_new": max(0, int(max_new or 0)),
        }
        try:
            _write_setup_config(cfg, sources, scan)
        except (OSError, yaml.YAMLError, ValueError) as exc:
            return RedirectResponse(
                f"/setup?step=2&msg={_msg('Could not save sources: ' + str(exc))}",
                status_code=303,
            )
        if db.stats(conn, profile.id or 0).get("jobs", 0):
            services.rescore(cfg, conn)
        return RedirectResponse(
            f"/setup?step=3&msg={_msg('Targets and sources saved')}", status_code=303
        )

    @app.post("/setup/sources")
    @locked_write
    def setup_sources(
        preset_ai: str = Form(""),
        preset_fintech: str = Form(""),
        preset_product: str = Form(""),
        preset_public: str = Form(""),
        preset_github_lists: str = Form(""),
        greenhouse_custom: str = Form(""),
        lever_custom: str = Form(""),
        ashby_custom: str = Form(""),
        workable_custom: str = Form(""),
        workday_custom: str = Form(""),
        smartrecruiters_custom: str = Form(""),
        github_jobs_custom: str = Form(""),
        scan_keywords: str = Form(""),
        scan_exclude: str = Form(""),
        max_new: int = Form(500),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        selected = [
            key
            for key, value in (
                ("ai", preset_ai),
                ("fintech", preset_fintech),
                ("product", preset_product),
                ("public", preset_public),
                ("github_lists", preset_github_lists),
            )
            if value
        ]
        custom = {
            "greenhouse": greenhouse_custom,
            "lever": lever_custom,
            "ashby": ashby_custom,
            "workable": workable_custom,
            "workday": workday_custom,
            "smartrecruiters": smartrecruiters_custom,
            "github_jobs": github_jobs_custom,
        }
        sources = setup_presets.build_sources(selected, custom)
        if not sources:
            return RedirectResponse(
                f"/setup?step=3&msg={_msg('Choose at least one preset or add a custom board.')}",
                status_code=303,
            )
        scan = {
            "keywords": _split_csv(scan_keywords),
            "exclude_keywords": _split_csv(scan_exclude),
            "max_new": max(0, int(max_new or 0)),
        }
        try:
            _write_setup_config(cfg, sources, scan)
        except (OSError, yaml.YAMLError, ValueError) as exc:
            return RedirectResponse(
                f"/setup?step=3&msg={_msg('Could not save sources: ' + str(exc))}",
                status_code=303,
            )
        return RedirectResponse(f"/setup?step=3&msg={_msg('Sources saved')}", status_code=303)

    @app.post("/setup/models")
    def setup_models(
        action: str = Form("skip"),
        provider_id: str = Form(""),
        key: str = Form(""),
        base_url: str = Form(""),
        model: str = Form(""),
    ):
        message = (
            "Model setup moved to Models. The deterministic workbench does not need a provider."
            if cfg.features.agent
            else "The agent feature is off; model setup is unavailable."
        )
        return RedirectResponse(
            f"/setup?step=3&msg={_msg(message)}", status_code=303
        )

    @app.post("/setup/finish")
    @locked_write
    def setup_finish(conn: sqlite3.Connection = Depends(get_conn)):
        # Finishing setup is an explicit initialization. If the human skipped
        # the profile upload, create the default profile now rather than on a
        # later read.
        services.ensure_profile(cfg, conn)
        if not setup_presets.source_board_count(cfg.sources or {}):
            sources = setup_presets.build_sources(SOURCE_MIXES["recommended"], {})
            try:
                _write_setup_config(cfg, sources, cfg.scan or {})
            except (OSError, yaml.YAMLError, ValueError) as exc:
                return RedirectResponse(
                    f"/setup?step=3&msg={_msg('Could not save default sources: ' + str(exc))}",
                    status_code=303,
                )
        db.setting_set(conn, "setup_complete", "1")
        db.log_event(conn, None, "setup", "completed")
        return RedirectResponse(
            f"/?msg={_msg('Setup complete. Run a scan to populate jobs.')}", status_code=303
        )

    @app.post("/setup/skip")
    @locked_write
    def setup_skip(conn: sqlite3.Connection = Depends(get_conn)):
        services.ensure_profile(cfg, conn)
        db.setting_set(conn, "setup_complete", "1")
        db.log_event(conn, None, "setup", "skipped")
        return RedirectResponse(
            f"/?msg={_msg('Setup skipped. You can return to it from the Setup link.')}",
            status_code=303,
        )

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_detail(job_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        ctx = context(request, conn)
        data = services.job_context(conn, job_id, ctx["profile"].id or 0, config=cfg)
        if not data:
            raise HTTPException(status_code=404, detail="Job not found")
        events = db.list_events(conn, data["application"]["id"]) if data.get("application") else []
        job_obj = db.get_job(conn, job_id)
        application_obj = (
            db.get_application(conn, data["application"]["id"])
            if data.get("application")
            else None
        )
        quality_report = (
            quality.evaluate(conn, job_obj, ctx["profile"], ctx["prefs"], application_obj)
            if job_obj
            else None
        )
        return templates.TemplateResponse(
            request=request,
            name="job_detail.html",
            context={
                **ctx,
                **data,
                "events": events,
                "quality": quality_report,
                "quality_ready": quality.is_ready(quality_report) if quality_report else False,
            },
        )

    @app.post("/scan/run")
    @locked_write
    def scan_run(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        stats = services.scan(cfg, conn)
        msg = f"Scan: {stats.get('new', 0)} new, {stats.get('seen', 0)} seen"
        if stats.get("filtered"):
            msg += f", {stats['filtered']} filtered"
        if stats.get("deferred"):
            msg += f", {stats['deferred']} deferred"
        per_source = stats.get("per_source") or {}
        if per_source:
            msg += " [" + ", ".join(f"{k}:{v}" for k, v in sorted(per_source.items())) + "]"
        if stats.get("errors"):
            msg += f", {len(stats['errors'])} errors"
        # A scan only inserts jobs. Score just the unscored ones so new rows
        # clear the dashboard/queue min_score filter without a second click.
        try:
            result = services.rescore(cfg, conn, only_unscored=True)
            msg += f", scored {result['scored']}"
        except Exception as exc:
            msg += f", score failed: {exc}"
        return RedirectResponse(f"/?msg={_msg(msg)}", status_code=303)

    @app.post("/score/run")
    @locked_write
    def score_run(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        result = services.rescore(cfg, conn)
        msg = f"Rescored {result['scored']} jobs"
        return RedirectResponse(f"/?msg={_msg(msg)}", status_code=303)

    @app.post("/jobs/{job_id}/tailor")
    @locked_write
    def tailor(job_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        try:
            result = services.tailor_job(cfg, conn, job_id)
            msg = f"Tailored resume: {result['pdf']}"
        except Exception as exc:
            msg = f"Tailor failed: {exc}"
        return RedirectResponse(f"/jobs/{job_id}?msg={_msg(msg)}", status_code=303)

    @app.post("/jobs/{job_id}/apply")
    @locked_write
    def apply(job_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        job = db.get_job(conn, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        if is_manual_only_url(job.apply_url or job.url):
            msg = (
                "Apply blocked: Indeed and LinkedIn are manual-only in JobForge. "
                "Open the posting and apply yourself."
            )
            return RedirectResponse(f"/jobs/{job_id}?msg={_msg(msg)}", status_code=303)
        profile, prefs = services.require_profile(cfg, conn)
        report = quality.evaluate(conn, job, profile, prefs)
        if not report.ready:
            msg = f"Apply blocked: {quality.QualityBlocked(report)}"
            return RedirectResponse(f"/jobs/{job_id}?msg={_msg(msg)}", status_code=303)
        try:
            pid = services.launch_apply_subprocess(
                cfg, job_id, hold_seconds=600, review_only=True
            )
            msg = f"Browser opening in a separate process (pid {pid}). Review and submit manually."
        except Exception as exc:
            msg = f"Apply failed: {exc}"
        return RedirectResponse(f"/jobs/{job_id}?msg={_msg(msg)}", status_code=303)

    @app.get("/queue", response_class=HTMLResponse)
    def queue_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        ctx = context(request, conn)
        queue = Queue(cfg.data_dir / "queue.json")
        return templates.TemplateResponse(
            request=request,
            name="queue.html",
            context={
                **ctx,
                "queue_summary": queue.summary(),
                "queue_items": queue.list_items(),
            },
        )

    @app.get("/autopilot", response_class=HTMLResponse)
    def autopilot_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        ctx = context(request, conn)
        queue = Queue(cfg.data_dir / "queue.json")
        allowed, reason, _policy = risk.can_submit_autopilot(cfg)
        return templates.TemplateResponse(
            request=request,
            name="autopilot.html",
            context={
                **ctx,
                "risk": risk.status(cfg),
                "can_submit": allowed,
                "submit_reason": reason,
                "digest": latest_digest(cfg),
                "queue_summary": queue.summary(),
            },
        )

    @app.get("/api/autopilot/status")
    def autopilot_status():
        return status_payload(cfg)

    @app.post("/autopilot/run")
    @locked_write
    def autopilot_run(
        limit: int = Form(5),
        min_score: float | None = Form(None),
        scan: str = Form(""),
        score: str = Form(""),
        dry_run: str = Form(""),
        mode: str = Form(""),
    ):
        dry = (mode != "live") if mode else bool(dry_run)
        if not dry:
            allowed, reason, _policy = risk.can_submit_autopilot(cfg)
            if not allowed:
                return RedirectResponse(
                    f"/autopilot?msg={_msg(reason)}", status_code=303
                )
        try:
            pid = launch_autopilot(
                cfg,
                limit=max(1, min(10, int(limit or 5))),
                min_score=min_score,
                scan=bool(scan),
                score=bool(score),
                dry_run=dry,
            )
            mode = "dry run" if dry else "autopilot run"
            message = f"Autopilot {mode} started (pid {pid}). Refresh for the digest."
        except Exception as exc:  # noqa: BLE001
            message = f"Could not start autopilot: {exc}"
        return RedirectResponse(f"/autopilot?msg={_msg(message)}", status_code=303)

    @app.post("/jobs/{job_id}/save")
    @locked_write
    def save_job(job_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        profile, _ = services.require_profile(cfg, conn)
        db.set_saved(conn, job_id, profile.id or 0, True)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/hide")
    @locked_write
    def hide_job(job_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        profile, _ = services.require_profile(cfg, conn)
        db.set_hidden(conn, job_id, profile.id or 0, True)
        return RedirectResponse("/jobs", status_code=303)

    @app.get("/applications", response_class=HTMLResponse)
    def applications(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        ctx = context(request, conn)
        rows = db.list_applications(conn, ctx["profile"].id, limit=300)
        return templates.TemplateResponse(
            request=request,
            name="applications.html",
            context={**ctx, "apps": rows, "statuses": [s.value for s in ApplicationStatus]},
        )

    @app.post("/applications/{app_id}/status")
    @locked_write
    def update_status(
        app_id: int,
        request: Request,
        status: str = Form(...),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        valid = {s.value for s in ApplicationStatus}
        if status not in valid:
            raise HTTPException(status_code=400, detail="Invalid status")
        profile, _prefs = services.read_profile(cfg, conn)
        application = db.get_application(conn, app_id)
        if (
            profile is None
            or application is None
            or int(application.profile_id or 0) != int(profile.id or 0)
        ):
            raise HTTPException(status_code=404, detail="Application not found")
        fields: dict[str, Any] = {"status": status}
        if status == ApplicationStatus.SUBMITTED.value and not application.submitted_at:
            fields["submitted_at"] = db.utcnow()
        db.update_application(conn, app_id, **fields)
        db.log_event(conn, app_id, "status", status)
        return RedirectResponse("/applications", status_code=303)

    def _operation_view(conn: sqlite3.Connection, operation: dict[str, Any]) -> dict[str, Any]:
        job = None
        job_id = int((operation.get("payload") or {}).get("job_id") or 0)
        if job_id:
            job = db.get_job(conn, job_id)
        result = operation.get("result") or {}
        artifact_url = (
            f"/artifacts/{int(result['application_id'])}/resume"
            if result.get("application_id")
            else ""
        )
        return {
            "operation": operation,
            "job": job,
            "artifact_url": artifact_url,
        }

    @app.get("/operations", response_class=HTMLResponse)
    def operations_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        ctx = context(request, conn)
        rows = db.list_agent_operations(
            conn, profile_id=ctx["profile"].id, limit=50
        )
        operations = [_operation_view(conn, row) for row in rows]
        return templates.TemplateResponse(
            request=request,
            name="operations.html",
            context={**ctx, "operations": operations},
        )

    @app.get("/operations/{operation_id}", response_class=HTMLResponse)
    def operation_detail(
        operation_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)
    ):
        ctx = context(request, conn)
        operation = db.get_agent_operation(conn, operation_id)
        if operation is None or int(operation["profile_id"]) != int(ctx["profile"].id or 0):
            raise HTTPException(status_code=404, detail="Operation not found")
        return templates.TemplateResponse(
            request=request,
            name="operation.html",
            context={**ctx, **_operation_view(conn, operation)},
        )

    @app.post("/operations/{operation_id}/approve")
    def operation_approve(
        operation_id: str, conn: sqlite3.Connection = Depends(get_conn)
    ):
        before = db.get_agent_operation(conn, operation_id)
        profile, _prefs = services.read_profile(cfg, conn)
        if (
            before is None
            or profile is None
            or int(before["profile_id"]) != int(profile.id or 0)
        ):
            raise HTTPException(status_code=404, detail="Operation not found")
        if before["state"] == "pending_approval" and before["expires_at"] < db.utcnow():
            db.update_agent_operation(
                conn, operation_id, state="expired", finished_at=db.utcnow()
            )
            db.append_agent_operation_event(conn, operation_id, "expired", {})
            return RedirectResponse(
                f"/operations/{operation_id}?msg={_msg('This request expired before approval.')}",
                status_code=303,
            )
        with action_lock(cfg):
            operation = db.approve_agent_operation(conn, operation_id)
        if operation and operation.get("state") == "approved" and before.get("state") == "pending_approval":
            try:
                launch_worker(cfg, operation_id)
                message = "Approved. The resume worker is running."
            except Exception as exc:  # noqa: BLE001 - dispatch boundary
                db.update_agent_operation(
                    conn,
                    operation_id,
                    state="failed",
                    finished_at=db.utcnow(),
                    error_code="DEPENDENCY_MISSING",
                    error_message=f"Could not launch worker: {exc}"[:500],
                )
                db.append_agent_operation_event(conn, operation_id, "failed", {})
                message = "Approved, but the worker could not start."
        else:
            message = "Operation is no longer awaiting approval."
        return RedirectResponse(
            f"/operations/{operation_id}?msg={_msg(message)}", status_code=303
        )

    @app.post("/operations/{operation_id}/deny")
    def operation_deny(
        operation_id: str, conn: sqlite3.Connection = Depends(get_conn)
    ):
        existing = db.get_agent_operation(conn, operation_id)
        profile, _prefs = services.read_profile(cfg, conn)
        if (
            existing is None
            or profile is None
            or int(existing["profile_id"]) != int(profile.id or 0)
        ):
            raise HTTPException(status_code=404, detail="Operation not found")
        with action_lock(cfg):
            operation = db.deny_agent_operation(conn, operation_id)
        message = "Denied." if operation and operation.get("state") == "denied" else "Operation is no longer awaiting approval."
        return RedirectResponse(
            f"/operations/{operation_id}?msg={_msg(message)}", status_code=303
        )

    @app.get("/artifacts/{application_id}/screenshot")
    def artifact_screenshot(
        application_id: int, conn: sqlite3.Connection = Depends(get_conn)
    ):
        profile, _prefs = services.read_profile(cfg, conn)
        if profile is None:
            raise HTTPException(status_code=404, detail="Artifact not found")
        application = db.get_application(conn, application_id)
        if (
            application is None
            or int(application.profile_id or 0) != int(profile.id or 0)
            or not application.screenshot_path
        ):
            raise HTTPException(status_code=404, detail="Artifact not found")
        path = Path(application.screenshot_path).resolve()
        root = (Path(cfg.data_dir) / "screenshots").resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(status_code=404, detail="Artifact not found")
        return FileResponse(path)

    @app.get("/artifacts/{application_id}/resume")
    def artifact_resume(
        application_id: int, conn: sqlite3.Connection = Depends(get_conn)
    ):
        # Build the profile identity explicitly because this route has no
        # Request parameter.
        profile, _prefs = services.read_profile(cfg, conn)
        if profile is None:
            raise HTTPException(status_code=404, detail="Artifact not found")
        application = db.get_application(conn, application_id)
        if (
            application is None
            or int(application.profile_id or 0) != int(profile.id or 0)
            or not application.resume_path
        ):
            raise HTTPException(status_code=404, detail="Artifact not found")
        path = Path(application.resume_path).resolve()
        root = Path(cfg.output_dir).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(status_code=404, detail="Artifact not found")
        return FileResponse(
            path,
            media_type="application/pdf",
            filename=f"jobforge-{application.job_id}-resume.pdf",
        )

    @app.get("/profiles", response_class=HTMLResponse)
    def profiles(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        ctx = context(request, conn)
        rows = db.list_profiles(conn)
        return templates.TemplateResponse(
            request=request,
            name="profiles.html",
            context={
                **ctx,
                "profiles": rows,
                "github_connected": bool(db.get_connection(conn, "github")),
            },
        )

    @app.post("/profiles/import")
    @locked_write
    def import_profile(request: Request, file: UploadFile = File(...), conn: sqlite3.Connection = Depends(get_conn)):
        _import_profile_upload(cfg, conn, file)
        return RedirectResponse(f"/profiles?msg={_msg('Profile imported')}", status_code=303)

    @app.post("/profiles/{profile_id}/activate")
    @locked_write
    def activate_profile(profile_id: int, conn: sqlite3.Connection = Depends(get_conn)):
        db.set_active_profile(conn, profile_id)
        return RedirectResponse("/profiles", status_code=303)

    @app.get("/connections", response_class=HTMLResponse)
    def connections_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        ctx = context(request, conn)
        connection = db.get_connection(conn, "github")
        store = _github_store(cfg)
        imported = _github_imported(ctx["profile"])
        backend_labels = {
            "keyring": "OS keychain",
            "file": "local 0600 file",
            "env": "environment variable",
        }
        return templates.TemplateResponse(
            request=request,
            name="connections.html",
            context={
                **ctx,
                "connection": connection,
                "token_backend_label": backend_labels.get(
                    (connection or {}).get("token_backend", ""), "unknown"
                ),
                "repo_count": db.count_github_repos(conn, include_ignored=True),
                "imported_count": len(imported),
                "env_token": bool(store.env_token),
                "keyring_available": store.keyring_available,
            },
        )

    @app.post("/connections/github/connect")
    @locked_write
    def github_connect(
        token: str = Form(""),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        try:
            result = github_integration.connect_github(_github_store(cfg), conn, token)
            msg = (
                f"Connected @{result['account']['login']} · "
                f"{result['repositories']} repositories synced."
            )
            if result.get("warning"):
                msg += " " + str(result["warning"])
            return RedirectResponse(f"/connections/github?msg={_msg(msg)}", status_code=303)
        except github_integration.GitHubError as exc:
            return RedirectResponse(
                f"/connections?msg={_msg('GitHub connection failed: ' + str(exc))}",
                status_code=303,
            )
        except Exception as exc:  # pragma: no cover - defensive for transport bugs
            return RedirectResponse(
                f"/connections?msg={_msg('GitHub connection failed: ' + str(exc))}",
                status_code=303,
            )

    @app.post("/connections/github/sync")
    @locked_write
    def github_sync(conn: sqlite3.Connection = Depends(get_conn)):
        try:
            result = github_integration.sync_github(_github_store(cfg), conn)
            msg = (
                f"Synced @{result['account']['login']} · "
                f"{result['repositories']} repositories."
            )
            return RedirectResponse(f"/connections/github?msg={_msg(msg)}", status_code=303)
        except github_integration.GitHubError as exc:
            return RedirectResponse(
                f"/connections?msg={_msg('GitHub sync failed: ' + str(exc))}",
                status_code=303,
            )
        except Exception as exc:  # pragma: no cover - defensive for transport bugs
            return RedirectResponse(
                f"/connections?msg={_msg('GitHub sync failed: ' + str(exc))}",
                status_code=303,
            )

    @app.post("/connections/github/disconnect")
    @locked_write
    def github_disconnect(conn: sqlite3.Connection = Depends(get_conn)):
        github_integration.disconnect_github(_github_store(cfg), conn)
        return RedirectResponse(
            f"/connections?msg={_msg('GitHub disconnected. Imported resume projects were kept.')}",
            status_code=303,
        )

    @app.get("/connections/github", response_class=HTMLResponse)
    def github_repos_page(
        request: Request,
        q: str = "",
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        ctx = context(request, conn)
        connection = db.get_connection(conn, "github")
        if not connection:
            return RedirectResponse(
                f"/connections?msg={_msg('Connect GitHub to browse repositories.')}",
                status_code=303,
            )
        repos = db.list_github_repos(conn, q)
        imported = _github_imported(ctx["profile"])
        for repo in repos:
            repo["imported"] = int(repo["repo_id"]) in imported
        return templates.TemplateResponse(
            request=request,
            name="connections_github.html",
            context={
                **ctx,
                "connection": connection,
                "repos": repos,
                "q": q,
                "imported_count": len(imported),
            },
        )

    @app.get("/connections/github/repos/{repo_id}", response_class=HTMLResponse)
    def github_repo_detail(
        repo_id: int,
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        ctx = context(request, conn)
        if not db.get_connection(conn, "github"):
            return RedirectResponse(
                f"/connections?msg={_msg('Connect GitHub to review repositories.')}",
                status_code=303,
            )
        repo = db.get_github_repo(conn, repo_id)
        if not repo:
            raise HTTPException(status_code=404, detail="GitHub repository not found")
        context_error = ""
        if not repo.get("readme") and not repo.get("languages"):
            try:
                repo = github_integration.refresh_repo_context(
                    _github_store(cfg), conn, repo
                )
            except github_integration.GitHubError as exc:
                context_error = str(exc)
        project = github_integration.find_repo_project(ctx["profile"], repo_id)
        if project:
            title = str(project.get("title") or "")
            tech_line = str(project.get("tech_line") or "")
            bullets = "\n".join(str(item) for item in project.get("bullets") or [])
            tags = ", ".join(str(item) for item in project.get("tags") or [])
        else:
            title = str(repo.get("name") or "")
            tech_line = str(repo.get("tech_line") or "")
            bullets = "\n".join(str(item) for item in repo.get("suggested_bullets") or [])
            tags = ", ".join(str(item) for item in repo.get("tags") or [])
        return templates.TemplateResponse(
            request=request,
            name="github_repo.html",
            context={
                **ctx,
                "connection": db.get_connection(conn, "github"),
                "repo": repo,
                "project": project,
                "suggested": not bool(project),
                "title_value": title,
                "tech_line_value": tech_line,
                "bullets_value": bullets,
                "tags_value": tags,
                "readme_excerpt": (repo.get("readme") or "")[:14000],
                "context_error": context_error,
            },
        )

    @app.post("/connections/github/repos/{repo_id}/import")
    @locked_write
    def github_repo_import(
        repo_id: int,
        title: str = Form(""),
        tech_line: str = Form(""),
        bullets: str = Form(""),
        tags: str = Form(""),
        add_skills: str = Form(""),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        repo = db.get_github_repo(conn, repo_id)
        if not repo:
            raise HTTPException(status_code=404, detail="GitHub repository not found")
        profile, _ = services.require_profile(cfg, conn)
        if not title.strip():
            return RedirectResponse(
                f"/connections/github/repos/{repo_id}?msg={_msg('Project name is required.')}",
                status_code=303,
            )
        try:
            project, added = github_integration.import_repo_project(
                profile,
                repo,
                title=title,
                tech_line=tech_line,
                bullets=_split_lines(bullets),
                tags=[item.strip() for item in tags.replace("\n", ",").split(",") if item.strip()],
                add_skills=bool(add_skills),
            )
            db.save_profile(conn, profile)
            db.log_event(
                conn,
                None,
                "github_project_imported",
                f"{repo.get('full_name', '')} -> {project.get('title', '')}",
            )
            msg = f"Added {project.get('title', 'project')} to the project bank."
            if added:
                msg += f" Detected {len(added)} new skill(s): {', '.join(added[:8])}."
            if added:
                try:
                    result = services.rescore(cfg, conn)
                    msg += f" Rescored {result['scored']} jobs."
                except Exception as exc:  # pragma: no cover - scoring is deterministic
                    msg += f" Rescore failed: {exc}"
        except Exception as exc:
            msg = f"Could not add project: {exc}"
        return RedirectResponse(
            f"/connections/github/repos/{repo_id}?msg={_msg(msg)}", status_code=303
        )

    @app.post("/connections/github/repos/{repo_id}/remove")
    @locked_write
    def github_repo_remove(repo_id: int, conn: sqlite3.Connection = Depends(get_conn)):
        profile, _ = services.require_profile(cfg, conn)
        removed = github_integration.remove_repo_project(profile, repo_id)
        if removed:
            db.save_profile(conn, profile)
            db.log_event(conn, None, "github_project_removed", f"repo:{repo_id}")
        msg = "Removed from the project bank." if removed else "That project is not in the bank."
        return RedirectResponse(
            f"/connections/github/repos/{repo_id}?msg={_msg(msg)}", status_code=303
        )

    @app.post("/connections/github/repos/{repo_id}/refresh")
    @locked_write
    def github_repo_refresh(repo_id: int, conn: sqlite3.Connection = Depends(get_conn)):
        repo = db.get_github_repo(conn, repo_id)
        if not repo:
            raise HTTPException(status_code=404, detail="GitHub repository not found")
        try:
            github_integration.refresh_repo_context(_github_store(cfg), conn, repo)
            msg = "README, languages, and suggested bullets refreshed."
        except github_integration.GitHubError as exc:
            msg = f"Refresh failed: {exc}"
        return RedirectResponse(
            f"/connections/github/repos/{repo_id}?msg={_msg(msg)}", status_code=303
        )

    @app.post("/settings/features")
    @locked_write
    def save_feature_flags(
        setup_wizard: str = Form(""),
        github_connections: str = Form(""),
        scoring_v2: str = Form(""),
        agent: str = Form(""),
        agent_integrations: str = Form(""),
    ):
        flags = FeatureFlags(
            setup_wizard=bool(setup_wizard),
            github_connections=bool(github_connections),
            scoring_v2=bool(scoring_v2),
            agent=bool(agent),
            agent_integrations=bool(agent_integrations),
        )
        try:
            _write_feature_config(cfg, flags)
            msg = "Feature flags saved."
        except (OSError, yaml.YAMLError, ValueError) as exc:
            msg = f"Could not save feature flags: {exc}"
        return RedirectResponse(f"/settings?msg={_msg(msg)}", status_code=303)

    @app.get("/settings/risk", response_class=HTMLResponse)
    def risk_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        ctx = context(request, conn)
        return templates.TemplateResponse(
            request=request,
            name="risk.html",
            context={**ctx, "risk": risk.status(cfg)},
        )

    @app.post("/settings/risk")
    @locked_write
    def risk_update(
        request: Request,
        action: str = Form(...),
        flag: str = Form(""),
        acknowledge: str = Form(""),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        try:
            if action == "enable":
                risk.enable(cfg, flag, acknowledge, conn=conn)
                msg = f"Enabled {flag}. You are responsible for every submission."
            elif action == "disable":
                risk.disable(cfg, flag, conn=conn)
                msg = f"Disabled {flag}."
            else:
                raise risk.RiskError("Unknown risk action")
        except risk.RiskError as exc:
            msg = str(exc)
        return RedirectResponse(f"/settings/risk?msg={_msg(msg)}", status_code=303)

    @app.get("/settings", response_class=HTMLResponse)
    def settings(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        ctx = context(request, conn)
        boards = {name: configured_boards(cfg, name) for name in SOURCES}
        return templates.TemplateResponse(
            request=request,
            name="settings.html",
            context={
                **ctx,
                "boards": boards,
                "llm_ready": ctx["agent_ready"],
                "email_ready": cfg.email.enabled,
                "features": cfg.features,
                "active_provider": ctx["active_provider"],
                "active_model": ctx["active_model"],
            },
        )

    @app.post("/settings")
    @locked_write
    def save_settings(
        request: Request,
        titles: str = Form(""),
        locations: str = Form(""),
        work_modes: str = Form(""),
        employment_types: str = Form(""),
        experience_levels: str = Form(""),
        salary_min: int = Form(0),
        min_match: int = Form(55),
        needs_sponsorship: str = Form(""),
        open_to_relocation: str = Form(""),
        excluded_companies: str = Form(""),
        excluded_keywords: str = Form(""),
        included_keywords: str = Form(""),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        profile, _ = services.require_profile(cfg, conn)
        prefs = _preferences_from_form(
            titles=titles,
            locations=locations,
            work_modes=work_modes,
            employment_types=employment_types,
            experience_levels=experience_levels,
            salary_min=salary_min,
            min_match=min_match,
            needs_sponsorship=needs_sponsorship,
            open_to_relocation=open_to_relocation,
            excluded_companies=excluded_companies,
            excluded_keywords=excluded_keywords,
            included_keywords=included_keywords,
        )
        db.save_preferences(conn, profile.id or 0, prefs)
        services.rescore(cfg, conn)
        return RedirectResponse(f"/settings?msg={_msg('Preferences saved and jobs rescored')}", status_code=303)

    @app.get("/inbox", response_class=HTMLResponse)
    def inbox(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        ctx = context(request, conn)
        rows = db.list_emails(conn, 200)
        return templates.TemplateResponse(request=request, name="inbox.html", context={**ctx, "emails": rows})

    @app.post("/email/poll")
    @locked_write
    def email_poll(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        try:
            from ..email.imap import poll

            result = poll(cfg.email, conn)
            msg = f"Email: {result['fetched']} fetched, {result['saved']} saved"
        except Exception as exc:
            msg = f"Email poll failed: {exc}"
        return RedirectResponse(f"/inbox?msg={_msg(msg)}", status_code=303)

    @app.get("/runs", response_class=HTMLResponse)
    def runs(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        ctx = context(request, conn)
        return templates.TemplateResponse(
            request=request, name="runs.html", context={**ctx, "runs": db.list_runs(conn, 100)}
        )

    @app.post("/export")
    @locked_write
    def export(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        path = services.export_data(cfg, conn)
        return RedirectResponse(f"/settings?msg={_msg(f'Exported to {path}')}", status_code=303)

    @app.get("/api/jobs")
    def api_jobs(conn: sqlite3.Connection = Depends(get_conn)):
        profile, prefs = services.require_profile(cfg, conn)
        return db.list_jobs(
            conn, profile.id, one_per_company=prefs.one_application_per_company, limit=500
        )

    @app.get("/api/status")
    def api_status(conn: sqlite3.Connection = Depends(get_conn)):
        profile, _ = services.require_profile(cfg, conn)
        return db.stats(conn, profile.id)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/auth")
    def auth(token: str = ""):
        if cfg.server.token and secrets.compare_digest(token, cfg.server.token):
            response = RedirectResponse("/", status_code=303)
            response.set_cookie("jf_token", token, httponly=True, samesite="strict")
            return response
        raise HTTPException(status_code=401, detail="Invalid token")

    return app


def run(config: Config | None = None, host: str = "127.0.0.1", port: int = 8765) -> None:
    app = create_app(config)
    uvicorn.run(app, host=host, port=port, log_level="info")
