"""Candidate context pack for the agent and advisor.

The pack is local, bounded, and deliberately redacts contact details by default.
The agent needs skills, evidence, project and application context, not the
candidate's email address.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from .. import db
from ..models import Preferences, Profile

EVIDENCE_WEIGHTS = {
    "direct_professional": 1.0,
    "direct_project": 0.8,
    "adjacent_professional": 0.7,
    "adjacent_project_coursework": 0.55,
    "skills_list_only": 0.3,
    "interest_only": 0.1,
    "no_evidence": 0.0,
}

CHANNEL_RE = re.compile(r"\bchannel\s+([a-z][a-z0-9_ -]{1,40})", re.IGNORECASE)
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(
    r"(?<!\w)\+?\d{1,3}[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\w)"
)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _short(value: Any, limit: int) -> str:
    text = _text(value) if not isinstance(value, (dict, list)) else ""
    return _redact(text[:limit] + ("…" if len(text) > limit else ""))


def _redact(text: str) -> str:
    return PHONE_RE.sub("[phone]", EMAIL_RE.sub("[email]", text))


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def fact_skill_weights(profile: Profile) -> dict[str, float]:
    """Best evidence weight per skill from the fact bank."""
    weights: dict[str, float] = {skill: 0.3 for skill in profile.skills}
    for fact in profile.facts:
        section = str(fact.get("section", "")).lower()
        weight = 1.0 if section == "experience" else 0.8
        for skill in fact.get("skills", []) or []:
            key = str(skill)
            if key:
                weights[key] = max(weights.get(key, 0.0), weight)
    return weights


def skill_evidence(profile: Profile) -> dict[str, dict[str, Any]]:
    """Combine the curated evidence matrix with fact-bank support.

    ``level`` comes from the evidence matrix when present; otherwise it is
    inferred from the fact section. ``fact_ids`` and ``projects`` are always
    derived from the actual fact bank so the agent can cite proof.
    """
    matrix = profile.evidence or {}
    matrix_skills = matrix.get("skills") or {}
    items = matrix.get("evidence_items") or {}
    out: dict[str, dict[str, Any]] = {}

    for category, group in matrix_skills.items():
        if not isinstance(group, dict):
            continue
        for skill, entry in group.items():
            if not isinstance(entry, dict):
                entry = {}
            level = str(entry.get("best_level") or "skills_list_only")
            source_ids = [str(item) for item in entry.get("evidence", []) or []]
            sources = []
            for source_id in source_ids[:8]:
                item = items.get(source_id) if isinstance(items, dict) else None
                if isinstance(item, dict):
                    sources.append(
                        {
                            "id": source_id,
                            "level": str(item.get("level") or ""),
                            "summary": _short(item.get("summary"), 220),
                        }
                    )
            out[str(skill)] = {
                "category": str(category),
                "level": level,
                "weight": EVIDENCE_WEIGHTS.get(level, 0.0),
                "notes": _short(entry.get("notes"), 260),
                "evidence_items": sources,
                "fact_ids": [],
                "projects": [],
            }

    fact_weights = fact_skill_weights(profile)
    for fact in profile.facts:
        section = str(fact.get("section", "")).lower()
        level = "direct_professional" if section == "experience" else "direct_project"
        for skill in fact.get("skills", []) or []:
            key = str(skill)
            if not key:
                continue
            entry = out.setdefault(
                key,
                {
                    "category": "derived",
                    "level": level,
                    "weight": fact_weights.get(key, 0.0),
                    "notes": "",
                    "evidence_items": [],
                    "fact_ids": [],
                    "projects": [],
                },
            )
            fact_id = str(fact.get("id") or "")
            if fact_id and fact_id not in entry["fact_ids"]:
                entry["fact_ids"].append(fact_id)
            if section == "projects":
                project = _text(fact.get("title"))
                if project and project not in entry["projects"]:
                    entry["projects"].append(project)

    for skill, weight in fact_weights.items():
        entry = out.setdefault(
            skill,
            {
                "category": "skills-list",
                "level": "skills_list_only",
                "weight": weight,
                "notes": "",
                "evidence_items": [],
                "fact_ids": [],
                "projects": [],
            },
        )
        entry["weight"] = max(float(entry.get("weight") or 0.0), weight)

    for entry in out.values():
        entry["fact_ids"] = _unique(entry["fact_ids"])[:12]
        entry["projects"] = _unique(entry["projects"])[:8]
    return out


def strongest_skills(profile: Profile, limit: int = 15) -> list[dict[str, Any]]:
    evidence = skill_evidence(profile)
    ranked = sorted(
        evidence.items(),
        key=lambda item: (
            -float(item[1].get("weight") or 0.0),
            -len(item[1].get("fact_ids") or []),
            item[0].lower(),
        ),
    )
    return [
        {
            "skill": skill,
            "evidence_level": entry.get("level"),
            "weight": round(float(entry.get("weight") or 0.0), 2),
            "fact_ids": entry.get("fact_ids") or [],
            "projects": entry.get("projects") or [],
        }
        for skill, entry in ranked[:limit]
    ]


def _identity(profile: Profile, *, include_contact: bool) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": profile.full_name or profile.name,
        "headline": profile.headline,
        "location": profile.location,
        "links": dict(profile.links or {}),
        "contact_available": bool(profile.email or profile.phone),
    }
    if include_contact:
        data["email"] = profile.email
        data["phone"] = profile.phone
    return data


def _resume_forge_extras(profile: Profile) -> dict[str, Any]:
    raw = ((profile.context or {}).get("resume_forge") or {})
    scoring = dict(raw.get("scoring_inputs") or {})
    scoring.pop("gpa_for_scoring", None)
    return {
        "programs": raw.get("programs") or [],
        "verified_quantifiers": raw.get("verified_quantifiers") or {},
        "sensitive_wording": raw.get("sensitive_wording") or {},
        "resume_style_rules": raw.get("resume_style_rules") or {},
        "scoring_inputs": scoring,
        "target_domains": (raw.get("preferences") or {}).get("target_domains") or [],
        "comp_policy": (raw.get("preferences") or {}).get("comp_policy") or {},
    }


def _application_context(
    conn: sqlite3.Connection | None, profile: Profile, limit: int = 20
) -> dict[str, Any]:
    if conn is None or profile.id is None:
        return {
            "total": 0,
            "by_status": {},
            "by_channel": {},
            "channel_performance": [],
            "recent": [],
        }
    rows = db.list_applications(conn, profile.id, limit=300)
    by_status: dict[str, int] = {}
    by_channel: dict[str, int] = {}
    responses: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "draft")
        by_status[status] = by_status.get(status, 0) + 1
        match = CHANNEL_RE.search(str(row.get("notes") or ""))
        channel = match.group(1).strip().lower().replace(" ", "_") if match else "unknown"
        by_channel[channel] = by_channel.get(channel, 0) + 1
        if status in {"interview", "offer", "rejected"}:
            responses[channel] = responses.get(channel, 0) + 1
    performance = [
        {
            "channel": channel,
            "submitted": count,
            "responses": responses.get(channel, 0),
        }
        for channel, count in sorted(by_channel.items(), key=lambda item: -item[1])
    ]
    recent = [
        {
            "company": str(row.get("company") or ""),
            "title": str(row.get("title") or ""),
            "status": str(row.get("status") or ""),
            "created_at": str(row.get("created_at") or ""),
            "submitted_at": str(row.get("submitted_at") or ""),
            "notes": _redact(_short(row.get("notes"), 180)),
        }
        for row in rows[:limit]
    ]
    return {
        "total": len(rows),
        "by_status": by_status,
        "by_channel": by_channel,
        "channel_performance": performance,
        "recent": recent,
    }


def _network_context(cfg: Any) -> dict[str, Any]:
    if cfg is None:
        return {"connections": [], "warnings": []}
    try:
        from ..services import load_network

        graph = load_network(cfg)
    except Exception:  # noqa: BLE001 - context must never break the dashboard
        return {"connections": [], "warnings": ["Network file could not be loaded"]}
    if graph is None:
        return {"connections": [], "warnings": []}
    connections = [
        {
            "name": str(item.get("name") or ""),
            "company": str(item.get("company") or ""),
            "role": str(item.get("role") or ""),
            "relationship": str(item.get("relationship") or ""),
            "strength": str(item.get("strength") or ""),
            "last_contact": str(item.get("last_contact") or ""),
        }
        for item in graph.connections[:80]
    ]
    return {"connections": connections, "warnings": list(graph.warnings)[:10]}


def _market_context(
    conn: sqlite3.Connection | None, profile: Profile, limit: int = 10
) -> dict[str, Any]:
    if conn is None or profile.id is None:
        return {"top_jobs": [], "counts": {}}
    counts = db.stats(conn, profile.id)
    rows = db.list_jobs(conn, profile.id, min_score=0, limit=limit)
    top = []
    for row in rows:
        meta = row.get("meta") or {}
        top.append(
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
    return {"top_jobs": top, "counts": counts}


def build_candidate_context(
    profile: Profile,
    prefs: Preferences,
    conn: sqlite3.Connection | None = None,
    cfg: Any = None,
    *,
    include_contact: bool = False,
    market_limit: int = 10,
    fact_limit: int = 80,
    project_limit: int = 20,
) -> dict[str, Any]:
    """Build a bounded, JSON-serializable context pack."""
    evidence = skill_evidence(profile)
    facts = [
        {
            "id": str(fact.get("id") or ""),
            "section": str(fact.get("section") or ""),
            "title": str(fact.get("title") or ""),
            "company": str(fact.get("company") or ""),
            "text": _short(fact.get("text"), 500),
            "skills": list(fact.get("skills") or [])[:12],
        }
        for fact in profile.facts[:fact_limit]
    ]
    experience = [
        {
            "title": str(entry.get("title") or ""),
            "company": str(entry.get("company") or ""),
            "location": str(entry.get("location") or ""),
            "start": str(entry.get("start") or ""),
            "end": str(entry.get("end") or ""),
            "bullets": [_redact(str(item)) for item in (entry.get("bullets") or [])[:6]],
        }
        for entry in profile.experience
    ]
    projects = [
        {
            "name": str(entry.get("title") or entry.get("name") or ""),
            "tech_line": str(entry.get("tech_line") or entry.get("description") or ""),
            "tags": list(entry.get("tags") or [])[:12],
            "best_for": list(entry.get("best_for") or [])[:8],
            "bullets": [_redact(str(item)) for item in (entry.get("bullets") or [])[:4]],
            "url": str(entry.get("url") or entry.get("link") or ""),
            "dead": bool(entry.get("dead")),
            "notes": _short(entry.get("notes"), 260),
        }
        for entry in profile.projects[:project_limit]
    ]
    evidence_items = (profile.evidence or {}).get("evidence_items") or {}
    item_summaries = []
    if isinstance(evidence_items, dict):
        for item_id, item in list(evidence_items.items())[:50]:
            if isinstance(item, dict):
                item_summaries.append(
                    {
                        "id": str(item_id),
                        "source": _short(item.get("source"), 100),
                        "level": str(item.get("level") or ""),
                        "summary": _short(item.get("summary"), 220),
                    }
                )
    return {
        "schema_version": 1,
        "identity": _identity(profile, include_contact=include_contact),
        "work_authorization": dict(profile.work_auth or {}),
        "preferences": prefs.to_dict(),
        "resume_forge": _resume_forge_extras(profile),
        "skills": {
            "all": sorted(evidence.keys(), key=str.lower),
            "by_evidence": evidence,
            "strongest": strongest_skills(profile, 15),
        },
        "experience": experience,
        "projects": projects,
        "facts": facts,
        "fact_count": len(profile.facts),
        "facts_truncated": len(profile.facts) > fact_limit,
        "evidence": {
            "levels": (profile.evidence or {}).get("evidence_levels") or {},
            "items": item_summaries,
            "item_count": len(evidence_items) if isinstance(evidence_items, dict) else 0,
        },
        "applications": _application_context(conn, profile),
        "network": _network_context(cfg),
        "market": _market_context(conn, profile, market_limit),
        "profile_quality": {
            "has_resume": bool(profile.resume_path and Path(profile.resume_path).exists()),
            "has_facts": bool(profile.facts),
            "has_projects": bool(profile.projects),
            "live_projects": sum(1 for project in profile.projects if not project.get("dead")),
            "skill_count": len(profile.skills),
            "experience_count": len(profile.experience),
            "summary_present": bool(profile.summary),
        },
    }
