"""Administrative memory, dashboard, and resume-request actions."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

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
        )
        if key in result
    }
    if result.get("application_id"):
        safe_result["artifact_url"] = f"/artifacts/{int(result['application_id'])}/resume"
    return {
        "operation_id": operation["id"],
        "action": operation["action"],
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
        "submission_performed_by_jobforge": False,
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
    try:
        operation = db.create_agent_operation(
            context.conn,
            connection_id=context.connection_id,
            profile_id=context.profile_id,
            action="render_resume",
            payload={"job_id": int(job.id or 0)},
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
    rows = db.list_agent_operations(
        context.conn,
        profile_id=context.profile_id,
        connection_id=context.connection_id,
        limit=int(params.get("limit") or 10),
    )
    return {"operations": [_operation_public(row) for row in rows]}, [
        {"rel": "dashboard", "path": "/operations"}
    ]


ACTION_TOOLS = {
    "jobforge_remember",
    "jobforge_recall",
    "jobforge_forget",
    "jobforge_dashboard",
    "jobforge_open_dashboard",
    "jobforge_request_resume",
    "jobforge_get_operation",
    "jobforge_list_operations",
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
        "jobforge_get_operation": get_operation,
        "jobforge_list_operations": list_operations,
    }
    handler = handlers.get(tool)
    if handler is None:
        raise ToolError("NOT_FOUND", "Unknown tool.")
    return handler(context, params)
