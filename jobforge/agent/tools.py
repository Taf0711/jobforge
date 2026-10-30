"""Allowlisted tools available to the JobForge agent.

Tools are the only way the model can read or change application state. Every
tool has a small JSON schema and an access class. The runtime enforces write
gating; the handlers also validate their own numeric bounds.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .. import db, quality, services
from ..config import Config
from ..models import Preferences, Profile
from ..queue import Queue
from .advisor import suggest_profile_improvements
from .context import build_candidate_context, skill_evidence


@dataclass
class ToolContext:
    config: Config
    conn: sqlite3.Connection
    profile: Profile
    prefs: Preferences
    allow_writes: bool = False
    thread_id: int | None = None


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    access: str
    handler: Callable[[ToolContext, dict[str, Any]], Any]


def _int(value: Any, default: int, *, low: int = 1, high: int = 100) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _job_row(conn: sqlite3.Connection, profile: Profile, job_id: int) -> dict[str, Any]:
    job = db.get_job(conn, job_id)
    if job is None:
        raise ValueError(f"Job {job_id} not found")
    match = db.get_match(conn, job_id, profile.id or 0)
    app_row = conn.execute(
        "SELECT * FROM applications WHERE job_id=? AND profile_id=?",
        (job_id, profile.id),
    ).fetchone()
    row = {
        "job_id": job.id,
        "source": job.source,
        "company": job.company,
        "title": job.title,
        "location": job.location,
        "remote": job.remote,
        "salary_min": job.salary_min,
        "salary_max": job.salary_max,
        "url": job.url,
        "apply_url": job.apply_url,
        "posted_at": job.posted_at,
        "discovered_at": job.discovered_at,
        "application_status": str(app_row["status"]) if app_row else "",
        "application_id": int(app_row["id"]) if app_row else None,
    }
    if match:
        row.update(
            {
                "fit": round(float(match.score or 0), 1),
                "coverage": round(float(match.coverage or 0), 3),
                "gate_survival": round(float(match.gate_survival or 0), 3),
                "reasons": list(match.reasons or [])[:8],
                "gaps": list(match.gaps or [])[:10],
                "pursuit": round(float((match.meta or {}).get("pursuit") or 0), 1),
                "comp": (match.meta or {}).get("comp") or {},
            }
        )
    else:
        row.update({"fit": None, "pursuit": None, "reasons": [], "gaps": []})
    return row


def _quality_payload(conn: sqlite3.Connection, ctx: ToolContext, job_id: int) -> dict[str, Any]:
    job = db.get_job(conn, job_id)
    if job is None:
        raise ValueError(f"Job {job_id} not found")
    report = quality.evaluate(conn, job, ctx.profile, ctx.prefs)
    return {
        "ready": report.ready,
        "score": report.score,
        "blocking": [
            {"name": check.name, "detail": check.detail}
            for check in quality.blocking_checks(report)
        ],
        "warnings": [
            {"name": check.name, "detail": check.detail}
            for check in report.checks
            if not check.passed and check.severity != quality.BLOCKER
        ],
        "checks": [
            {
                "name": check.name,
                "passed": check.passed,
                "severity": check.severity,
                "detail": check.detail,
            }
            for check in report.checks
        ],
    }


def _tool_profile_context(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    section = str(args.get("section") or "overview")
    limit = _int(args.get("limit"), 20, low=1, high=80)
    context = build_candidate_context(
        ctx.profile,
        ctx.prefs,
        ctx.conn,
        ctx.config,
        include_contact=False,
        market_limit=min(limit, 20),
        fact_limit=limit * 4,
        project_limit=limit,
    )
    if section == "all":
        return context
    if section == "overview":
        return {
            "identity": context["identity"],
            "work_authorization": context["work_authorization"],
            "preferences": context["preferences"],
            "profile_quality": context["profile_quality"],
            "resume_forge": context["resume_forge"],
        }
    if section not in context:
        raise ValueError(
            "Unknown section. Use overview, skills, projects, experience, facts, "
            "evidence, applications, network, market, resume_forge, or all."
        )
    value = context[section]
    if isinstance(value, list):
        return {"section": section, "items": value[:limit], "truncated": len(value) > limit}
    return {"section": section, "value": value}


def _tool_search_jobs(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()[:200]
    limit = _int(args.get("limit"), 10, low=1, high=50)
    minimum = args.get("min_score")
    try:
        min_score = float(minimum) if minimum is not None else None
    except (TypeError, ValueError):
        min_score = None
    if min_score is not None:
        min_score = max(0.0, min(100.0, min_score))
    source = str(args.get("source") or "").strip()[:60]
    rows = db.list_jobs(
        ctx.conn,
        ctx.profile.id,
        q=query,
        min_score=min_score,
        source=source,
        one_per_company=bool(ctx.prefs.one_application_per_company),
        limit=limit,
    )
    jobs = []
    for row in rows:
        meta = row.get("meta") or {}
        jobs.append(
            {
                "job_id": row.get("id"),
                "company": row.get("company"),
                "title": row.get("title"),
                "location": row.get("location"),
                "fit": round(float(row.get("score") or 0), 1),
                "pursuit": round(float(meta.get("pursuit") or 0), 1),
                "gaps": list(row.get("gaps") or [])[:5],
                "source": row.get("source"),
                "posted_at": row.get("posted_at"),
                "application_status": row.get("application_status") or "",
            }
        )
    return {"count": len(jobs), "jobs": jobs}


def _tool_get_job(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    job_id = _int(args.get("job_id"), 0, low=1, high=2_000_000_000)
    if not job_id:
        raise ValueError("job_id is required")
    row = _job_row(ctx.conn, ctx.profile, job_id)
    job = db.get_job(ctx.conn, job_id)
    if job is None:
        raise ValueError(f"Job {job_id} not found")
    if bool(args.get("include_description")):
        row["description"] = (job.description or "")[:8000]
    row["quality"] = _quality_payload(ctx.conn, ctx, job_id)
    network = services.load_network(ctx.config)
    if network:
        row["warm"] = services.warm_summary(network, job.company, float(row.get("fit") or 0))
    return row


def _tool_explain_fit(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    job_id = _int(args.get("job_id"), 0, low=1, high=2_000_000_000)
    if not job_id:
        raise ValueError("job_id is required")
    job = db.get_job(ctx.conn, job_id)
    if job is None:
        raise ValueError(f"Job {job_id} not found")
    match = db.get_match(ctx.conn, job_id, ctx.profile.id or 0)
    if match is None:
        raise ValueError(f"Job {job_id} has no stored match; run score first")
    evidence = skill_evidence(ctx.profile)
    required = []
    for requirement in (match.requirements or [])[:25]:
        skill = str(requirement.get("skill") or "")
        proof = evidence.get(skill) if skill else None
        required.append(
            {
                "skill": skill or None,
                "importance": requirement.get("importance"),
                "coverage": requirement.get("coverage"),
                "matched_by": requirement.get("matched_by"),
                "proof": (
                    {
                        "level": proof.get("level"),
                        "fact_ids": proof.get("fact_ids") or [],
                        "projects": proof.get("projects") or [],
                    }
                    if proof
                    else None
                ),
            }
        )
    return {
        "job_id": job_id,
        "company": job.company,
        "title": job.title,
        "fit": round(float(match.score or 0), 1),
        "coverage": round(float(match.coverage or 0), 3),
        "gate_survival": round(float(match.gate_survival or 0), 3),
        "pursuit": round(float((match.meta or {}).get("pursuit") or 0), 1),
        "reasons": list(match.reasons or []),
        "gaps": list(match.gaps or []),
        "requirements": required,
        "comp": (match.meta or {}).get("comp") or {},
    }


def _tool_readiness(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    job_id = _int(args.get("job_id"), 0, low=1, high=2_000_000_000)
    if not job_id:
        raise ValueError("job_id is required")
    payload = _quality_payload(ctx.conn, ctx, job_id)
    app = ctx.conn.execute(
        "SELECT id, status, resume_path, cover_letter_path FROM applications "
        "WHERE job_id=? AND profile_id=?",
        (job_id, ctx.profile.id),
    ).fetchone()
    payload["application"] = dict(app) if app else None
    if not payload["ready"]:
        payload["next_action"] = "Resolve the blocking checks before opening the fill browser."
    elif app is None:
        payload["next_action"] = "Tailor a resume for this job, then review the PDF."
    else:
        payload["next_action"] = (
            "Review the tailored PDF, then open the job detail and click Fill application; "
            "you must review the form and click Submit yourself."
        )
    payload["submission"] = "The agent cannot submit. Human review is required."
    return payload


def _tool_advice(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    target = args.get("target_job_id")
    try:
        target_job_id = int(target) if target is not None else None
    except (TypeError, ValueError):
        target_job_id = None
    return suggest_profile_improvements(
        ctx.profile,
        ctx.prefs,
        ctx.conn,
        ctx.config,
        focus=str(args.get("focus") or "")[:120],
        target_job_id=target_job_id,
        limit=_int(args.get("limit"), 12, low=3, high=40),
    )


def _tool_warm_path(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    company = str(args.get("company") or "").strip()[:120]
    if not company:
        raise ValueError("company is required")
    network = services.load_network(ctx.config)
    if network is None:
        return {"company": company, "available": False, "reason": "No network.yml is loaded."}
    fit = 0.0
    rows = db.list_jobs(ctx.conn, ctx.profile.id, q=company, min_score=0, limit=5)
    if rows:
        fit = float(rows[0].get("score") or 0)
    return {"available": True, **services.warm_summary(network, company, fit)}


def _tool_applications(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    status = str(args.get("status") or "").strip()[:30]
    rows = db.list_applications(
        ctx.conn,
        ctx.profile.id,
        status=status,
        limit=_int(args.get("limit"), 30, low=1, high=200),
    )
    return {
        "count": len(rows),
        "applications": [
            {
                "application_id": row.get("id"),
                "company": row.get("company"),
                "title": row.get("title"),
                "status": row.get("status"),
                "created_at": row.get("created_at"),
                "submitted_at": row.get("submitted_at"),
                "notes": str(row.get("notes") or "")[:220],
            }
            for row in rows
        ],
    }


def _tool_market(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    limit = _int(args.get("limit"), 10, low=1, high=40)
    return {
        "counts": db.stats(ctx.conn, ctx.profile.id),
        "jobs": _tool_search_jobs(ctx, {"limit": limit, "min_score": args.get("min_score")}),
    }


def _tool_tailor(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if not ctx.allow_writes:
        return {
            "error": "Writes are disabled. Enable Allow actions for this message to tailor.",
            "requires_write_permission": True,
        }
    job_id = _int(args.get("job_id"), 0, low=1, high=2_000_000_000)
    if not job_id:
        raise ValueError("job_id is required")
    result = services.tailor_job(
        ctx.config, ctx.conn, job_id, profile_id=ctx.profile.id
    )
    readiness = _quality_payload(ctx.conn, ctx, job_id)
    next_action = (
        "Review the PDF, then open the job detail and click Fill application. "
        "The agent cannot submit."
        if readiness["ready"]
        else "Resolve the quality blockers before opening the fill browser: "
        + "; ".join(item["name"] for item in readiness["blocking"])
    )
    return {
        "ok": True,
        "job_id": job_id,
        "pdf": result.get("pdf"),
        "cover_letter": result.get("cover_letter"),
        "ats": result.get("ats") or {},
        "facts_selected": result.get("facts"),
        "warnings": result.get("warnings") or [],
        "quality": readiness,
        "next_action": next_action,
    }


def _tool_open_application_review(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if not ctx.allow_writes:
        return {
            "error": (
                "Writes are disabled. Enable Allow actions for this message to open "
                "the application review browser."
            ),
            "requires_write_permission": True,
        }
    job_id = _int(args.get("job_id"), 0, low=1, high=2_000_000_000)
    if not job_id:
        raise ValueError("job_id is required")
    job = db.get_job(ctx.conn, job_id)
    if job is None:
        raise ValueError(f"Job {job_id} not found")
    from ..automation.base import is_manual_only_url

    if is_manual_only_url(job.apply_url or job.url):
        raise ValueError(
            "Indeed and LinkedIn are manual-only. Open the posting and apply yourself."
        )
    report = quality.evaluate(ctx.conn, job, ctx.profile, ctx.prefs)
    if not report.ready:
        return {
            "ok": False,
            "job_id": job_id,
            "ready": False,
            "blocking": [
                {"name": check.name, "detail": check.detail}
                for check in quality.blocking_checks(report)
            ],
            "next_action": "Resolve the quality blockers before opening the browser.",
        }
    hold_seconds = _int(args.get("hold_seconds"), 600, low=30, high=3600)
    pid = services.launch_apply_subprocess(
        ctx.config, job_id, hold_seconds=hold_seconds, review_only=True
    )
    return {
        "ok": True,
        "job_id": job_id,
        "pid": pid,
        "mode": "review",
        "submitted": False,
        "hold_seconds": hold_seconds,
        "next_action": (
            "The fill browser is opening in a separate process. Review every field, "
            "solve any captcha yourself, and click Submit yourself. The agent did "
            "not submit and will not submit."
        ),
    }

def _tool_scan(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if not ctx.allow_writes:
        return {
            "error": "Writes are disabled. Enable Allow actions for this message to scan.",
            "requires_write_permission": True,
        }
    source = str(args.get("source") or "").strip()[:60]
    stats = services.scan(ctx.config, ctx.conn, only=[source] if source else None)
    try:
        scored = services.rescore(ctx.config, ctx.conn, only_unscored=True)
        stats["scored"] = scored
    except Exception as exc:  # noqa: BLE001 - scanning succeeded; scoring is separable
        stats["score_error"] = str(exc)
    return stats


def _tool_score(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if not ctx.allow_writes:
        return {
            "error": "Writes are disabled. Enable Allow actions for this message to score.",
            "requires_write_permission": True,
        }
    return services.rescore(
        ctx.config, ctx.conn, only_unscored=bool(args.get("only_unscored"))
    )


def _tool_queue(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if not ctx.allow_writes:
        return {
            "error": "Writes are disabled. Enable Allow actions for this message to build the queue.",
            "requires_write_permission": True,
        }
    limit = _int(args.get("limit"), 5, low=1, high=50)
    minimum = args.get("min_score")
    try:
        min_score = float(minimum) if minimum is not None else float(ctx.prefs.min_match)
    except (TypeError, ValueError):
        min_score = float(ctx.prefs.min_match)
    queue = Queue(ctx.config.data_dir / "queue.json")
    items = queue.build(
        ctx.conn,
        ctx.profile,
        ctx.prefs,
        limit=limit,
        min_score=max(0.0, min(100.0, min_score)),
    )
    return {
        "added": len(items),
        "items": [item.to_dict() for item in items],
        "summary": queue.summary(),
        "note": "Queue items still require tailoring and human review before submission.",
    }


def create_tool_registry(ctx: ToolContext) -> list[ToolSpec]:
    return [
        ToolSpec(
            name="get_profile_context",
            description=(
                "Read the candidate context: identity, skills with evidence, projects, "
                "experience, facts, evidence matrix, preferences, applications, network, "
                "market snapshot, or all."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "section": {
                        "type": "string",
                        "enum": [
                            "overview",
                            "skills",
                            "projects",
                            "experience",
                            "facts",
                            "evidence",
                            "applications",
                            "network",
                            "market",
                            "resume_forge",
                            "all",
                        ],
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 80},
                },
            },
            access="read",
            handler=_tool_profile_context,
        ),
        ToolSpec(
            name="search_jobs",
            description="Search stored jobs by title, company, description, score, and source.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "min_score": {"type": "number", "minimum": 0, "maximum": 100},
                    "source": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
            },
            access="read",
            handler=_tool_search_jobs,
        ),
        ToolSpec(
            name="get_job",
            description="Read one job with its match, application status, quality gate, and warm path.",
            parameters={
                "type": "object",
                "properties": {
                    "job_id": {"type": "integer"},
                    "include_description": {"type": "boolean"},
                },
                "required": ["job_id"],
            },
            access="read",
            handler=_tool_get_job,
        ),
        ToolSpec(
            name="explain_fit",
            description=(
                "Explain a stored match: fit, gate survival, reasons, gaps, requirement "
                "coverage, and the fact/project proof behind each requirement."
            ),
            parameters={
                "type": "object",
                "properties": {"job_id": {"type": "integer"}},
                "required": ["job_id"],
            },
            access="read",
            handler=_tool_explain_fit,
        ),
        ToolSpec(
            name="get_application_readiness",
            description=(
                "Return the quality-gate blockers and the exact next human action. "
                "This tool never submits."
            ),
            parameters={
                "type": "object",
                "properties": {"job_id": {"type": "integer"}},
                "required": ["job_id"],
            },
            access="read",
            handler=_tool_readiness,
        ),
        ToolSpec(
            name="suggest_profile_improvements",
            description=(
                "Analyze skills, evidence, project portfolio, application funnel, channel "
                "performance, and target-job gaps. Deterministic, not model-generated."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "focus": {"type": "string"},
                    "target_job_id": {"type": "integer"},
                    "limit": {"type": "integer", "minimum": 3, "maximum": 40},
                },
            },
            access="read",
            handler=_tool_advice,
        ),
        ToolSpec(
            name="get_warm_path",
            description="Return the referral/channel recommendation for a company.",
            parameters={
                "type": "object",
                "properties": {"company": {"type": "string"}},
                "required": ["company"],
            },
            access="read",
            handler=_tool_warm_path,
        ),
        ToolSpec(
            name="list_applications",
            description="List applications in the tracker, optionally filtered by status.",
            parameters={
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
            },
            access="read",
            handler=_tool_applications,
        ),
        ToolSpec(
            name="get_job_market_overview",
            description="Show stored job counts and the current top matches.",
            parameters={
                "type": "object",
                "properties": {
                    "min_score": {"type": "number", "minimum": 0, "maximum": 100},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 40},
                },
            },
            access="read",
            handler=_tool_market,
        ),
        ToolSpec(
            name="tailor_resume",
            description=(
                "Generate a tailored PDF and application draft for one job. Includes "
                "quality blockers. Requires Allow actions. Never opens a browser."
            ),
            parameters={
                "type": "object",
                "properties": {"job_id": {"type": "integer"}},
                "required": ["job_id"],
            },
            access="write",
            handler=_tool_tailor,
        ),
        ToolSpec(
            name="open_application_review",
            description=(
                "Open the supervised fill browser for a prepared, quality-ready job "
                "in review mode. The agent never clicks Submit. Requires Allow actions."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "job_id": {"type": "integer"},
                    "hold_seconds": {"type": "integer", "minimum": 30, "maximum": 3600},
                },
                "required": ["job_id"],
            },
            access="write",
            handler=_tool_open_application_review,
        ),
        ToolSpec(
            name="scan_jobs",
            description=(
                "Run the configured public job sources, optionally for one source, then "
                "score new rows. Requires Allow actions and network access."
            ),
            parameters={
                "type": "object",
                "properties": {"source": {"type": "string"}},
            },
            access="write",
            handler=_tool_scan,
        ),
        ToolSpec(
            name="score_jobs",
            description="Rescore stored jobs. Requires Allow actions.",
            parameters={
                "type": "object",
                "properties": {"only_unscored": {"type": "boolean"}},
            },
            access="write",
            handler=_tool_score,
        ),
        ToolSpec(
            name="build_application_queue",
            description=(
                "Add ranked jobs at or above the minimum score to the supervised queue. "
                "Requires Allow actions; the queue still needs tailoring and human review."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    "min_score": {"type": "number", "minimum": 0, "maximum": 100},
                },
            },
            access="write",
            handler=_tool_queue,
        ),
    ]


def tool_schemas(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
        for tool in tools
    ]


def execute_tool(
    tools: list[ToolSpec], ctx: ToolContext, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    spec = next((tool for tool in tools if tool.name == name), None)
    if spec is None:
        return {"error": f"Unknown tool: {name}"}
    if spec.access in {"write", "network"} and not ctx.allow_writes:
        return {
            "error": "Writes are disabled for this message.",
            "requires_write_permission": True,
        }
    try:
        result = spec.handler(ctx, arguments if isinstance(arguments, dict) else {})
    except Exception as exc:  # noqa: BLE001 - report tool errors to the model safely
        return {"error": str(exc)[:500]}
    try:
        json.dumps(result, ensure_ascii=False, default=str)
        return result
    except (TypeError, ValueError):
        return {"error": "Tool returned a non-serializable result."}
