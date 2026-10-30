"""Deterministic candidate advice used by the agent and CLI.

This module does not call a model. It turns the stored profile, evidence,
project bank, application history, and market snapshot into a stable,
explainable advice object.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from .. import db
from ..models import Preferences, Profile
from .context import (
    build_candidate_context,
    skill_evidence,
    strongest_skills,
)

IMPORTANCE_ORDER = {"must": 0, "nice": 1, "context": 2}


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _requirements_gaps(requirements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for requirement in requirements:
        skill = str(requirement.get("skill") or "").strip()
        importance = str(requirement.get("importance") or "").lower()
        if not skill or importance not in IMPORTANCE_ORDER:
            continue
        # scored requirements carry `gap` and/or `credit`; older fixtures may
        # only carry `coverage`. Trust the explicit gap flag first so a direct
        # 1.0-credit skill never becomes a false "gap".
        explicit_gap = requirement.get("gap")
        if explicit_gap is False:
            continue
        raw_coverage = requirement.get("coverage")
        if raw_coverage is None:
            raw_coverage = requirement.get("credit")
        try:
            coverage = float(raw_coverage if raw_coverage is not None else 0.0)
        except (TypeError, ValueError):
            coverage = 0.0
        if explicit_gap is not True and coverage >= 0.99:
            continue
        out.append(
            {
                "skill": skill,
                "importance": importance,
                "coverage": round(coverage, 2),
                "matched_by": str(requirement.get("matched_by") or ""),
            }
        )
    return out


def _target_gaps(
    conn: sqlite3.Connection | None,
    profile: Profile,
    *,
    target_job_id: int | None,
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if conn is None or profile.id is None:
        return [], None
    if target_job_id is not None:
        job = db.get_job(conn, target_job_id)
        match = db.get_match(conn, target_job_id, profile.id)
        if not job or not match:
            return [], None
        gaps = _requirements_gaps(match.requirements)
        return (
            [
                {
                    **gap,
                    "reason": f"{gap['importance']}-have gap in {job.title} at {job.company}",
                }
                for gap in gaps[:limit]
            ],
            {
                "job_id": target_job_id,
                "company": job.company,
                "title": job.title,
                "fit": round(float(match.score or 0), 1),
                "gate_survival": round(float(match.gate_survival or 0), 2),
                "gaps": list(match.gaps or [])[:8],
            },
        )

    rows = db.list_jobs(conn, profile.id, min_score=0, limit=max(limit, 10))
    counts: dict[str, dict[str, Any]] = {}
    for row in rows:
        for gap in _requirements_gaps(row.get("requirements") or []):
            key = gap["skill"]
            entry = counts.setdefault(
                key,
                {
                    "skill": key,
                    "count": 0,
                    "must_count": 0,
                    "coverage": gap["coverage"],
                    "importance": gap["importance"],
                },
            )
            entry["count"] += 1
            if gap["importance"] == "must":
                entry["must_count"] += 1
                entry["importance"] = "must"
            entry["coverage"] = min(float(entry["coverage"]), gap["coverage"])
    ranked = sorted(
        counts.values(),
        key=lambda item: (
            -int(item["must_count"]),
            -int(item["count"]),
            item["skill"].lower(),
        ),
    )
    return (
        [
            {
                "skill": item["skill"],
                "importance": item["importance"],
                "coverage": round(float(item["coverage"]), 2),
                "occurrences": item["count"],
                "must_occurrences": item["must_count"],
                "reason": (
                    f"appears in {item['count']} of the top stored matches "
                    f"({item['must_count']} must-have)"
                ),
            }
            for item in ranked[:limit]
        ],
        None,
    )


def _project_portfolio(profile: Profile) -> dict[str, Any]:
    live = [project for project in profile.projects if not project.get("dead")]
    dead = [project for project in profile.projects if project.get("dead")]
    missing_url = [
        str(project.get("title") or project.get("name") or "")
        for project in live
        if not str(project.get("url") or project.get("link") or "").strip()
    ]
    without_quantified = [
        str(project.get("title") or project.get("name") or "")
        for project in live
        if not any(re.search(r"\d", str(bullet)) for bullet in (project.get("bullets") or []))
    ]
    domains: dict[str, int] = {}
    for project in live:
        for label in [*(project.get("tags") or []), *(project.get("best_for") or [])]:
            key = str(label).strip().lower()
            if key:
                domains[key] = domains.get(key, 0) + 1
    return {
        "live": len(live),
        "dead": len(dead),
        "dead_projects": [
            str(project.get("title") or project.get("name") or "") for project in dead[:20]
        ],
        "missing_url": missing_url[:20],
        "without_quantified_bullet": without_quantified[:20],
        "by_domain": dict(sorted(domains.items(), key=lambda item: -item[1])[:20]),
    }


def _data_quality(profile: Profile, context: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    quality = context.get("profile_quality") or {}
    if not quality.get("has_resume"):
        warnings.append("No readable base resume is attached to the active profile.")
    if not quality.get("has_facts"):
        warnings.append("The fact bank is empty; tailoring will fall back to parsed resume bullets.")
    if not quality.get("live_projects"):
        warnings.append("No live projects are available for tailoring.")
    evidence = skill_evidence(profile)
    only_list = [
        skill
        for skill, entry in evidence.items()
        if float(entry.get("weight") or 0) <= 0.3 and not entry.get("fact_ids")
    ]
    if len(only_list) > max(8, len(evidence) // 3):
        warnings.append(
            f"{len(only_list)} skills have no fact/project evidence; do not treat the skill list as proof."
        )
    if (profile.context or {}).get("resume_forge", {}).get("programs"):
        warnings.append(
            "Some imported programs are explicitly excluded from resumes; never add them back without the user's approval."
        )
    return warnings


def _next_actions(
    profile: Profile,
    *,
    target_gaps: list[dict[str, Any]],
    skills_list_only: list[str],
    portfolio: dict[str, Any],
    funnel: dict[str, Any],
    data_quality: list[str],
) -> list[str]:
    actions: list[str] = []
    if data_quality:
        actions.append("Fix the data-quality warnings before relying on tailoring or scoring.")
    if target_gaps:
        top = ", ".join(gap["skill"] for gap in target_gaps[:5])
        actions.append(
            f"Close or route around the biggest target gaps: {top}. Add only verified evidence; do not add a skill to the resume because a JD asks for it."
        )
    if skills_list_only:
        actions.append(
            "Prove or demote skills that exist only in the skills list: "
            + ", ".join(skills_list_only[:8])
            + "."
        )
    if portfolio.get("dead_projects"):
        actions.append(
            "Keep dead projects out of every tailored resume: "
            + ", ".join(portfolio["dead_projects"][:5])
            + "."
        )
    if portfolio.get("missing_url"):
        actions.append(
            "Add public URLs to live projects where available: "
            + ", ".join(portfolio["missing_url"][:5])
            + "."
        )
    if portfolio.get("without_quantified_bullet"):
        actions.append(
            "Tighten projects with no measurable outcome: "
            + ", ".join(portfolio["without_quantified_bullet"][:5])
            + "."
        )
    performance = funnel.get("channel_performance") or []
    for channel in performance:
        if channel.get("submitted", 0) >= 3 and channel.get("responses", 0) == 0:
            actions.append(
                f"{channel['channel']} has {channel['submitted']} submissions and no recorded responses; prefer a warm path where one exists."
            )
    if not profile.facts and not profile.skills:
        actions.append("Import a resume or the resume-forge profile before asking the agent to tailor.")
    if not actions:
        actions.append(
            "Pick a high-fit job, inspect its gaps and warm path, then tailor and review the PDF before filling the form."
        )
    return actions


def suggest_profile_improvements(
    profile: Profile,
    prefs: Preferences,
    conn: sqlite3.Connection | None = None,
    cfg: Any = None,
    *,
    focus: str = "",
    target_job_id: int | None = None,
    limit: int = 12,
) -> dict[str, Any]:
    """Return deterministic, explainable skill/project/funnel advice."""
    context = build_candidate_context(profile, prefs, conn, cfg)
    evidence = skill_evidence(profile)
    strong = strongest_skills(profile, max(5, limit))
    skills_list_only = [
        skill
        for skill, entry in evidence.items()
        if float(entry.get("weight") or 0) <= 0.3 and not entry.get("fact_ids")
    ]
    skills_list_only.sort(key=str.lower)
    target_gaps, target_job = _target_gaps(
        conn, profile, target_job_id=target_job_id, limit=limit
    )
    portfolio = _project_portfolio(profile)
    funnel = context.get("applications") or {}
    data_quality = _data_quality(profile, context)
    resume_forge = context.get("resume_forge") or {}
    comp_policy = (
        prefs.comp_policy
        or resume_forge.get("comp_policy")
        or {}
    )
    interview = (resume_forge.get("scoring_inputs") or {}).get("interview_readiness")
    actions = _next_actions(
        profile,
        target_gaps=target_gaps,
        skills_list_only=skills_list_only,
        portfolio=portfolio,
        funnel=funnel,
        data_quality=data_quality,
    )
    if focus.strip():
        focus_lower = focus.strip().lower()
        target_gaps = sorted(
            target_gaps,
            key=lambda gap: 0 if focus_lower in gap["skill"].lower() else 1,
        )
    return {
        "focus": focus.strip(),
        "target_job": target_job,
        "strongest_skills": strong,
        "skills_list_only": skills_list_only[:20],
        "target_gaps": target_gaps,
        "project_portfolio": portfolio,
        "application_funnel": {
            "total": funnel.get("total", 0),
            "by_status": funnel.get("by_status") or {},
            "by_channel": funnel.get("by_channel") or {},
        },
        "channel_performance": funnel.get("channel_performance") or [],
        "comp_policy": comp_policy,
        "interview_readiness": interview,
        "data_quality": data_quality,
        "next_actions": actions,
        "profile_quality": context.get("profile_quality") or {},
    }
