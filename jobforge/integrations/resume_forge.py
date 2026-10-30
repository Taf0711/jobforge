"""Import the verified resume-forge data into JobForge.

resume-forge (the personal resume system) keeps a curated profile, a project
bank, an evidence matrix, a referral network, and a long-lived application
tracker. This module converts those files into JobForge's local models. It is an
importer, not a runtime dependency: JobForge keeps working without it.

Nothing here is bundled with the public package as personal data. The command
reads local paths supplied by the user.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

import yaml

from .. import db
from ..matching.skills import ALIASES, SKILLS, canonical, extract_skills
from ..models import Application, Job, Preferences, Profile, slugify, utcnow

STATUS_MAP = {
    "not started": "draft",
    "not_started": "draft",
    "resume ready": "prepared",
    "resume_ready": "prepared",
    "applied": "submitted",
    "oa": "submitted",
    "phone screen": "interview",
    "phone_screen": "interview",
    "interview": "interview",
    "offer": "offer",
    "rejected": "rejected",
    "withdrawn": "withdrawn",
}


def _load_yaml(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {}
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"Not a file: {p}")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected a mapping in {p}")
    return data


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


KNOWN_SKILLS = {canonical(skill) for skill in [*SKILLS, *ALIASES.keys()]}


def _skills_from(*values: Any, allow_unknown: bool = False) -> list[str]:
    found: list[str] = []
    for value in values:
        if isinstance(value, str):
            found.extend(extract_skills(value))
        elif isinstance(value, list):
            for item in value:
                if not isinstance(item, str):
                    continue
                skill = canonical(item)
                if allow_unknown or skill in KNOWN_SKILLS:
                    found.append(skill)
    return _unique(found)


def _split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split()
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


def _salary_range(text: str) -> tuple[int | None, int | None]:
    values: list[int] = []
    for number, suffix in re.findall(r"\$?\s*([\d][\d,]*(?:\.\d+)?)\s*([kK])?", text or ""):
        try:
            value = float(number.replace(",", ""))
        except ValueError:
            continue
        if suffix:
            value *= 1000
        if value >= 1000:
            values.append(int(value))
    if not values:
        return None, None
    if len(values) == 1:
        return values[0], values[0]
    return min(values[:2]), max(values[:2])


def _project_url(entry: dict[str, Any]) -> str:
    for key in ("url", "link", "repo", "github", "source_url"):
        value = _text(entry.get(key))
        if value:
            return value if value.startswith(("http://", "https://")) else "https://" + value
    haystack = " ".join(
        str(entry.get(key) or "")
        for key in ("notes", "description", "tech_line")
    ) + " " + " ".join(str(item) for item in entry.get("bullets", []) or [])
    match = re.search(r"(?:https?://)?github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", haystack)
    return f"https://{match.group(0).removeprefix('https://').removeprefix('http://')}" if match else ""


def load_profile(
    profile_path: str | Path,
    *,
    projects_path: str | Path | None = None,
    evidence_path: str | Path | None = None,
    resume_path: str = "",
) -> Profile:
    """Convert profile.yml + projects.yml + evidence_matrix.yml to a Profile."""
    data = _load_yaml(profile_path)
    candidate = data.get("candidate", {}) or {}
    education = data.get("education", {}) or {}
    authorization = data.get("authorization", {}) or {}
    scoring_inputs = data.get("scoring_inputs", {}) or {}
    evidence = _load_yaml(evidence_path) if evidence_path else {}

    first, last = _split_name(_text(candidate.get("name")))
    context = {
        "resume_forge": {
            "schema_version": data.get("schema_version"),
            "last_updated": data.get("last_updated"),
            "candidate": candidate,
            "preferences": data.get("preferences", {}) or {},
            "authorization": authorization,
            "programs": data.get("programs", []) or [],
            "verified_quantifiers": data.get("verified_quantifiers", {}) or {},
            "sensitive_wording": data.get("sensitive_wording", {}) or {},
            "resume_style_rules": data.get("resume_style_rules", {}) or {},
            "scoring_inputs": scoring_inputs,
        }
    }
    profile = Profile(
        name=slugify(_text(candidate.get("name")) or "resume-forge"),
        first_name=first,
        last_name=last,
        email=_text(candidate.get("email")),
        phone=_text(candidate.get("phone")),
        location=_text(candidate.get("current_location")),
        headline=_text(candidate.get("target_level")),
        links={
            key: _text(candidate.get(key))
            for key in ("linkedin", "github", "portfolio")
            if _text(candidate.get(key))
        },
        resume_path=resume_path,
        work_auth={
            "us_citizen": bool(authorization.get("us_citizen", False)),
            "work_authorized_us": bool(authorization.get("work_authorized_us", False)),
            "sponsorship_needed": bool(authorization.get("sponsorship_needed", False)),
            "itar_eligible": bool(authorization.get("itar_eligible", False)),
            "clearance_eligible": bool(authorization.get("clearance_eligible", False)),
            "gpa": scoring_inputs.get("gpa_for_scoring"),
        },
        education=[
            {
                "institution": _text(education.get("institution")),
                "degree": _text(education.get("degree")),
                "major": _text(education.get("major")),
                "graduation": _text(education.get("graduation")),
                "courses": education.get("relevant_courses", []) or [],
            }
        ],
    )

    facts: list[dict[str, Any]] = []
    experience: list[dict[str, Any]] = []
    skill_pool: list[str] = []
    for index, entry in enumerate(data.get("experience", []) or [], start=1):
        if not isinstance(entry, dict):
            continue
        bullets = [str(item).strip() for item in entry.get("bullet_facts", []) or [] if str(item).strip()]
        tags = [str(item) for item in entry.get("evidence_tags", []) or []]
        company = _text(entry.get("display_organization")) or _text(entry.get("organization"))
        experience.append(
            {
                "title": _text(entry.get("title")),
                "company": company,
                "location": _text(entry.get("location")),
                "start": _text(entry.get("start")),
                "end": _text(entry.get("end")),
                "bullets": bullets,
                "source_id": _text(entry.get("id")),
            }
        )
        entry_skills = _unique(_skills_from(tags) + _skills_from(" ".join(bullets)))
        skill_pool.extend(entry_skills)
        for bullet_index, bullet in enumerate(bullets, start=1):
            facts.append(
                {
                    "id": f"rf-exp-{index:02d}-{bullet_index:02d}",
                    "section": "experience",
                    "title": _text(entry.get("title")),
                    "company": company,
                    "text": bullet,
                    "skills": _unique(_skills_from(bullet, " ".join(tags))),
                    "source_id": _text(entry.get("id")),
                }
            )

    projects_data = _load_yaml(projects_path).get("projects", []) if projects_path else []
    projects: list[dict[str, Any]] = []
    for index, entry in enumerate(projects_data or [], start=1):
        if not isinstance(entry, dict):
            continue
        name = _text(entry.get("name")) or _text(entry.get("title"))
        bullets = [str(item).strip() for item in entry.get("bullets", []) or [] if str(item).strip()]
        tech_line = _text(entry.get("tech_line"))
        tags = [str(item) for item in entry.get("tags", []) or []]
        projects.append(
            {
                "title": name,
                "name": name,
                "company": "",
                "description": tech_line,
                "tech_line": tech_line,
                "tags": tags,
                "best_for": [str(item) for item in entry.get("best_for", []) or []],
                "bullets": bullets,
                "url": _project_url(entry),
                "dead": bool(entry.get("dead") or entry.get("death_reason")),
                "notes": _text(entry.get("notes")),
                "source_id": f"proj-{index:02d}",
            }
        )
        project_skills = _unique(_skills_from(tags) + _skills_from(tech_line, " ".join(bullets)))
        skill_pool.extend(project_skills)
        for bullet_index, bullet in enumerate(bullets, start=1):
            facts.append(
                {
                    "id": f"rf-proj-{index:02d}-{bullet_index:02d}",
                    "section": "projects",
                    "title": name,
                    "company": "",
                    "text": bullet,
                    "skills": _unique(_skills_from(bullet, tech_line, " ".join(tags))),
                    "source_id": f"proj-{index:02d}",
                }
            )

    for group in (evidence.get("skills", {}) or {}).values():
        if isinstance(group, dict):
            skill_pool.extend(
                canonical(str(name)) for name in group.keys()
            )  # evidence-matrix keys are curated; keep niche technologies

    profile.experience = experience
    profile.projects = projects
    profile.facts = facts
    profile.skills = _unique(skill_pool)
    profile.evidence = evidence
    profile.context = context

    if resume_path and Path(resume_path).suffix.lower() == ".pdf" and Path(resume_path).is_file():
        _merge_resume_bullets(profile, resume_path)
    return profile


def _merge_resume_bullets(profile: Profile, resume_path: str | Path) -> None:
    """Prefer the curated bullets in the published PDF over long profile notes.

    profile.yml's bullet_facts sometimes carry planning/correction notes after
    the resume-ready sentence. The published resume PDF is the source of truth
    for what was actually shipped on the page.
    """
    from ..matching.resume import profile_from_resume

    parsed = profile_from_resume(resume_path)

    def key(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()

    parsed_experience = {
        (key(item.get("title", "")), key(item.get("company", ""))): item
        for item in parsed.experience
    }
    parsed_education = parsed.education[0] if parsed.education else {}
    for edu in profile.education:
        for field in ("start", "end", "location"):
            if not edu.get(field) and parsed_education.get(field):
                edu[field] = parsed_education[field]
        if not edu.get("courses") and parsed_education.get("courses"):
            edu["courses"] = parsed_education["courses"]
    for entry in profile.experience:
        match = parsed_experience.get((key(entry.get("title", "")), key(entry.get("company", ""))))
        if match is None:
            # The PDF may use a shorter display company; match the title exactly
            # rather than a substring, so "Mentor" cannot inherit Hunter's bullets.
            for candidate in parsed.experience:
                if key(candidate.get("title", "")) == key(entry.get("title", "")):
                    match = candidate
                    break
        if match and match.get("bullets"):
            entry["bullets"] = list(match["bullets"])

    project_facts = [fact for fact in profile.facts if fact.get("section") == "projects"]
    experience_facts: list[dict[str, Any]] = []
    for index, entry in enumerate(profile.experience, start=1):
        tags = [str(item) for item in entry.get("evidence_tags", []) or []]
        for bullet_index, bullet in enumerate(entry.get("bullets", []), start=1):
            experience_facts.append(
                {
                    "id": f"rf-exp-{index:02d}-{bullet_index:02d}",
                    "section": "experience",
                    "title": _text(entry.get("title")),
                    "company": _text(entry.get("company")),
                    "text": str(bullet).strip(),
                    "skills": _unique(_skills_from(str(bullet), " ".join(tags))),
                    "source_id": _text(entry.get("source_id")),
                }
            )
    profile.facts = experience_facts + project_facts


def preferences_from_profile(data: dict[str, Any]) -> Preferences:
    """Map the resume-forge preference block to JobForge preferences."""
    preferences = data.get("preferences", {}) or {}
    authorization = data.get("authorization", {}) or {}
    return Preferences(
        locations=[str(item) for item in preferences.get("preferred_locations", []) or []],
        open_to_relocation=bool(preferences.get("open_to_relocation", False)),
        needs_sponsorship=bool(authorization.get("sponsorship_needed", False)),
        industries=[str(item) for item in preferences.get("target_domains", []) or []],
        comp_policy=dict(preferences.get("comp_policy", {}) or {}),
        min_match=55,
        one_application_per_company=True,
    )


def load_network(path: str | Path) -> dict[str, Any]:
    """Convert resume-forge network.yml to JobForge's network schema."""
    data = _load_yaml(path)
    connections: list[dict[str, Any]] = []
    for entry in data.get("connections", []) or []:
        if not isinstance(entry, dict):
            continue
        raw_strength = _text(entry.get("relationship")).lower()
        strength = raw_strength if raw_strength in {"strong", "medium", "weak"} else "medium"
        title = _text(entry.get("title"))
        how = " ".join(
            part
            for part in (_text(entry.get("how")), _text(entry.get("notes")))
            if part
        )
        probe = f"{title} {how}".lower()
        if "recruit" in probe:
            relationship = "recruiter"
        elif "hiring manager" in probe or "engineering manager" in probe:
            relationship = "hiring_manager"
        elif "friend" in probe:
            relationship = "friend"
        elif "colleague" in probe or "worked with" in probe:
            relationship = "former_colleague"
        else:
            relationship = "other"
        connections.append(
            {
                "name": _text(entry.get("name")),
                "company": _text(entry.get("company")),
                "role": title,
                "relationship": relationship,
                "strength": strength,
                "last_contact": _text(entry.get("last_contacted")),
                "notes": how,
            }
        )
    return {"schema_version": 1, "connections": connections}


def load_applications(path: str | Path) -> list[dict[str, Any]]:
    """Normalize resume-forge application entries."""
    data = _load_yaml(path)
    entries: list[dict[str, Any]] = []
    for entry in data.get("applications", []) or []:
        if not isinstance(entry, dict):
            continue
        outcome = entry.get("outcome", {}) or {}
        raw_status = _text(entry.get("status")) or _text(outcome.get("current_stage")) or "Not Started"
        key = re.sub(r"[^a-z]+", " ", raw_status.lower()).strip()
        status = STATUS_MAP.get(key, STATUS_MAP.get(key.replace(" ", "_"), "draft"))
        salary_min, salary_max = _salary_range(_text(entry.get("salary")))
        entries.append(
            {
                "source_id": str(entry.get("id") or len(entries) + 1),
                "company": _text(entry.get("company")),
                "role": _text(entry.get("role")),
                "location": _text(entry.get("location")),
                "salary_min": salary_min,
                "salary_max": salary_max,
                "applied_at": _text(entry.get("applied_at")),
                "status": status,
                "raw_status": raw_status,
                "url": _text(entry.get("source_url")),
                "resume_path": _text(entry.get("resume_path")),
                "notes": _text(entry.get("notes")),
                "channel": _text(outcome.get("channel")),
                "email": _text(outcome.get("email_used")),
                "stage": _text(outcome.get("current_stage")),
                "outcome_notes": _text(outcome.get("notes_outcome")),
            }
        )
    return entries


def import_applications(
    conn: sqlite3.Connection,
    profile_id: int,
    path: str | Path,
    *,
    include_unstarted: bool = False,
) -> dict[str, int]:
    """Create placeholder jobs and application rows from resume-forge history."""
    counts = {"imported": 0, "skipped": 0}
    for entry in load_applications(path):
        if entry["status"] == "draft" and not include_unstarted:
            counts["skipped"] += 1
            continue
        unique_url = entry["url"] or f"resume-forge://application/{entry['source_id']}"
        notes = " | ".join(
            part
            for part in (
                f"resume-forge id {entry['source_id']}",
                f"status {entry['raw_status']}",
                f"stage {entry['stage']}" if entry["stage"] else "",
                f"channel {entry['channel']}" if entry["channel"] else "",
                f"email {entry['email']}" if entry["email"] else "",
                entry["notes"],
                entry["outcome_notes"],
            )
            if part
        )
        job_id = db.upsert_job(
            conn,
            Job(
                source="resume-forge",
                source_id=entry["source_id"],
                company=entry["company"] or "Unknown",
                title=entry["role"] or "Unknown role",
                location=entry["location"],
                salary_min=entry["salary_min"],
                salary_max=entry["salary_max"],
                url=unique_url,
                apply_url=unique_url,
                description=notes,
                raw={"resume_forge": entry},
            ),
        )
        created = entry["applied_at"] or utcnow()
        db.create_application(
            conn,
            Application(
                job_id=job_id,
                profile_id=profile_id,
                status=entry["status"],
                ats="",
                resume_path="",
                notes=notes,
                created_at=created,
                submitted_at=created if entry["status"] in {"submitted", "interview", "offer", "rejected"} else None,
                updated_at=utcnow(),
            ),
        )
        counts["imported"] += 1
    return counts
