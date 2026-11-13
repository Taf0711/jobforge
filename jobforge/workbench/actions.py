"""Administrative memory, dashboard, and resume-request actions."""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .. import db
from ..paths import base_dir
from .authorization import QueryContext
from .contracts import ToolError

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+\d[\d\s().-]{7,}\d|\(\d{3}\)\s*\d{3}[-. ]\d{4})(?!\d)"
)
_SECRET_RE = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9._-]{20,}|BEGIN [A-Z ]*PRIVATE KEY)"
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MANUAL_ONLY_HOSTS = ("indeed.com", "linkedin.com")
_TRACKING_PARAM_PREFIXES = ("utm_", "gclid", "fbclid", "msclkid")
_TRACKING_PARAMS = {"ref", "referrer", "lever-source", "gh_src"}


def _is_tracking_param(name: str) -> bool:
    lowered = name.lower()
    return lowered in _TRACKING_PARAMS or any(
        lowered.startswith(prefix) for prefix in _TRACKING_PARAM_PREFIXES
    )


def _public_job_url(value: str, field: str) -> str:
    """Validate a public http(s) posting URL without fetching it."""
    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError as exc:
        raise ToolError("INVALID_ARGUMENT", f"{field} is not a valid URL.") from exc
    if parsed.scheme not in {"http", "https"} or not host:
        raise ToolError("INVALID_ARGUMENT", f"{field} must be a public http(s) URL.")
    if parsed.username or parsed.password:
        raise ToolError("INVALID_ARGUMENT", f"{field} must not contain credentials.")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ToolError("INVALID_ARGUMENT", f"{field} must not target a local host.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        raise ToolError("INVALID_ARGUMENT", f"{field} must not target a private address.")
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not _is_tracking_param(key)
    ]
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc.lower(),
            parsed.path or "/",
            urlencode(query, doseq=True),
            "",
        )
    )


def _import_line(value: str, field: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", _CONTROL_RE.sub("", str(value or ""))).strip()
    if not text:
        raise ToolError("INVALID_ARGUMENT", f"{field} must not be empty.")
    return text[:limit]


def _import_description(value: str, limit: int) -> str:
    text = _CONTROL_RE.sub("", str(value or "")).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise ToolError("INVALID_ARGUMENT", "description must not be empty.")
    return text[:limit]


def _import_source(value: str, url: str) -> str:
    from ..sources.direct import detect_ats

    cleaned = re.sub(r"[^a-z0-9._:-]+", "-", str(value or "").strip().lower()).strip("-")
    if cleaned:
        return cleaned[:40]
    ats = detect_ats(url)
    return ats[:40] if ats and ats != "generic" else "agent"


def _is_manual_only(url: str) -> bool:
    host = (urlsplit(url or "").hostname or "").lower().rstrip(".")
    return any(host == suffix or host.endswith("." + suffix) for suffix in _MANUAL_ONLY_HOSTS)


def _memory_text(value: str, field: str) -> str:
    text = _CONTROL_RE.sub("", str(value or "")).strip()
    if not text:
        raise ToolError("INVALID_ARGUMENT", f"{field} must not be empty.")
    if _EMAIL_RE.search(text) or _PHONE_RE.search(text):
        raise ToolError(
            "INVALID_ARGUMENT",
            "Do not store contact details in memory; use the profile or answer bank.",
        )
    if _SECRET_RE.search(text):
        raise ToolError("INVALID_ARGUMENT", "Do not store secrets or credentials in memory.")
    return text


def remember(context: QueryContext, params: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    key = _memory_text(params["key"], "key")
    value = _memory_text(params["value"], "value")
    row = db.remember_agent_memory(
        context.conn,
        connection_id=context.connection_id,
        profile_id=context.profile_id,
        key=key,
        value=value,
    )
    return {
        "key": row.get("key", key),
        "value": row.get("value", value),
        "updated_at": row.get("updated_at", ""),
        "scope": "profile",
    }, []


def recall(context: QueryContext, params: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    rows = db.list_agent_memories(
        context.conn,
        profile_id=context.profile_id,
        query=params.get("query", ""),
        limit=int(params.get("limit") or 20),
    )
    return {
        "memories": [
            {
                "key": str(row.get("key") or ""),
                "value": str(row.get("value") or ""),
                "updated_at": str(row.get("updated_at") or ""),
            }
            for row in rows
        ]
    }, []


def forget(context: QueryContext, params: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    key = _memory_text(params["key"], "key")
    deleted = db.forget_agent_memory(context.conn, profile_id=context.profile_id, key=key)
    return {"key": key, "deleted": bool(deleted)}, []


def dashboard_url(context: QueryContext) -> str:
    host = str(context.config.server.host or "127.0.0.1")
    if host in {"0.0.0.0", "::", "[::]"}:
        host = "127.0.0.1"
    return f"http://{host}:{int(context.config.server.port)}"


def _dashboard_health(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/healthz", timeout=0.8) as response:
            return response.status == 200
    except Exception:
        return False


def dashboard(
    context: QueryContext, params: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    url = dashboard_url(context)
    return {
        "url": url,
        "running": _dashboard_health(url),
        "approval_path": "/operations",
    }, [{"rel": "dashboard", "path": "/"}]


def _start_dashboard(context: QueryContext) -> dict[str, Any]:
    url = dashboard_url(context)
    if _dashboard_health(url):
        return {"url": url, "running": True, "started": False, "pid": None}
    command = [sys.executable, "-m", "jobforge"]
    if context.config.config_path:
        command.extend(["--config", str(context.config.config_path)])
    command.extend(
        [
            "serve",
            "--host",
            str(context.config.server.host or "127.0.0.1"),
            "--port",
            str(int(context.config.server.port)),
        ]
    )
    log_path = Path(context.config.data_dir) / "dashboard.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch(exist_ok=True)
    try:
        os.chmod(log_path, 0o600)
    except OSError:
        pass
    with log_path.open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            cwd=str(base_dir()),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if _dashboard_health(url):
            return {"url": url, "running": True, "started": True, "pid": int(process.pid)}
        if process.poll() is not None:
            break
        time.sleep(0.2)
    return {
        "url": url,
        "running": _dashboard_health(url),
        "started": True,
        "pid": int(process.pid),
    }


def open_dashboard(
    context: QueryContext, params: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    result = _start_dashboard(context)
    opened = False
    if result["running"]:
        try:
            opened = bool(webbrowser.open(str(result["url"])))
        except Exception:
            opened = False
    result["opened"] = opened
    return result, [{"rel": "dashboard", "path": "/"}]


def _resume_export_dir(value: str, config: Any) -> str:
    """Resolve a writable export directory for the rendered PDF.

    An explicit path must be an existing absolute directory. Without one, the
    MCP server's working directory is used when it looks like a host session
    workspace, not the JobForge app home or source checkout.
    """
    text = str(value or "").strip()
    if text:
        path = Path(text).expanduser()
        if not path.is_absolute():
            raise ToolError(
                "INVALID_ARGUMENT", "destination_dir must be an absolute path."
            )
        try:
            path = path.resolve(strict=True)
        except OSError as exc:
            raise ToolError(
                "INVALID_ARGUMENT", "destination_dir does not exist."
            ) from exc
        if not path.is_dir() or not os.access(path, os.W_OK):
            raise ToolError(
                "INVALID_ARGUMENT", "destination_dir must be a writable directory."
            )
        return str(path)

    cwd = Path.cwd().resolve()
    home = Path.home().resolve()
    app_home = Path(config.data_dir).parent.resolve()
    checkout = base_dir().resolve()
    if cwd in {Path("/"), home}:
        return ""
    if cwd == app_home or cwd.is_relative_to(app_home):
        return ""
    if cwd == checkout or cwd.is_relative_to(checkout):
        return ""
    if not cwd.is_dir() or not os.access(cwd, os.W_OK):
        return ""
    return str(cwd)


def _operation_public(operation: dict[str, Any]) -> dict[str, Any]:
    result = operation.get("result") or {}
    safe_result = {
        key: result.get(key)
        for key in (
            "application_id",
            "ats",
            "warnings",
            "llm_used",
            "llm_model",
            "mode",
            "facts",
            "export_path",
            "submitted",
            "captcha",
        )
        if key in result
    }
    if result.get("application_id"):
        safe_result["artifact_url"] = f"/artifacts/{int(result['application_id'])}/resume"
    return {
        "operation_id": operation["id"],
        "action": operation["action"],
        "destination_dir": str((operation.get("payload") or {}).get("export_dir") or ""),
        "state": operation["state"],
        "created_at": operation["created_at"],
        "expires_at": operation["expires_at"],
        "approved_at": operation["approved_at"],
        "started_at": operation["started_at"],
        "finished_at": operation["finished_at"],
        "approval_path": f"/operations/{operation['id']}",
        "result": safe_result,
        "error_code": operation.get("error_code") or "",
        "error_message": operation.get("error_message") or "",
        "submission_performed_by_jobforge": bool(
            operation.get("action") == "submit_application"
            and result.get("submitted") is True
        ),
    }


def request_resume(
    context: QueryContext, params: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    job = db.get_job(context.conn, int(params["job_id"]))
    if job is None:
        raise ToolError("NOT_FOUND", "Job not found.")
    base_resume = context.profile.resume_path or (
        str(context.config.resume_path) if context.config.resume_path else ""
    )
    if not base_resume or not Path(base_resume).exists():
        raise ToolError(
            "INVALID_ARGUMENT",
            "No base resume is configured for the bound profile.",
        )
    export_dir = _resume_export_dir(params.get("destination_dir", ""), context.config)
    payload: dict[str, Any] = {"job_id": int(job.id or 0)}
    if export_dir:
        payload["export_dir"] = export_dir
    try:
        operation = db.create_agent_operation(
            context.conn,
            connection_id=context.connection_id,
            profile_id=context.profile_id,
            action="render_resume",
            payload=payload,
            idempotency_key=params["idempotency_key"],
        )
    except ValueError as exc:
        raise ToolError("IDEMPOTENCY_CONFLICT", str(exc)) from exc
    return _operation_public(operation), [
        {"rel": "approval", "path": f"/operations/{operation['id']}"}
    ]


def get_operation(
    context: QueryContext, params: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    db.expire_stale_agent_operations(context.conn)
    operation = db.get_agent_operation(context.conn, params["operation_id"])
    if (
        operation is None
        or operation["connection_id"] != context.connection_id
        or int(operation["profile_id"]) != context.profile_id
    ):
        raise ToolError("NOT_FOUND", "Operation not found.")
    return _operation_public(operation), [
        {"rel": "operation", "path": f"/operations/{operation['id']}"}
    ]


def list_operations(
    context: QueryContext, params: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    db.expire_stale_agent_operations(context.conn)
    rows = db.list_agent_operations(
        context.conn,
        profile_id=context.profile_id,
        connection_id=context.connection_id,
        limit=int(params.get("limit") or 10),
    )
    return {"operations": [_operation_public(row) for row in rows]}, [
        {"rel": "dashboard", "path": "/operations"}
    ]


def import_job(
    context: QueryContext, params: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Store and quick-score one posting the host agent already fetched."""
    from .. import services
    from ..models import Job
    from ..scan.quick import quick_scan
    from ..sources.base import is_remote

    url = _public_job_url(params["url"], "url")
    apply_url = _public_job_url(params["apply_url"], "apply_url") if params.get("apply_url") else url
    title = _import_line(params["title"], "title", 300)
    company = _import_line(params["company"], "company", 200)
    description = _import_description(params["description"], 60_000)
    location = (
        _import_line(params.get("location", ""), "location", 200)
        if str(params.get("location") or "").strip()
        else ""
    )
    remote = params.get("remote")
    if remote is None:
        remote = is_remote(location, description)
    salary_min = params.get("salary_min")
    salary_max = params.get("salary_max")
    if salary_min is not None and salary_max is not None and salary_min > salary_max:
        raise ToolError("INVALID_ARGUMENT", "salary_min must not exceed salary_max.")
    currency = str(params.get("salary_currency") or "USD").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ToolError("INVALID_ARGUMENT", "salary_currency must be a three-letter code.")
    source = _import_source(params.get("source", ""), url)
    source_id = str(params.get("source_id") or "").strip()
    if not source_id:
        source_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    competition = str(params.get("competition_profile") or "").strip()
    job = Job(
        source=source,
        source_id=source_id,
        company=company,
        title=title,
        location=location,
        remote=bool(remote),
        salary_min=salary_min,
        salary_max=salary_max,
        salary_currency=currency,
        url=url,
        apply_url=apply_url,
        description=description,
        posted_at=(str(params.get("posted_at") or "").strip() or None),
        competition_profile=competition,
        raw={"agent_import": True, "site": (urlsplit(url).hostname or "")},
    )
    existing = context.conn.execute(
        "SELECT id FROM jobs WHERE fingerprint=?", (job.fingerprint,)
    ).fetchone()
    job_id = db.upsert_job(context.conn, job)
    job.id = job_id
    archetypes = services.load_archetypes(context.config)
    scan = quick_scan(
        job,
        profile=context.profile,
        prefs=context.prefs,
        archetypes=archetypes,
        scan_cfg=dict(context.config.scan or {}),
    )
    db.set_scan(context.conn, scan)
    match = db.get_match(context.conn, job_id, context.profile_id)
    return {
        "job_id": job_id,
        "created": existing is None,
        "source": source,
        "url": url,
        "scan_score": round(float(scan.scan_score), 1),
        "scan_band": scan.band,
        "fit_score": round(float(match.score), 1) if match else None,
        "manual_only": _is_manual_only(url) or _is_manual_only(apply_url),
        "next": "jobforge_score" if match is None else "jobforge_explain_fit",
    }, [{"rel": "job", "path": f"/jobs/{job_id}"}]


def ingest_emails(
    context: QueryContext, params: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Accept host-read employer mail and run the deterministic tracker."""
    from ..email.ingest import ingest_messages

    result = ingest_messages(
        context.conn,
        context.config.email,
        params.get("provider", "agent"),
        params["messages"],
        apply_status=bool(params.get("apply_status", True)),
    )
    return result, [
        {"rel": "dashboard", "path": "/inbox"},
        {"rel": "dashboard", "path": "/applications"},
    ]


def scan_jobs(
    context: QueryContext, params: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Run one bounded scan for the connection's bound profile."""
    from .. import services

    stats = services.scan(
        context.config,
        context.conn,
        only=params.get("sources") or None,
        profile_id=context.profile_id,
    )
    return {
        "new": int(stats.get("new") or 0),
        "seen": int(stats.get("seen") or 0),
        "filtered": int(stats.get("filtered") or 0),
        "deferred": int(stats.get("deferred") or 0),
        "scanned": int(stats.get("scanned") or 0),
        "per_source": dict(stats.get("per_source") or {}),
        "errors": [str(item)[:300] for item in (stats.get("errors") or [])[:20]],
    }, [{"rel": "dashboard", "path": "/jobs"}]


def score_jobs(
    context: QueryContext, params: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Deep-score stored jobs for the bound profile."""
    from .. import services

    stats = services.rescore(
        context.config,
        context.conn,
        only_unscored=(
            params["scope"] == "unscored"
            and not params["job_ids"]
            and params["top"] is None
        ),
        job_ids=params["job_ids"] or None,
        top=params["top"],
        limit=params["limit"],
        profile_id=context.profile_id,
    )
    return {
        "scored": int(stats.get("scored") or 0),
        "total": int(stats.get("total") or 0),
        "comp": dict(stats.get("comp") or {}),
    }, [{"rel": "dashboard", "path": "/jobs"}]


def build_queue(
    context: QueryContext, params: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Build the supervised queue for the bound profile."""
    from ..queue import Queue

    queue = Queue(Path(context.config.data_dir) / "queue.json")
    if any(item.profile_id != context.profile_id for item in queue.list_items()):
        raise ToolError(
            "PROFILE_QUEUE_CONFLICT",
            "The shared queue contains items for another profile. Resolve them first.",
        )
    items = queue.build(
        context.conn,
        context.profile,
        context.prefs,
        limit=int(params["limit"]),
        min_score=float(context.prefs.min_match or 0),
    )
    return {
        "items": [
            {
                "job_id": int(item.job_id),
                "status": item.status,
                "score": round(float(item.score), 1),
                "company": item.company[:200],
                "title": item.title[:200],
            }
            for item in items
        ],
        "summary": queue.summary(),
    }, [{"rel": "dashboard", "path": "/queue"}]


def request_submission(
    context: QueryContext, params: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Create a pending one-use submission operation; never submit here."""
    from .. import risk

    job = db.get_job(context.conn, int(params["job_id"]))
    if job is None:
        raise ToolError("NOT_FOUND", "Job not found.")
    try:
        operation = db.create_agent_operation(
            context.conn,
            connection_id=context.connection_id,
            profile_id=context.profile_id,
            action="submit_application",
            payload={"job_id": int(job.id or 0)},
            idempotency_key=params["idempotency_key"],
        )
    except ValueError as exc:
        raise ToolError("IDEMPOTENCY_CONFLICT", str(exc)) from exc
    allowed, reason, policy = risk.can_submit_unattended(context.config)
    data = _operation_public(operation)
    data["risk"] = {
        "authorized": bool(allowed),
        "reason": reason,
        "daily_cap": int(policy.daily_submit_cap),
    }
    return data, [{"rel": "approval", "path": f"/operations/{operation['id']}"}]


ACTION_TOOLS = {
    "jobforge_remember",
    "jobforge_recall",
    "jobforge_forget",
    "jobforge_dashboard",
    "jobforge_open_dashboard",
    "jobforge_request_resume",
    "jobforge_request_submission",
    "jobforge_get_operation",
    "jobforge_list_operations",
    "jobforge_scan",
    "jobforge_import_job",
    "jobforge_ingest_emails",
    "jobforge_score",
    "jobforge_build_queue",
}


def dispatch(
    context: QueryContext, tool: str, params: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    handlers = {
        "jobforge_remember": remember,
        "jobforge_recall": recall,
        "jobforge_forget": forget,
        "jobforge_dashboard": dashboard,
        "jobforge_open_dashboard": open_dashboard,
        "jobforge_request_resume": request_resume,
        "jobforge_request_submission": request_submission,
        "jobforge_scan": scan_jobs,
        "jobforge_import_job": import_job,
        "jobforge_ingest_emails": ingest_emails,
        "jobforge_score": score_jobs,
        "jobforge_build_queue": build_queue,
        "jobforge_get_operation": get_operation,
        "jobforge_list_operations": list_operations,
    }
    handler = handlers.get(tool)
    if handler is None:
        raise ToolError("NOT_FOUND", "Unknown tool.")
    return handler(context, params)
