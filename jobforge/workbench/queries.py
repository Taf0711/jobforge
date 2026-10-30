"""Pure read-only queries and external-safe projections for the workbench."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .. import db, quality
from ..locking import action_lock
from ..models import Profile
from .actions import ACTION_TOOLS, dispatch as dispatch_action
from .authorization import QueryContext, require_scope
from .contracts import (
    MAX_DESCRIPTION_CHARS,
    TOOL_SPECS,
    ToolError,
    check_envelope_size,
    envelope,
    error_envelope,
    request_id,
    validate_tool_input,
)

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+\d[\d\s().-]{7,}\d|\(\d{3}\)\s*\d{3}[-. ]\d{4})(?!\d)")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _digest(parts: list[Any]) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _redact_contact(text: str) -> str:
    value = _EMAIL_RE.sub("[redacted email]", str(text or ""))
    value = _PHONE_RE.sub("[redacted phone]", value)
    return _CONTROL_RE.sub("", value)


def _clean(text: Any, limit: int = 2_000) -> str:
    value = _CONTROL_RE.sub("", str(text or ""))
    return value[:limit]


def _safe_url(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    if parsed.username or parsed.password:
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _encode_cursor(offset: int, revision: str) -> str:
    raw = json.dumps({"o": offset, "r": revision}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str, revision: str) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        raise ToolError("INVALID_ARGUMENT", "Cursor is invalid.") from None
    if not isinstance(data, dict) or data.get("r") != revision:
        raise ToolError("STALE_CONTEXT", "The data changed; fetch the first page again.")
    offset = data.get("o")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ToolError("INVALID_ARGUMENT", "Cursor is invalid.")
    return offset


def _revision(*parts: Any) -> str:
    return _digest(list(parts))


def _search_revision(conn: sqlite3.Connection, profile_id: int) -> str:
    jobs = conn.execute("SELECT COUNT(*) AS n, MAX(id) AS max_id FROM jobs").fetchone()
    matches = conn.execute(
        "SELECT COUNT(*) AS n, MAX(scored_at) AS max_scored FROM matches WHERE profile_id=?",
        (profile_id,),
    ).fetchone()
    apps = conn.execute(
        "SELECT COUNT(*) AS n, MAX(updated_at) AS max_updated FROM applications WHERE profile_id=?",
        (profile_id,),
    ).fetchone()
    return _revision(
        "jobs", jobs["n"], jobs["max_id"],
        "matches", matches["n"], matches["max_scored"],
        "applications", apps["n"], apps["max_updated"],
    )


def _applications_revision(conn: sqlite3.Connection, profile_id: int) -> str:
    row = conn.execute(
        "SELECT COUNT(*) AS n, MAX(updated_at) AS max_updated FROM applications WHERE profile_id=?",
        (profile_id,),
    ).fetchone()
    return _revision("applications", profile_id, row["n"], row["max_updated"])


def _profile_revision(profile: Profile) -> str:
    return _revision("profile", profile.to_dict())


def _job_status(conn: sqlite3.Connection, profile_id: int, job_id: int) -> str:
    row = conn.execute(
        "SELECT status FROM applications WHERE job_id=? AND profile_id=?",
        (job_id, profile_id),
    ).fetchone()
    return str(row["status"]) if row else ""


def _match_age(scored_at: str | None) -> dict[str, Any]:
    return {"scored_at": scored_at or None}


def _sanitize_detail(context: QueryContext, detail: str) -> str:
    value = _clean(detail, 500)
    for path in (
        context.profile.resume_path,
        str(Path(context.config.data_dir).parent),
        str(context.config.data_dir),
        str(context.config.output_dir),
    ):
        if path:
            value = value.replace(path, "[local path]")
    return _redact_contact(value)


def _sanitize_url_list(url: str) -> str:
    return _safe_url(url)


def tool_list(context: QueryContext) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name, spec in TOOL_SPECS.items():
        scope = spec["scope"]
        out.append(
            {
                "name": name,
                "description": spec["description"],
                "scope": scope,
                "scopes": list(spec.get("required_scopes") or []),
                "available": all(
                    item in context.scopes for item in (spec.get("required_scopes") or [])
                ),
                "input_schema": spec["input_schema"],
                "annotations": {
                    "readOnlyHint": bool(spec.get("read_only", True)),
                    "destructiveHint": bool(spec.get("destructive", False)),
                    "idempotentHint": bool(spec.get("idempotent", True)),
                    "openWorldHint": False,
                },
            }
        )
    return out


def _status(context: QueryContext) -> dict[str, Any]:
    from importlib.metadata import PackageNotFoundError, version

    try:
        app_version = version("jobforge")
    except PackageNotFoundError:
        app_version = "0.1.0-dev"
    typst_ready = bool(shutil.which("typst"))
    browser_ready = _browser_ready(context.config)
    setup_complete = db.setting_get(context.conn, "setup_complete") == "1"
    warnings: list[str] = []
    if not typst_ready:
        warnings.append("Typst is not installed; resume rendering is unavailable.")
    if not browser_ready:
        warnings.append("No supported browser was found; review fill is unavailable.")
    return {
        "app_version": app_version,
        "instance": {
            "id": context.instance_id,
            "label": _clean(context.instance.get("label"), 80),
        },
        "connection": {
            "id": context.connection_id,
            "label": _clean(context.connection.get("label"), 120),
            "client": _clean(context.connection.get("client"), 40),
            "scopes": sorted(context.scopes),
            "generation": int(context.connection.get("generation") or 1),
        },
        "profile": {
            "id": context.profile_id,
            "label": _clean(context.profile.name, 120),
        },
        "interface_version": "1",
        "schema_version": "1",
        "db_schema_version": db.SCHEMA_VERSION,
        "setup": {
            "complete": setup_complete,
            "profile_ready": bool(context.profile.resume_path or context.profile.facts or context.profile.experience),
            "typst_ready": typst_ready,
            "browser_ready": browser_ready,
        },
        "capabilities": {
            "read_only": True,
            "proposals": False,
            "browser_actions": False,
            "submission": False,
        },
        "warnings": warnings,
        "links": [{"rel": "dashboard", "path": "/"}],
    }


def _browser_ready(config: Any) -> bool:
    channel = getattr(config.browser, "channel", "") if config else ""
    if channel and shutil.which(channel):
        return True
    if shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chrome"):
        return True
    candidates = [
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        Path.home() / "Library/Caches/ms-playwright",
        Path.home() / ".cache/ms-playwright",
    ]
    for candidate in candidates:
        try:
            if candidate.is_file():
                return True
            if candidate.is_dir() and any(candidate.glob("chromium-*")):
                return True
        except OSError:
            continue
    return False


def _search_jobs(context: QueryContext, params: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    revision = _search_revision(context.conn, context.profile_id)
    offset = _decode_cursor(params["cursor"], revision)
    where = ["h.job_id IS NULL"]
    values: list[Any] = []
    if params["source"]:
        where.append("j.source=?")
        values.append(params["source"])
    else:
        where.append("j.source <> 'resume-forge'")
    if params["query"]:
        where.append("(j.title LIKE ? OR j.company LIKE ? OR j.description LIKE ?)")
        like = f"%{params['query']}%"
        values.extend([like, like, like])
    if params["min_fit"] is not None:
        where.append("m.score IS NOT NULL AND m.score >= ?")
        values.append(params["min_fit"])
    sql = f"""
        SELECT j.id AS job_id, j.company, j.title, j.location, j.remote, j.source,
               j.posted_at, m.score, m.scored_at, m.meta_json,
               a.status AS application_status
        FROM jobs j
        LEFT JOIN matches m ON m.job_id=j.id AND m.profile_id=?
        LEFT JOIN applications a ON a.job_id=j.id AND a.profile_id=?
        LEFT JOIN hidden_jobs h ON h.job_id=j.id AND h.profile_id=?
        WHERE {' AND '.join(where)}
        ORDER BY (m.score IS NULL) ASC, m.score DESC, j.id DESC
        LIMIT ? OFFSET ?
    """
    params_sql = [context.profile_id, context.profile_id, context.profile_id, *values, params["limit"] + 1, offset]
    rows = context.conn.execute(sql, params_sql).fetchall()
    more = len(rows) > params["limit"]
    rows = rows[: params["limit"]]
    jobs = []
    for row in rows:
        meta = json.loads(row["meta_json"] or "{}") if row["meta_json"] else {}
        pursuit = meta.get("pursuit")
        jobs.append(
            {
                "job_id": int(row["job_id"]),
                "company": _clean(row["company"], 200),
                "title": _clean(row["title"], 200),
                "location": _clean(row["location"], 200),
                "remote": bool(row["remote"]),
                "source": _clean(row["source"], 40),
                "posted_at": row["posted_at"] or None,
                "fit": round(float(row["score"]), 1) if row["score"] is not None else None,
                "pursuit": round(float(pursuit), 1) if pursuit is not None else None,
                "scored_at": row["scored_at"] or None,
                "application_status": str(row["application_status"] or ""),
            }
        )
    data = {
        "jobs": jobs,
        "next_cursor": _encode_cursor(offset + params["limit"], revision) if more else None,
        "dataset_revision": revision,
    }
    return data, [{"rel": "dashboard", "path": "/jobs"}]


def _get_job(context: QueryContext, params: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    job = db.get_job(context.conn, int(params["job_id"]))
    if job is None:
        raise ToolError("NOT_FOUND", "Job not found.")
    match = db.get_match(context.conn, int(params["job_id"]), context.profile_id)
    scored = context.conn.execute(
        "SELECT scored_at FROM matches WHERE job_id=? AND profile_id=?",
        (job.id, context.profile_id),
    ).fetchone()
    row = {
        "job_id": int(job.id or 0),
        "company": _clean(job.company, 200),
        "title": _clean(job.title, 200),
        "location": _clean(job.location, 200),
        "remote": bool(job.remote),
        "source": _clean(job.source, 40),
        "posted_at": job.posted_at,
        "discovered_at": job.discovered_at,
        "salary_min": job.salary_min,
        "salary_max": job.salary_max,
        "salary_currency": _clean(job.salary_currency, 8),
        "url": _sanitize_url_list(job.url),
        "apply_url": _sanitize_url_list(job.apply_url),
        "fit": round(float(match.score), 1) if match else None,
        "coverage": round(float(match.coverage), 3) if match else None,
        "gate_survival": round(float(match.gate_survival), 3) if match else None,
        "scored_at": scored["scored_at"] if scored else None,
        "application_status": _job_status(context.conn, context.profile_id, int(job.id or 0)),
    }
    if params["include_description"]:
        text = _clean(job.description, MAX_DESCRIPTION_CHARS + 1)
        row["description"] = text[:MAX_DESCRIPTION_CHARS]
        row["description_truncated"] = len(text) > MAX_DESCRIPTION_CHARS
        row["description_untrusted"] = True
    return row, [{"rel": "dashboard", "path": f"/jobs/{job.id}"}]


def _explain_fit(context: QueryContext, params: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    job = db.get_job(context.conn, int(params["job_id"]))
    if job is None:
        raise ToolError("NOT_FOUND", "Job not found.")
    match = db.get_match(context.conn, int(params["job_id"]), context.profile_id)
    link = [{"rel": "dashboard", "path": f"/jobs/{job.id}"}]
    if match is None:
        return {
            "job_id": int(job.id or 0),
            "fit": None,
            "coverage": None,
            "gate_survival": None,
            "reasons": [],
            "gaps": [],
            "requirements": [],
            "model_version": None,
            "scored_at": None,
            "stale": True,
            "message": "This job has not been scored for the bound profile.",
        }, link
    scored = context.conn.execute(
        "SELECT scored_at FROM matches WHERE job_id=? AND profile_id=?",
        (job.id, context.profile_id),
    ).fetchone()
    requirements = []
    for item in (match.requirements or [])[:40]:
        if not isinstance(item, dict):
            continue
        requirements.append(
            {
                "requirement": _clean(item.get("requirement"), 300),
                "skill": _clean(item.get("skill"), 80) or None,
                "importance": _clean(item.get("importance"), 20),
                "credit": item.get("credit"),
                "weight": item.get("weight"),
                "matched_by": _clean(item.get("matched_by"), 120),
                "gap": bool(item.get("gap")),
            }
        )
    return {
        "job_id": int(job.id or 0),
        "fit": round(float(match.score), 1),
        "coverage": round(float(match.coverage), 3),
        "gate_survival": round(float(match.gate_survival), 3),
        "reasons": [_clean(item, 300) for item in (match.reasons or [])[:8]],
        "gaps": [_clean(item, 300) for item in (match.gaps or [])[:10]],
        "requirements": requirements,
        "model_version": _clean(match.model_version, 80),
        "scored_at": scored["scored_at"] if scored else None,
        "stale": False,
    }, link


def _get_readiness(context: QueryContext, params: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    job = db.get_job(context.conn, int(params["job_id"]))
    if job is None:
        raise ToolError("NOT_FOUND", "Job not found.")
    app_row = context.conn.execute(
        "SELECT id, status, resume_path FROM applications WHERE job_id=? AND profile_id=?",
        (job.id, context.profile_id),
    ).fetchone()
    report = quality.evaluate(
        context.conn,
        job,
        context.profile,
        context.prefs,
        require_prepared=False,
    )
    blockers = [
        {"name": check.name, "detail": _sanitize_detail(context, check.detail)}
        for check in quality.blocking_checks(report)
    ]
    warnings = [
        {"name": check.name, "detail": _sanitize_detail(context, check.detail)}
        for check in report.checks
        if not check.passed and check.severity != quality.BLOCKER
    ]
    resume_state = "not_prepared"
    if app_row and app_row["resume_path"]:
        resume_state = "attached" if Path(str(app_row["resume_path"])).exists() else "missing"
    if not report.ready:
        next_action = "Resolve the listed blockers, then re-check readiness."
    elif resume_state in {"not_prepared", "missing"}:
        next_action = "Tailor a resume, review the PDF, then re-check readiness."
    else:
        next_action = "Review the application and open it for a human-supervised fill."
    data = {
        "job_id": int(job.id or 0),
        "company": _clean(job.company, 200),
        "title": _clean(job.title, 200),
        "ready": bool(report.ready),
        "score": report.score,
        "blockers": blockers,
        "warnings": warnings,
        "resume_state": resume_state,
        "review_state": "not_tracked",
        "application_status": str(app_row["status"]) if app_row else "",
        "next_action": next_action,
    }
    return data, [{"rel": "dashboard", "path": f"/jobs/{job.id}"}]


def _profile_overview(profile: Profile) -> dict[str, Any]:
    return {
        "name": _clean(profile.full_name or profile.name, 160),
        "headline": _clean(profile.headline, 240),
        "location": _clean(profile.location, 160),
        "counts": {
            "skills": len(profile.skills or []),
            "experience": len(profile.experience or []),
            "projects": len(profile.projects or []),
            "education": len(profile.education or []),
        },
    }


def _profile_items(profile: Profile, section: str) -> list[Any]:
    if section == "skills":
        return [_clean(item, 120) for item in (profile.skills or [])]
    if section == "experience":
        items = []
        for entry in profile.experience or []:
            if not isinstance(entry, dict):
                continue
            items.append(
                {
                    "title": _clean(entry.get("title"), 160),
                    "company": _clean(entry.get("company"), 160),
                    "location": _clean(entry.get("location"), 160),
                    "start": _clean(entry.get("start"), 40),
                    "end": _clean(entry.get("end"), 40),
                    "bullets": [
                        _redact_contact(_clean(item, 360))
                        for item in list(entry.get("bullets") or [])[:8]
                    ],
                }
            )
        return items
    if section == "projects":
        items = []
        for entry in profile.projects or []:
            if not isinstance(entry, dict):
                continue
            items.append(
                {
                    "title": _clean(entry.get("title"), 160),
                    "company": _clean(entry.get("company"), 160),
                    "tech": _clean(entry.get("tech") or entry.get("tech_line"), 240),
                    "url": _sanitize_url_list(str(entry.get("url") or "")),
                    "bullets": [
                        _redact_contact(_clean(item, 360))
                        for item in list(entry.get("bullets") or [])[:8]
                    ],
                }
            )
        return items
    return []


def _get_profile(context: QueryContext, params: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    revision = _profile_revision(context.profile)
    offset = _decode_cursor(params["cursor"], revision)
    section = params["section"]
    if section == "overview":
        return {
            "section": "overview",
            "overview": _profile_overview(context.profile),
            "profile_revision": revision,
            "next_cursor": None,
        }, [{"rel": "dashboard", "path": "/profiles"}]
    items = _profile_items(context.profile, section)
    page = items[offset : offset + params["limit"]]
    more = len(items) > offset + params["limit"]
    return {
        "section": section,
        "items": page,
        "next_cursor": _encode_cursor(offset + params["limit"], revision) if more else None,
        "profile_revision": revision,
    }, [{"rel": "dashboard", "path": "/profiles"}]


def _list_applications(context: QueryContext, params: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    revision = _applications_revision(context.conn, context.profile_id)
    offset = _decode_cursor(params["cursor"], revision)
    where = ["a.profile_id=?"]
    values: list[Any] = [context.profile_id]
    if params["status"]:
        where.append("a.status=?")
        values.append(params["status"])
    rows = context.conn.execute(
        f"""
        SELECT a.id, a.job_id, a.status, a.ats, a.created_at, a.submitted_at,
               a.updated_at, j.company, j.title
        FROM applications a JOIN jobs j ON j.id=a.job_id
        WHERE {' AND '.join(where)}
        ORDER BY a.updated_at DESC, a.id DESC
        LIMIT ? OFFSET ?
        """,
        [*values, params["limit"] + 1, offset],
    ).fetchall()
    more = len(rows) > params["limit"]
    rows = rows[: params["limit"]]
    applications = [
        {
            "application_id": int(row["id"]),
            "job_id": int(row["job_id"]),
            "company": _clean(row["company"], 200),
            "title": _clean(row["title"], 200),
            "status": _clean(row["status"], 30),
            "channel": _clean(row["ats"], 40),
            "created_at": row["created_at"],
            "submitted_at": row["submitted_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]
    return {
        "applications": applications,
        "next_cursor": _encode_cursor(offset + params["limit"], revision) if more else None,
        "dataset_revision": revision,
    }, [{"rel": "dashboard", "path": "/applications"}]


def call_tool(context: QueryContext, tool: str, payload: Any) -> dict[str, Any]:
    rid = context.request_id or request_id()
    context.request_id = rid
    try:
        params = validate_tool_input(tool, payload)
        for required in TOOL_SPECS[tool].get("required_scopes") or []:
            require_scope(context, required)
        if tool in ACTION_TOOLS:
            if tool in {
                "jobforge_remember",
                "jobforge_forget",
                "jobforge_request_resume",
            }:
                with action_lock(context.config):
                    data, links = dispatch_action(context, tool, params)
            else:
                data, links = dispatch_action(context, tool, params)
            warnings = []
        elif tool == "jobforge_status":
            data = _status(context)
            warnings = data.pop("warnings", [])
            links = data.pop("links", [])
        elif tool == "jobforge_search_jobs":
            data, links = _search_jobs(context, params)
            warnings = []
        elif tool == "jobforge_get_job":
            data, links = _get_job(context, params)
            warnings = []
        elif tool == "jobforge_explain_fit":
            data, links = _explain_fit(context, params)
            warnings = []
        elif tool == "jobforge_get_readiness":
            data, links = _get_readiness(context, params)
            warnings = []
        elif tool == "jobforge_get_profile":
            data, links = _get_profile(context, params)
            warnings = []
        elif tool == "jobforge_list_applications":
            data, links = _list_applications(context, params)
            warnings = []
        else:
            raise ToolError("NOT_FOUND", "Unknown tool.")
        result = envelope(
            data,
            request_id_value=rid,
            warnings=warnings,
            links=links,
        )
        return check_envelope_size(result)
    except ToolError as exc:
        return error_envelope(exc, request_id_value=rid)
    except Exception:
        return error_envelope(
            ToolError(
                "INTERNAL_ERROR",
                "The workbench could not complete this read. The local log has the request ID.",
            ),
            request_id_value=rid,
        )
