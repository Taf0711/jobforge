"""One-operation worker for human-approved resume and submission actions."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .. import db, quality, risk, services
from ..config import Config
from ..locking import action_lock
from ..models import utcnow
from ..paths import base_dir
from .authorization import load_query_context
from .contracts import ToolError


def _safe_error(value: Exception, config: Config) -> str:
    text = str(value).strip() or value.__class__.__name__
    for path in (
        str(Path(config.data_dir).parent),
        str(config.data_dir),
        str(config.output_dir),
    ):
        text = text.replace(path, "[local path]")
    return text[:500]


def _finish(
    conn,
    operation_id: str,
    state: str,
    *,
    result: dict[str, Any] | None = None,
    error_code: str = "",
    error_message: str = "",
) -> None:
    db.update_agent_operation(
        conn,
        operation_id,
        state=state,
        finished_at=utcnow(),
        result_json=result or {},
        error_code=error_code,
        error_message=error_message,
    )
    db.append_agent_operation_event(
        conn,
        operation_id,
        state,
        {"error_code": error_code, "application_id": (result or {}).get("application_id")},
    )


def run_operation(config: Config, conn, operation_id: str) -> dict[str, Any]:
    """Claim and execute one approved operation under the domain-action lock."""
    operation = db.get_agent_operation(conn, operation_id)
    if operation is None:
        raise ValueError("Operation not found")
    if operation["state"] != "approved":
        return operation
    if operation["expires_at"] and operation["expires_at"] < utcnow():
        db.update_agent_operation(conn, operation_id, state="expired", finished_at=utcnow())
        db.append_agent_operation_event(conn, operation_id, "expired", {})
        return db.get_agent_operation(conn, operation_id) or operation
    if operation["state"] == "approved" and operation.get("approved_at"):
        try:
            approved_at = datetime.fromisoformat(
                str(operation["approved_at"]).replace("Z", "+00:00")
            )
        except ValueError:
            approved_at = None
        if approved_at is not None and approved_at.tzinfo is None:
            approved_at = approved_at.replace(tzinfo=timezone.utc)
        if approved_at is not None and (
            datetime.now(timezone.utc) - approved_at > timedelta(minutes=10)
        ):
            db.update_agent_operation(
                conn, operation_id, state="expired", finished_at=utcnow()
            )
            db.append_agent_operation_event(
                conn, operation_id, "expired", {"reason": "approval_window"}
            )
            return db.get_agent_operation(conn, operation_id) or operation

    with action_lock(config):
        operation = db.claim_agent_operation(conn, operation_id, uuid.uuid4().hex)
        if operation is None or operation["state"] != "running":
            return operation or {}
        try:
            action = str(operation["action"])
            if action not in {"render_resume", "submit_application"}:
                _finish(
                    conn,
                    operation_id,
                    "failed",
                    error_code="INVALID_PROPOSAL",
                    error_message="Unsupported operation action.",
                )
                return db.get_agent_operation(conn, operation_id) or operation
            job_id = int((operation.get("payload") or {}).get("job_id") or 0)

            # Approval is not a permanent grant. Re-check the connection and
            # its scopes at execution time; revocation or disabling the local
            # integration must stop a pending action.
            try:
                context = load_query_context(
                    config, conn, connection_id=str(operation["connection_id"])
                )
            except ToolError as exc:
                _finish(
                    conn,
                    operation_id,
                    "failed",
                    error_code=exc.code,
                    error_message=_safe_error(exc, config),
                )
                return db.get_agent_operation(conn, operation_id) or operation
            if int(context.profile_id) != int(operation["profile_id"]):
                _finish(
                    conn,
                    operation_id,
                    "failed",
                    error_code="AUTHORIZATION_CHANGED",
                    error_message="The connection is no longer bound to the approved profile.",
                )
                return db.get_agent_operation(conn, operation_id) or operation
            required = (
                {"proposals:create", "profile:resume_context"}
                if action == "render_resume"
                else {"submission:request"}
            )
            if not required.issubset(context.scopes):
                _finish(
                    conn,
                    operation_id,
                    "failed",
                    error_code="SCOPE_DENIED",
                    error_message="The connection no longer grants the scopes this operation needs.",
                )
                return db.get_agent_operation(conn, operation_id) or operation

            if action == "render_resume":
                stats = services.tailor_job(
                    config, conn, job_id, profile_id=context.profile_id
                )
                result = {
                    "application_id": int(stats.get("application_id") or 0),
                    "ats": stats.get("ats") or {},
                    "facts": int(stats.get("facts") or 0),
                    "warnings": list(stats.get("warnings") or [])[:20],
                    "llm_used": bool(stats.get("llm_used")),
                    "llm_model": str(stats.get("llm_model") or ""),
                    "mode": str(stats.get("mode") or ""),
                }
                _finish(conn, operation_id, "succeeded", result=result)
                return db.get_agent_operation(conn, operation_id) or operation

            try:
                stats = services.apply_job(
                    config,
                    conn,
                    job_id,
                    allow_submit=True,  # security-audit: allow auto-submit enabled
                    submission_mode="unattended",
                    review_only=False,
                    non_interactive=True,
                    profile_id=context.profile_id,
                )
            except quality.QualityBlocked as exc:
                _finish(
                    conn,
                    operation_id,
                    "failed",
                    error_code="QUALITY_BLOCKED",
                    error_message=_safe_error(exc, config),
                )
                return db.get_agent_operation(conn, operation_id) or operation
            except risk.RiskError as exc:
                text = _safe_error(exc, config)
                _finish(
                    conn,
                    operation_id,
                    "failed",
                    error_code=(
                        "DAILY_CAP_REACHED"
                        if "cap reached" in text.lower()
                        else "RISK_NOT_AUTHORIZED"
                    ),
                    error_message=text,
                )
                return db.get_agent_operation(conn, operation_id) or operation
            except ValueError as exc:
                _finish(
                    conn,
                    operation_id,
                    "failed",
                    error_code="SUBMISSION_REFUSED",
                    error_message=_safe_error(exc, config),
                )
                return db.get_agent_operation(conn, operation_id) or operation

            result = {
                "application_id": int(stats.get("application_id") or 0),
                "ats": stats.get("ats") or {},
                "submitted": bool(stats.get("submitted")),
                "captcha": bool(stats.get("captcha")),
                "warnings": list(stats.get("warnings") or [])[:20],
            }
            if result["submitted"]:
                _finish(conn, operation_id, "succeeded", result=result)
            elif result["captcha"]:
                _finish(
                    conn,
                    operation_id,
                    "failed",
                    error_code="CAPTCHA_REQUIRED",
                    error_message="The application reached a captcha; solve it manually.",
                    result=result,
                )
            else:
                _finish(
                    conn,
                    operation_id,
                    "failed",
                    error_code="SUBMISSION_NOT_CONFIRMED",
                    error_message="The form did not report a successful submission.",
                    result=result,
                )
        except Exception as exc:  # noqa: BLE001 - worker boundary
            _finish(
                conn,
                operation_id,
                "failed",
                error_code=(
                    "RENDER_FAILED"
                    if operation.get("action") == "render_resume"
                    else "SUBMISSION_FAILED"
                ),
                error_message=_safe_error(exc, config),
            )
        return db.get_agent_operation(conn, operation_id) or operation


def launch_worker(config: Config, operation_id: str) -> int:
    """Spawn one detached worker. The worker claims the approved operation."""
    command = [sys.executable, "-m", "jobforge"]
    if config.config_path:
        command.extend(["--config", str(config.config_path)])
    command.extend(["operation", "run", "--id", operation_id])
    log_path = Path(config.data_dir) / "operations.log"
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
    return int(process.pid)
