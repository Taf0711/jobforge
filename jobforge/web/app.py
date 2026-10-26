"""FastAPI dashboard. Local-first: binds to 127.0.0.1 by default."""

from __future__ import annotations

import secrets
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Generator
from urllib.parse import urlsplit

import uvicorn
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import db, quality, risk, services
from ..automation.base import is_manual_only_url
from ..config import Config, load_config
from ..matching.resume import load_profile_file
from ..models import ApplicationStatus, Preferences
from ..queue import Queue
from ..sources.registry import SOURCES, configured_boards
from .answers_routes import create_answers_router

WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
templates.env.filters["money"] = lambda v: f"${int(v):,}" if v else ""
templates.env.filters["pct"] = lambda v: f"{float(v or 0) * 100:.0f}%"
templates.env.filters["short"] = lambda v, n=140: (str(v or "")[:n] + ("..." if len(str(v or "")) > n else ""))


def _msg(text: str) -> str:
    from urllib.parse import quote

    return quote(text, safe="")


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or load_config()
    db.init_db(cfg.db_path)

    app = FastAPI(title="JobForge", docs_url="/api/docs", redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
    app.include_router(create_answers_router(cfg))

    def get_conn() -> Generator[sqlite3.Connection, None, None]:
        conn = db.connect(cfg.db_path)
        try:
            yield conn
        finally:
            conn.close()

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            host = request.headers.get("host", "")
            if origin:
                parsed = urlsplit(origin)
                if not host or parsed.scheme != request.url.scheme or parsed.netloc != host:
                    return JSONResponse({"error": "cross-origin request blocked"}, status_code=403)

        # The optional bearer token protects every route except the health check
        # and the token login endpoint. This covers unsafe writes and the
        # answer-bank GETs, which can expose salary/authorization answers.
        if cfg.server.token and request.url.path not in {"/healthz", "/auth"}:
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

    def context(request: Request, conn: sqlite3.Connection) -> dict[str, Any]:
        profile, prefs = services.ensure_profile(cfg, conn)
        return {
            "request": request,
            "profile": profile,
            "prefs": prefs,
            "cfg": cfg,
            "msg": request.query_params.get("msg", ""),
        }

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        ctx = context(request, conn)
        profile = ctx["profile"]
        stats = db.stats(conn, profile.id)
        jobs = db.list_jobs(
            conn,
            profile.id,
            min_score=ctx["prefs"].min_match,
            one_per_company=ctx["prefs"].one_application_per_company,
            limit=10,
        )
        graph = services.load_network(cfg)
        if graph:
            for job in jobs:
                warm = services.warm_summary(
                    graph, job["company"], float(job.get("score") or 0)
                )
                job["warm_channel"] = warm["channel"]
                job["warm_score"] = warm["score"]
        apps = db.list_applications(conn, profile.id, limit=8)
        runs = db.list_runs(conn, 6)
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={**ctx, "stats": stats, "jobs": jobs, "apps": apps, "runs": runs},
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
    def score_run(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        result = services.rescore(cfg, conn)
        msg = f"Rescored {result['scored']} jobs"
        return RedirectResponse(f"/?msg={_msg(msg)}", status_code=303)

    @app.post("/jobs/{job_id}/tailor")
    def tailor(job_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        try:
            result = services.tailor_job(cfg, conn, job_id)
            msg = f"Tailored resume: {result['pdf']}"
        except Exception as exc:
            msg = f"Tailor failed: {exc}"
        return RedirectResponse(f"/jobs/{job_id}?msg={_msg(msg)}", status_code=303)

    @app.post("/jobs/{job_id}/apply")
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
        profile, prefs = services.ensure_profile(cfg, conn)
        report = quality.evaluate(conn, job, profile, prefs)
        if not report.ready:
            msg = f"Apply blocked: {quality.QualityBlocked(report)}"
            return RedirectResponse(f"/jobs/{job_id}?msg={_msg(msg)}", status_code=303)
        if cfg.browser.allow_submit:
            allowed, reason, _policy = risk.can_submit_unattended(cfg)
            if not allowed:
                return RedirectResponse(
                    f"/jobs/{job_id}?msg={_msg('Apply blocked: ' + reason)}", status_code=303
                )
        try:
            pid = services.launch_apply_subprocess(cfg, job_id, hold_seconds=600)
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

    @app.post("/jobs/{job_id}/save")
    def save_job(job_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        profile, _ = services.ensure_profile(cfg, conn)
        db.set_saved(conn, job_id, profile.id or 0, True)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/hide")
    def hide_job(job_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        profile, _ = services.ensure_profile(cfg, conn)
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
    def update_status(
        app_id: int,
        request: Request,
        status: str = Form(...),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        valid = {s.value for s in ApplicationStatus}
        if status not in valid:
            raise HTTPException(status_code=400, detail="Invalid status")
        db.update_application(conn, app_id, status=status)
        db.log_event(conn, app_id, "status", status)
        return RedirectResponse("/applications", status_code=303)

    @app.get("/profiles", response_class=HTMLResponse)
    def profiles(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        ctx = context(request, conn)
        rows = db.list_profiles(conn)
        return templates.TemplateResponse(
            request=request, name="profiles.html", context={**ctx, "profiles": rows}
        )

    @app.post("/profiles/import")
    def import_profile(request: Request, file: UploadFile = File(...), conn: sqlite3.Connection = Depends(get_conn)):
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
        if suffix in {".pdf", ".docx", ".txt", ".md"}:
            services.refresh_profile_from_resume(cfg, conn, dest)
        else:
            profile = load_profile_file(dest)
            profile.id = db.save_profile(conn, profile)
            db.set_active_profile(conn, profile.id)
        return RedirectResponse(f"/profiles?msg={_msg('Profile imported')}", status_code=303)

    @app.post("/profiles/{profile_id}/activate")
    def activate_profile(profile_id: int, conn: sqlite3.Connection = Depends(get_conn)):
        db.set_active_profile(conn, profile_id)
        return RedirectResponse("/profiles", status_code=303)

    @app.get("/settings/risk", response_class=HTMLResponse)
    def risk_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        ctx = context(request, conn)
        return templates.TemplateResponse(
            request=request,
            name="risk.html",
            context={**ctx, "risk": risk.status(cfg)},
        )

    @app.post("/settings/risk")
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
            context={**ctx, "boards": boards, "llm_ready": cfg.llm.configured, "email_ready": cfg.email.enabled},
        )

    @app.post("/settings")
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
        profile, _ = services.ensure_profile(cfg, conn)
        def split(value: str) -> list[str]:
            return [item.strip() for item in value.split(",") if item.strip()]
        prefs = Preferences(
            titles=split(titles)[:5],
            locations=split(locations),
            work_modes=split(work_modes),
            employment_types=split(employment_types),
            experience_levels=split(experience_levels),
            salary_min=salary_min,
            min_match=max(0, min(100, min_match)),
            needs_sponsorship=bool(needs_sponsorship),
            open_to_relocation=bool(open_to_relocation),
            excluded_companies=split(excluded_companies),
            excluded_keywords=split(excluded_keywords),
            included_keywords=split(included_keywords),
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
    def export(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        path = services.export_data(cfg, conn)
        return RedirectResponse(f"/settings?msg={_msg(f'Exported to {path}')}", status_code=303)

    @app.get("/api/jobs")
    def api_jobs(conn: sqlite3.Connection = Depends(get_conn)):
        profile, prefs = services.ensure_profile(cfg, conn)
        return db.list_jobs(
            conn, profile.id, one_per_company=prefs.one_application_per_company, limit=500
        )

    @app.get("/api/status")
    def api_status(conn: sqlite3.Connection = Depends(get_conn)):
        profile, _ = services.ensure_profile(cfg, conn)
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
